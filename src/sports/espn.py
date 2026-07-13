from __future__ import annotations

from datetime import datetime, timezone

import httpx

from src.sports.models import BookOdds, SportsEvent
from src.sports.odds_math import american_to_decimal

# ESPN scoreboard embeds DraftKings (and sometimes other) moneylines.
ESPN_PATHS = {
  "nfl": ("football", "nfl", "football", "NFL"),
  "nba": ("basketball", "nba", "basketball", "NBA"),
  "mlb": ("baseball", "mlb", "baseball", "MLB"),
  "nhl": ("hockey", "nhl", "hockey", "NHL"),
  "ncaaf": ("football", "college-football", "football", "NCAAF"),
  "ncaab": ("basketball", "mens-college-basketball", "basketball", "NCAAB"),
  "wnba": ("basketball", "wnba", "basketball", "WNBA"),
  "mls": ("soccer", "usa.1", "soccer", "MLS"),
}


class EspnOddsClient:
  BASE = "https://site.api.espn.com/apis/site/v2/sports"

  def __init__(self, timeout: float = 20.0) -> None:
    self._timeout = timeout
    self._http: httpx.AsyncClient | None = None

  async def open(self) -> None:
    self._http = httpx.AsyncClient(
      timeout=self._timeout,
      headers={"User-Agent": "Mozilla/5.0 (compatible; CryptoTradeSports/1.0)"},
    )

  async def close(self) -> None:
    if self._http is not None:
      await self._http.aclose()
      self._http = None

  async def fetch_league(self, league_key: str) -> list[SportsEvent]:
    if self._http is None or league_key not in ESPN_PATHS:
      return []
    sport_path, league_path, sport, league = ESPN_PATHS[league_key]
    try:
      r = await self._http.get(f"{self.BASE}/{sport_path}/{league_path}/scoreboard")
      r.raise_for_status()
      data = r.json()
    except Exception:
      return []

    now = datetime.now(timezone.utc).isoformat()
    out: list[SportsEvent] = []
    for ev in data.get("events") or []:
      status = str(((ev.get("status") or {}).get("type") or {}).get("name") or "").lower()
      if status in {"final", "postponed", "canceled", "cancelled"}:
        continue
      comps = (ev.get("competitions") or [{}])[0]
      competitors = comps.get("competitors") or []
      home = next((c for c in competitors if c.get("homeAway") == "home"), None)
      away = next((c for c in competitors if c.get("homeAway") == "away"), None)
      if not home or not away:
        continue
      home_name = (home.get("team") or {}).get("displayName") or "Home"
      away_name = (away.get("team") or {}).get("displayName") or "Away"
      event = SportsEvent(
        event_id=f"espn-{league_key}-{ev.get('id')}",
        sport=sport,
        league=league,
        home=home_name,
        away=away_name,
        commence_time=ev.get("date"),
      )
      for odd in comps.get("odds") or []:
        provider = ((odd.get("provider") or {}).get("name") or "ESPN").strip()
        ml = odd.get("moneyline") or {}
        home_am = self._parse_am(((ml.get("home") or {}).get("close") or {}).get("odds"))
        away_am = self._parse_am(((ml.get("away") or {}).get("close") or {}).get("odds"))
        if home_am is not None:
          dec = american_to_decimal(home_am)
          if dec:
            event.odds.append(
              BookOdds(
                provider, "espn", "home", home_am, dec,
                market="h2h", region="us", fetched_at=now,
              )
            )
        if away_am is not None:
          dec = american_to_decimal(away_am)
          if dec:
            event.odds.append(
              BookOdds(
                provider, "espn", "away", away_am, dec,
                market="h2h", region="us", fetched_at=now,
              )
            )
        # Spreads / totals
        ps = odd.get("pointSpread") or {}
        for side, key in (("home", "home"), ("away", "away")):
          block = ((ps.get(key) or {}).get("close")) or {}
          am = self._parse_am(block.get("odds"))
          line = self._parse_line(block.get("line"))
          if am is None:
            continue
          dec = american_to_decimal(am)
          if not dec:
            continue
          event.odds.append(
            BookOdds(
              provider, "espn", side, am, dec,
              line=line, market="spreads", region="us", fetched_at=now,
            )
          )
        tot = odd.get("total") or {}
        for side, key in (("over", "over"), ("under", "under")):
          block = ((tot.get(key) or {}).get("close")) or {}
          am = self._parse_am(block.get("odds"))
          line = self._parse_line(block.get("line"))
          if am is None:
            continue
          dec = american_to_decimal(am)
          if not dec:
            continue
          event.odds.append(
            BookOdds(
              provider, "espn", side, am, dec,
              line=line, market="totals", region="us", fetched_at=now,
            )
          )
      if event.odds:
        out.append(event)
    return out

  @staticmethod
  def _parse_am(raw) -> int | None:
    if raw is None:
      return None
    try:
      s = str(raw).replace("−", "-").strip()
      return int(float(s))
    except (TypeError, ValueError):
      return None

  @staticmethod
  def _parse_line(raw) -> float | None:
    if raw is None:
      return None
    try:
      s = str(raw).lower().replace("o", "").replace("u", "").replace("+", "").strip()
      return float(s)
    except (TypeError, ValueError):
      return None

  async def fetch_all(self, leagues: list[str] | None = None) -> list[SportsEvent]:
    keys = leagues or list(ESPN_PATHS.keys())
    events: list[SportsEvent] = []
    for key in keys:
      events.extend(await self.fetch_league(key))
    return events
