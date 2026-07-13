from __future__ import annotations

from datetime import datetime, timezone

import httpx

from src.sports.models import BookOdds, SportsEvent
from src.sports.odds_math import american_to_decimal

# Action Network public web API — multi-book odds without a key.
LEAGUES = {
  "nfl": ("football", "NFL"),
  "nba": ("basketball", "NBA"),
  "mlb": ("baseball", "MLB"),
  "nhl": ("hockey", "NHL"),
  "ncaaf": ("football", "NCAAF"),
  "ncaab": ("basketball", "NCAAB"),
  "soccer": ("soccer", "Soccer"),
  "wnba": ("basketball", "WNBA"),
  "ufc": ("mma", "UFC"),
  "mls": ("soccer", "MLS"),
}

# Skip synthetic lines — not bettable books.
SKIP_BOOKS = {"open", "consensus", "siconsensus"}


class ActionNetworkClient:
  BASE = "https://api.actionnetwork.com/web/v1"

  def __init__(self, timeout: float = 25.0) -> None:
    self._timeout = timeout
    self._http: httpx.AsyncClient | None = None
    self._books: dict[int, str] = {}

  async def open(self) -> None:
    self._http = httpx.AsyncClient(
      timeout=self._timeout,
      headers={
        "User-Agent": "Mozilla/5.0 (compatible; CryptoTradeSports/1.0)",
        "Accept": "application/json",
      },
    )
    await self._load_books()

  async def close(self) -> None:
    if self._http is not None:
      await self._http.aclose()
      self._http = None

  async def _load_books(self) -> None:
    if self._http is None:
      return
    try:
      r = await self._http.get(f"{self.BASE}/books")
      r.raise_for_status()
      payload = r.json()
      rows = payload.get("books") if isinstance(payload, dict) else payload
      for b in rows or []:
        bid = b.get("id")
        name = b.get("display_name") or b.get("source_name") or str(bid)
        source = str(b.get("source_name") or "").lower()
        if bid is None or source in SKIP_BOOKS:
          continue
        # Prefer clean brand name without state suffix when possible.
        brand = (
          "DraftKings"
          if "draftkings" in source or source.startswith("dk")
          else "FanDuel"
          if "fanduel" in source
          else "BetMGM"
          if "mgm" in source or "playmgm" in source
          else "Caesars"
          if "caesar" in source
          else "BetRivers"
          if "river" in source or "sugarhouse" in source
          else "bet365"
          if "bet365" in source
          else "PointsBet"
          if "pointsbet" in source
          else "Fanatics"
          if "fanatic" in source
          else "ESPN BET"
          if "espn" in source
          else name.split()[0]
        )
        self._books[int(bid)] = brand
    except Exception:
      self._books = {
        68: "DraftKings",
        69: "FanDuel",
        71: "BetRivers",
        75: "BetMGM",
        79: "bet365",
        123: "Caesars",
      }

  async def fetch_league(self, league_key: str) -> list[SportsEvent]:
    if self._http is None:
      return []
    sport, league = LEAGUES.get(league_key, ("unknown", league_key.upper()))
    try:
      r = await self._http.get(f"{self.BASE}/scoreboard/{league_key}")
      r.raise_for_status()
      data = r.json()
    except Exception:
      return []

    now = datetime.now(timezone.utc).isoformat()
    out: list[SportsEvent] = []
    for g in data.get("games") or []:
      status = str(g.get("status") or g.get("real_status") or "").lower()
      if status in {"complete", "final", "closed", "cancelled", "postponed"}:
        continue
      teams = g.get("teams") or []
      if len(teams) < 2:
        continue
      # Action Network lists away then home in teams[], matching away/home ids.
      away_id = g.get("away_team_id")
      home_id = g.get("home_team_id")
      by_id = {t.get("id"): t for t in teams}
      away = by_id.get(away_id) or teams[0]
      home = by_id.get(home_id) or teams[1]
      away_name = away.get("full_name") or away.get("display_name") or "Away"
      home_name = home.get("full_name") or home.get("display_name") or "Home"
      event = SportsEvent(
        event_id=f"an-{league_key}-{g.get('id')}",
        sport=sport,
        league=league,
        home=home_name,
        away=away_name,
        commence_time=g.get("start_time"),
      )
      for o in g.get("odds") or []:
        # Full-game lines only — period/live rows create fake cross-market arbs.
        if str(o.get("type") or "game").lower() != "game":
          continue
        book_id = o.get("book_id")
        book = self._books.get(int(book_id)) if book_id is not None else None
        if not book:
          continue
        self._add_market(event, book, o, now)
      if event.odds:
        out.append(event)
    return out

  def _add_market(
    self, event: SportsEvent, book: str, o: dict, fetched_at: str
  ) -> None:
    pairs = [
      ("h2h", "away", o.get("ml_away"), None),
      ("h2h", "home", o.get("ml_home"), None),
      ("spreads", "away", o.get("spread_away_line"), o.get("spread_away")),
      ("spreads", "home", o.get("spread_home_line"), o.get("spread_home")),
      ("totals", "over", o.get("over"), o.get("total")),
      ("totals", "under", o.get("under"), o.get("total")),
    ]
    if o.get("draw") is not None:
      pairs.append(("h2h", "draw", o.get("draw"), None))
    for market, outcome, american, line in pairs:
      if american is None:
        continue
      try:
        am = int(american)
      except (TypeError, ValueError):
        continue
      dec = american_to_decimal(am)
      if dec is None:
        continue
      event.odds.append(
        BookOdds(
          book=book,
          source="action_network",
          outcome=outcome,
          american=am,
          decimal=dec,
          line=float(line) if line is not None else None,
          market=market,
          region="us",
          fetched_at=fetched_at,
        )
      )

  async def fetch_all(self, leagues: list[str] | None = None) -> list[SportsEvent]:
    keys = leagues or list(LEAGUES.keys())
    events: list[SportsEvent] = []
    for key in keys:
      events.extend(await self.fetch_league(key))
    return events
