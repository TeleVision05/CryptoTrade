from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.sports.action_network import ActionNetworkClient
from src.sports.espn import EspnOddsClient
from src.sports.models import ArbLeg, BookOdds, SportsArb, SportsEvent
from src.sports.odds_api import OddsApiClient
from src.sports.odds_math import three_way_arb, two_way_arb
from src.sports.regions import normalize_region


def _norm_name(name: str) -> str:
  return "".join(ch for ch in name.lower() if ch.isalnum())


def _event_key(ev: SportsEvent) -> str:
  return f"{ev.league}:{_norm_name(ev.home)}:{_norm_name(ev.away)}"


def _parse_ts(raw: str | None) -> datetime | None:
  if not raw:
    return None
  try:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))
  except ValueError:
    return None


def _odds_age_sec(odd: BookOdds, now: datetime) -> float | None:
  ts = _parse_ts(odd.fetched_at)
  if ts is None:
    return None
  return max(0.0, (now - ts).total_seconds())


def _is_soccer_like(ev: SportsEvent) -> bool:
  if (ev.sport or "").lower() == "soccer":
    return True
  league = (ev.league or "").upper()
  return any(tok in league for tok in ("EPL", "MLS", "SOCCER", "UEFA", "LIGA"))


@dataclass
class ScanSnapshot:
  updated_at: str
  scan_ms: int
  events: int
  books: int
  sources: list[str]
  arbs: list[dict]
  near_misses: list[dict]
  message: str
  odds_api_remaining: int | None = None
  filtered_out: int = 0


@dataclass
class SportsArbScanner:
  """
  Multi-source sports betting arbitrage scanner.

  Free sources (auto): Action Network, ESPN.
  Odds API: manual only via fetch_odds_api_manual() / dashboard button.

  Guards:
  - same-region legs only (never US↔EU/UK)
  - soccer h2h requires 3-way (home/draw/away)
  - drop stale odds and absurd % edges (data bugs)
  """

  min_profit_pct: float = 0.35
  stake_total: float = 100.0
  scan_interval_sec: float = 45.0
  max_odds_age_sec: float = 360.0
  max_profit_pct: float = 5.0
  odds_api_interval_sec: float = 300.0  # unused; config compat
  leagues: list[str] = field(
    default_factory=lambda: [
      "nfl", "nba", "mlb", "nhl", "ncaaf", "ncaab", "wnba", "mls", "soccer", "ufc",
    ]
  )
  odds_api_key: str = ""

  def __post_init__(self) -> None:
    self._an = ActionNetworkClient()
    self._espn = EspnOddsClient()
    self._odds_api = OddsApiClient(self.odds_api_key)
    self._running = False
    self._task: asyncio.Task | None = None
    self._odds_api_cached: list[SportsEvent] = []
    self._latest = ScanSnapshot(
      updated_at=datetime.now(timezone.utc).isoformat(),
      scan_ms=0,
      events=0,
      books=0,
      sources=[],
      arbs=[],
      near_misses=[],
      message="Not started",
    )
    self._opened = False

  @property
  def latest(self) -> ScanSnapshot:
    return self._latest

  async def open(self) -> None:
    if self._opened:
      return
    await self._an.open()
    await self._espn.open()
    await self._odds_api.open()
    self._opened = True

  async def close(self) -> None:
    self._running = False
    if self._task is not None:
      self._task.cancel()
      try:
        await self._task
      except asyncio.CancelledError:
        pass
      self._task = None
    await self._an.close()
    await self._espn.close()
    await self._odds_api.close()
    self._opened = False

  def start_background(self) -> None:
    if self._task is not None:
      return
    self._running = True
    self._task = asyncio.create_task(self._loop())

  async def _loop(self) -> None:
    await self.open()
    while self._running:
      try:
        await self.scan_once()
      except Exception as exc:
        self._latest.message = f"Scan error: {exc}"
      await asyncio.sleep(self.scan_interval_sec)

  async def scan_once(self, *, include_odds_api_cache: bool = True) -> ScanSnapshot:
    """Background / free-source scan. Never spends Odds API credits."""
    await self.open()
    t0 = time.monotonic()
    sources: list[str] = []
    raw: list[SportsEvent] = []

    an_events = await self._an.fetch_all(self.leagues)
    if an_events:
      sources.append("action_network")
      raw.extend(an_events)

    espn_events = await self._espn.fetch_all(
      [k for k in self.leagues if k in {"nfl", "nba", "mlb", "nhl", "ncaaf", "ncaab", "wnba", "mls"}]
    )
    if espn_events:
      sources.append("espn")
      raw.extend(espn_events)

    if include_odds_api_cache and self._odds_api_cached:
      sources.append("odds_api_cache")
      raw.extend(self._odds_api_cached)
      rem = self._odds_api.remaining_requests
      rem_s = f", {rem} credits left" if rem is not None else ""
      odds_note = f" · Odds API cache in use (manual only{rem_s})"
    elif self._odds_api.enabled:
      rem = self._odds_api.remaining_requests
      rem_s = f" · {rem} credits left" if rem is not None else ""
      odds_note = f" · Odds API manual only — click Fetch Odds API{rem_s}"
    else:
      odds_note = " · add ODDS_API_KEY + manual fetch for 40+ books"

    return self._finalize_snapshot(raw, sources, t0, odds_note)

  async def fetch_odds_api_manual(self) -> ScanSnapshot:
    """Spend Odds API credits once (dashboard button), then merge free sources."""
    await self.open()
    if not self._odds_api.enabled:
      snap = await self.scan_once(include_odds_api_cache=False)
      self._latest.message = (snap.message or "") + " · Odds API key missing"
      return self._latest

    t0 = time.monotonic()
    api_events = await self._odds_api.fetch_all(min_remaining=1)
    if api_events:
      self._odds_api_cached = api_events
    odds_note = f" · Odds API manual fetch: {self._odds_api.last_status}"

    sources: list[str] = []
    raw: list[SportsEvent] = []
    an_events = await self._an.fetch_all(self.leagues)
    if an_events:
      sources.append("action_network")
      raw.extend(an_events)
    espn_events = await self._espn.fetch_all(
      [k for k in self.leagues if k in {"nfl", "nba", "mlb", "nhl", "ncaaf", "ncaab", "wnba", "mls"}]
    )
    if espn_events:
      sources.append("espn")
      raw.extend(espn_events)
    if self._odds_api_cached:
      sources.append("odds_api")
      raw.extend(self._odds_api_cached)

    return self._finalize_snapshot(raw, sources, t0, odds_note)

  def _finalize_snapshot(
    self,
    raw: list[SportsEvent],
    sources: list[str],
    t0: float,
    odds_note: str,
  ) -> ScanSnapshot:
    merged = self._merge_events(raw)
    arbs, near, filtered = self._find_arbs(merged)
    books = {o.book for ev in merged for o in ev.odds}
    ms = int((time.monotonic() - t0) * 1000)
    msg = (
      f"Scanned {len(merged)} events across {len(books)} books "
      f"via {', '.join(sources) or 'no sources'} — "
      f"{len(arbs)} actionable arb(s), {len(near)} near-miss(es)"
    )
    if filtered:
      msg += f", filtered {filtered} fake/cross-region/stale"
    msg += odds_note
    self._latest = ScanSnapshot(
      updated_at=datetime.now(timezone.utc).isoformat(),
      scan_ms=ms,
      events=len(merged),
      books=len(books),
      sources=sources,
      arbs=[a.to_dict() for a in arbs[:40]],
      near_misses=near[:20],
      message=msg,
      odds_api_remaining=self._odds_api.remaining_requests,
      filtered_out=filtered,
    )
    return self._latest

  def _merge_events(self, events: list[SportsEvent]) -> list[SportsEvent]:
    buckets: dict[str, SportsEvent] = {}
    for ev in events:
      key = _event_key(ev)
      if key not in buckets:
        buckets[key] = SportsEvent(
          event_id=ev.event_id,
          sport=ev.sport,
          league=ev.league,
          home=ev.home,
          away=ev.away,
          commence_time=ev.commence_time,
          odds=[],
        )
      seen = {
        (o.book, o.market, o.outcome, o.line, o.region, o.source)
        for o in buckets[key].odds
      }
      for o in ev.odds:
        o.region = normalize_region(o.region)
        sig = (o.book, o.market, o.outcome, o.line, o.region, o.source)
        if sig in seen:
          continue
        rival = next(
          (
            x
            for x in buckets[key].odds
            if x.book == o.book
            and x.market == o.market
            and x.outcome == o.outcome
            and x.line == o.line
            and x.region == o.region
          ),
          None,
        )
        if rival is not None:
          if o.decimal > rival.decimal:
            buckets[key].odds.remove(rival)
            buckets[key].odds.append(o)
          continue
        buckets[key].odds.append(o)
        seen.add(sig)
    return list(buckets.values())

  def _find_arbs(
    self, events: list[SportsEvent]
  ) -> tuple[list[SportsArb], list[dict], int]:
    arbs: list[SportsArb] = []
    near: list[dict] = []
    filtered = 0
    now = datetime.now(timezone.utc)
    for ev in events:
      for market in ("h2h", "spreads", "totals"):
        found = self._arb_for_market(ev, market, now)
        for item in found:
          arb = item.get("arb")
          if arb is None:
            if item.get("near"):
              near.append(item["near"])
            continue
          reason = self._reject_reason(arb)
          if reason:
            filtered += 1
            continue
          if arb.profit_pct >= self.min_profit_pct:
            arbs.append(arb)
          else:
            near.append(
              {
                "event": arb.event,
                "league": arb.league,
                "market": arb.market,
                "profit_pct": round(arb.profit_pct, 3),
                "best": " / ".join(
                  f"{leg.outcome} {leg.american:+d}@{leg.book}[{leg.region}]"
                  for leg in arb.legs
                ),
                "region": arb.region,
              }
            )
    arbs.sort(key=lambda a: a.profit_pct, reverse=True)
    near.sort(key=lambda n: n["profit_pct"], reverse=True)
    return arbs, near, filtered

  def _reject_reason(self, arb: SportsArb) -> str | None:
    if arb.profit_pct > self.max_profit_pct:
      return "absurd_edge"
    if arb.max_odds_age_sec is not None and arb.max_odds_age_sec > self.max_odds_age_sec:
      return "stale"
    books = {leg.book for leg in arb.legs}
    if len(books) < 2:
      return "same_book"
    regions = {leg.region for leg in arb.legs}
    if len(regions) != 1:
      return "cross_region"
    return None

  def _arb_for_market(
    self, ev: SportsEvent, market: str, now: datetime
  ) -> list[dict]:
    odds = [o for o in ev.odds if o.market == market]
    if not odds:
      return []

    results: list[dict] = []
    regions = sorted({normalize_region(o.region) for o in odds})
    for region in regions:
      region_odds = [o for o in odds if normalize_region(o.region) == region]
      if market == "h2h":
        results.extend(self._h2h_region(ev, region_odds, region, now))
      elif market == "spreads":
        results.extend(self._spreads_region(ev, region_odds, region, now))
      elif market == "totals":
        results.extend(self._totals_region(ev, region_odds, region, now))
    return [r for r in results if r is not None]

  def _h2h_region(
    self,
    ev: SportsEvent,
    odds: list[BookOdds],
    region: str,
    now: datetime,
  ) -> list[dict]:
    has_draw = any(o.outcome == "draw" for o in odds)
    soccer = _is_soccer_like(ev)

    # Soccer (and any market with a draw) must be 3-way — 2-way home/away is fake.
    if soccer or has_draw:
      home = self._best_by_outcome(odds, "home")
      away = self._best_by_outcome(odds, "away")
      draw = self._best_by_outcome(odds, "draw")
      if not (home and away and draw):
        return []
      return [
        self._build_three_way(
          ev, "h2h", region, now,
          ("away", away), ("home", home), ("draw", draw),
        )
      ]

    home = self._best_by_outcome(odds, "home")
    away = self._best_by_outcome(odds, "away")
    if not (home and away):
      return []
    return [
      self._build_two_way(
        ev, "h2h", region, now, "away", away, "home", home
      )
    ]

  def _spreads_region(
    self,
    ev: SportsEvent,
    odds: list[BookOdds],
    region: str,
    now: datetime,
  ) -> list[dict]:
    results: list[dict] = []
    mags = sorted(
      {
        abs(round(float(o.line), 2))
        for o in odds
        if o.line is not None and abs(o.line) > 0
      }
    )
    for mag in mags:
      away = self._best_by_outcome(
        [
          o
          for o in odds
          if o.outcome == "away"
          and o.line is not None
          and abs(o.line - mag) < 0.011
        ],
        "away",
      )
      home = self._best_by_outcome(
        [
          o
          for o in odds
          if o.outcome == "home"
          and o.line is not None
          and abs(o.line + mag) < 0.011
        ],
        "home",
      )
      if away and home:
        results.append(
          self._build_two_way(
            ev,
            "spreads",
            region,
            now,
            f"away {away.line:+g}",
            away,
            f"home {home.line:+g}",
            home,
          )
        )
    return results

  def _totals_region(
    self,
    ev: SportsEvent,
    odds: list[BookOdds],
    region: str,
    now: datetime,
  ) -> list[dict]:
    results: list[dict] = []
    totals = sorted({round(float(o.line), 2) for o in odds if o.line is not None})
    for tot in totals:
      over = self._best_by_outcome(
        [
          o
          for o in odds
          if o.outcome == "over"
          and o.line is not None
          and abs(o.line - tot) < 0.01
        ],
        "over",
      )
      under = self._best_by_outcome(
        [
          o
          for o in odds
          if o.outcome == "under"
          and o.line is not None
          and abs(o.line - tot) < 0.01
        ],
        "under",
      )
      if over and under:
        results.append(
          self._build_two_way(
            ev,
            "totals",
            region,
            now,
            f"over {tot:g}",
            over,
            f"under {tot:g}",
            under,
          )
        )
    return results

  @staticmethod
  def _best_by_outcome(odds: list[BookOdds], outcome: str) -> BookOdds | None:
    cands = [o for o in odds if o.outcome == outcome]
    if not cands:
      return None
    return max(cands, key=lambda o: o.decimal)

  def _age_pair(
    self, now: datetime, *odds: BookOdds
  ) -> tuple[list[float | None], float | None]:
    ages = [_odds_age_sec(o, now) for o in odds]
    known = [a for a in ages if a is not None]
    return ages, max(known) if known else None

  def _build_two_way(
    self,
    ev: SportsEvent,
    market: str,
    region: str,
    now: datetime,
    label_a: str,
    odd_a: BookOdds,
    label_b: str,
    odd_b: BookOdds,
  ) -> dict:
    desc = (
      f"{label_a} {odd_a.american:+d}@{odd_a.book}[{region}] / "
      f"{label_b} {odd_b.american:+d}@{odd_b.book}[{region}]"
    )
    sizing = two_way_arb(odd_a.decimal, odd_b.decimal, self.stake_total)
    if sizing is None:
      inv = 1 / odd_a.decimal + 1 / odd_b.decimal
      return {
        "arb": None,
        "near": {
          "event": f"{ev.away} @ {ev.home}",
          "league": ev.league,
          "market": market,
          "profit_pct": round((1 - inv) * 100, 3),
          "best": desc,
          "region": region,
        }
        if odd_a.book != odd_b.book
        else None,
      }

    if odd_a.book == odd_b.book:
      return {"arb": None, "near": None}

    ages, max_age = self._age_pair(now, odd_a, odd_b)
    arb = SportsArb(
      id=str(uuid.uuid4()),
      sport=ev.sport,
      league=ev.league,
      event=f"{ev.away} @ {ev.home}",
      market=market,
      profit_pct=round(sizing["profit_pct"], 3),
      profit_usd=sizing["profit_usd"],
      stake_total=self.stake_total,
      payout=sizing["payout"],
      commence_time=ev.commence_time,
      sources=sorted({odd_a.source, odd_b.source}),
      region=region,
      n_way=2,
      max_odds_age_sec=round(max_age, 1) if max_age is not None else None,
      actionable=True,
      legs=[
        ArbLeg(
          outcome=label_a,
          book=odd_a.book,
          source=odd_a.source,
          american=odd_a.american,
          decimal=round(odd_a.decimal, 4),
          stake_usd=sizing["stake_a"],
          line=odd_a.line,
          market=market,
          region=region,
          odds_age_sec=round(ages[0], 1) if ages[0] is not None else None,
        ),
        ArbLeg(
          outcome=label_b,
          book=odd_b.book,
          source=odd_b.source,
          american=odd_b.american,
          decimal=round(odd_b.decimal, 4),
          stake_usd=sizing["stake_b"],
          line=odd_b.line,
          market=market,
          region=region,
          odds_age_sec=round(ages[1], 1) if ages[1] is not None else None,
        ),
      ],
    )
    return {"arb": arb, "near": None}

  def _build_three_way(
    self,
    ev: SportsEvent,
    market: str,
    region: str,
    now: datetime,
    *labeled: tuple[str, BookOdds],
  ) -> dict:
    labels = [x[0] for x in labeled]
    odds = [x[1] for x in labeled]
    desc = " / ".join(
      f"{lab} {o.american:+d}@{o.book}[{region}]" for lab, o in labeled
    )
    sizing = three_way_arb([o.decimal for o in odds], self.stake_total)
    if sizing is None:
      inv = sum(1 / o.decimal for o in odds)
      return {
        "arb": None,
        "near": {
          "event": f"{ev.away} @ {ev.home}",
          "league": ev.league,
          "market": market,
          "profit_pct": round((1 - inv) * 100, 3),
          "best": desc,
          "region": region,
        },
      }

    books = {o.book for o in odds}
    if len(books) < 2:
      return {"arb": None, "near": None}

    ages, max_age = self._age_pair(now, *odds)
    stakes = sizing["stakes"]
    arb = SportsArb(
      id=str(uuid.uuid4()),
      sport=ev.sport,
      league=ev.league,
      event=f"{ev.away} @ {ev.home}",
      market=market,
      profit_pct=round(sizing["profit_pct"], 3),
      profit_usd=sizing["profit_usd"],
      stake_total=self.stake_total,
      payout=sizing["payout"],
      commence_time=ev.commence_time,
      sources=sorted({o.source for o in odds}),
      region=region,
      n_way=3,
      max_odds_age_sec=round(max_age, 1) if max_age is not None else None,
      actionable=True,
      legs=[
        ArbLeg(
          outcome=labels[i],
          book=odds[i].book,
          source=odds[i].source,
          american=odds[i].american,
          decimal=round(odds[i].decimal, 4),
          stake_usd=stakes[i],
          line=odds[i].line,
          market=market,
          region=region,
          odds_age_sec=round(ages[i], 1) if ages[i] is not None else None,
        )
        for i in range(3)
      ],
    )
    return {"arb": arb, "near": None}
