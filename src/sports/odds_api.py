from __future__ import annotations

from datetime import datetime, timezone

import httpx

from src.sports.models import BookOdds, SportsEvent
from src.sports.odds_math import american_to_decimal
from src.sports.regions import infer_book_region


class OddsApiClient:
  """Optional The Odds API client. Needs ODDS_API_KEY. Free tier ~500 req/mo."""

  BASE = "https://api.the-odds-api.com/v4"
  # Keys we care about; inactive ones are skipped via /sports (free).
  SPORTS = [
    "americanfootball_nfl",
    "basketball_nba",
    "baseball_mlb",
    "icehockey_nhl",
    "americanfootball_ncaaf",
    "basketball_ncaab",
    "soccer_epl",
    "soccer_usa_mls",
    "mma_mixed_martial_arts",
  ]

  def __init__(self, api_key: str = "", timeout: float = 25.0) -> None:
    self._api_key = (api_key or "").strip()
    self._timeout = timeout
    self._http: httpx.AsyncClient | None = None
    self.remaining_requests: int | None = None
    self.last_status: str = "idle"
    self._cached_events: list[SportsEvent] = []
    self._cache_at: float = 0.0

  @property
  def enabled(self) -> bool:
    return bool(self._api_key)

  async def open(self) -> None:
    self._http = httpx.AsyncClient(timeout=self._timeout)

  async def close(self) -> None:
    if self._http is not None:
      await self._http.aclose()
      self._http = None

  def cache_age_sec(self, now: float | None = None) -> float | None:
    if not self._cache_at:
      return None
    import time

    return max(0.0, (now or time.time()) - self._cache_at)

  async def refresh_quota(self) -> int | None:
    """Spend at most 1 credit to learn remaining quota."""
    if not self.enabled or self._http is None:
      return None
    try:
      r = await self._http.get(
        f"{self.BASE}/sports/baseball_mlb/odds",
        params={
          "apiKey": self._api_key,
          "regions": "us",
          "markets": "h2h",
          "oddsFormat": "american",
        },
      )
      self._update_remaining(r)
      if r.status_code == 200 and r.json():
        # Keep a tiny cache crumb if MLB has lines.
        now = datetime.now(timezone.utc).isoformat()
        parsed = self._parse_rows("baseball_mlb", r.json(), now)
        if parsed:
          import time

          self._cached_events = parsed
          self._cache_at = time.time()
    except Exception:
      pass
    return self.remaining_requests

  async def fetch_all(
    self,
    regions: str = "us,us2,uk,eu",
    *,
    min_remaining: int = 12,
    use_cache_if_skipped: bool = True,
  ) -> list[SportsEvent]:
    """
    Fetch odds. Stops early when quota is exhausted.
    Returns cached events when skipping to save credits.
    """
    if not self.enabled or self._http is None:
      self.last_status = "disabled"
      return []

    if (
      self.remaining_requests is not None
      and self.remaining_requests < min_remaining
    ):
      self.last_status = (
        f"paused_quota ({self.remaining_requests} left — "
        f"need ≥{min_remaining} to poll safely)"
      )
      return list(self._cached_events) if use_cache_if_skipped else []

    active = await self._active_sports()
    sport_keys = [k for k in self.SPORTS if k in active]
    if not sport_keys:
      self.last_status = "no_active_sports"
      return list(self._cached_events) if use_cache_if_skipped else []

    # Never start a multi-sport poll we can't finish.
    if (
      self.remaining_requests is not None
      and self.remaining_requests < len(sport_keys)
    ):
      self.last_status = (
        f"paused_quota ({self.remaining_requests} left, "
        f"need {len(sport_keys)} for active sports)"
      )
      return list(self._cached_events) if use_cache_if_skipped else []

    out: list[SportsEvent] = []
    now = datetime.now(timezone.utc).isoformat()
    for sport_key in sport_keys:
      if (
        self.remaining_requests is not None
        and self.remaining_requests < 1
      ):
        self.last_status = f"paused_quota ({self.remaining_requests} left)"
        break
      try:
        r = await self._http.get(
          f"{self.BASE}/sports/{sport_key}/odds",
          params={
            "apiKey": self._api_key,
            "regions": regions,
            "markets": "h2h,spreads,totals",
            "oddsFormat": "american",
          },
        )
        self._update_remaining(r)
        if r.status_code == 401:
          self.last_status = "invalid_key"
          break
        if r.status_code == 429:
          self.last_status = "rate_limited"
          break
        if r.status_code != 200:
          continue
        rows = r.json()
      except Exception as exc:
        self.last_status = f"error: {exc}"
        continue
      out.extend(self._parse_rows(sport_key, rows or [], now))

    if out:
      import time

      self._cached_events = out
      self._cache_at = time.time()
      rem = (
        f", {self.remaining_requests} credits left"
        if self.remaining_requests is not None
        else ""
      )
      self.last_status = f"ok ({len(out)} events{rem})"
      return out

    if self._cached_events and use_cache_if_skipped:
      self.last_status = (
        f"empty_live_using_cache ({len(self._cached_events)} events"
        f"{', ' + str(self.remaining_requests) + ' left' if self.remaining_requests is not None else ''})"
      )
      return list(self._cached_events)

    rem = (
      f" ({self.remaining_requests} credits left)"
      if self.remaining_requests is not None
      else ""
    )
    self.last_status = f"empty{rem}"
    return []

  async def _active_sports(self) -> set[str]:
    """GET /sports does not consume quota."""
    assert self._http is not None
    try:
      r = await self._http.get(
        f"{self.BASE}/sports",
        params={"apiKey": self._api_key},
      )
      if r.status_code != 200:
        return set(self.SPORTS)
      return {
        row["key"]
        for row in (r.json() or [])
        if row.get("active") and row.get("key")
      }
    except Exception:
      return set(self.SPORTS)

  def _update_remaining(self, r: httpx.Response) -> None:
    if "x-requests-remaining" not in r.headers:
      return
    try:
      self.remaining_requests = int(r.headers["x-requests-remaining"])
    except ValueError:
      pass

  def _parse_rows(
    self, sport_key: str, rows: list, now: str
  ) -> list[SportsEvent]:
    sport = sport_key.split("_")[0]
    league = sport_key.split("_", 1)[-1].upper()
    out: list[SportsEvent] = []
    for row in rows:
      event = SportsEvent(
        event_id=f"oddsapi-{row.get('id')}",
        sport=sport,
        league=league,
        home=row.get("home_team") or "Home",
        away=row.get("away_team") or "Away",
        commence_time=row.get("commence_time"),
      )
      for book in row.get("bookmakers") or []:
        book_name = book.get("title") or book.get("key") or "Book"
        region = infer_book_region(book_name)
        for market in book.get("markets") or []:
          mkey = market.get("key") or "h2h"
          for outcome in market.get("outcomes") or []:
            name = (outcome.get("name") or "").strip()
            am = outcome.get("price")
            if am is None:
              continue
            try:
              american = int(am)
            except (TypeError, ValueError):
              continue
            dec = american_to_decimal(american)
            if not dec:
              continue
            if mkey == "h2h":
              if name == event.home:
                side = "home"
              elif name == event.away:
                side = "away"
              elif name.lower() == "draw":
                side = "draw"
              else:
                side = name.lower()
            elif mkey == "spreads":
              side = (
                "home"
                if name == event.home
                else "away"
                if name == event.away
                else name.lower()
              )
            else:
              side = name.lower()
            event.odds.append(
              BookOdds(
                book=book_name,
                source="odds_api",
                outcome=side,
                american=american,
                decimal=dec,
                line=(
                  float(outcome["point"])
                  if outcome.get("point") is not None
                  else None
                ),
                market=mkey,
                region=region,
                fetched_at=now,
              )
            )
      if event.odds:
        out.append(event)
    return out
