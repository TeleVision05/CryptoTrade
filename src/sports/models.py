from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone


@dataclass
class BookOdds:
  book: str
  source: str
  outcome: str  # home | away | draw | over | under
  american: int
  decimal: float
  line: float | None = None  # spread/total point when relevant
  market: str = "h2h"  # h2h | spreads | totals
  url: str | None = None
  region: str = "us"
  fetched_at: str = field(
    default_factory=lambda: datetime.now(timezone.utc).isoformat()
  )


@dataclass
class SportsEvent:
  event_id: str
  sport: str
  league: str
  home: str
  away: str
  commence_time: str | None
  odds: list[BookOdds] = field(default_factory=list)


@dataclass
class ArbLeg:
  outcome: str
  book: str
  source: str
  american: int
  decimal: float
  stake_usd: float
  line: float | None = None
  market: str = "h2h"
  region: str = "us"
  odds_age_sec: float | None = None


@dataclass
class SportsArb:
  id: str
  sport: str
  league: str
  event: str
  market: str
  profit_pct: float
  profit_usd: float
  stake_total: float
  payout: float
  commence_time: str | None
  legs: list[ArbLeg]
  found_at: str = field(
    default_factory=lambda: datetime.now(timezone.utc).isoformat()
  )
  sources: list[str] = field(default_factory=list)
  region: str = "us"
  n_way: int = 2
  max_odds_age_sec: float | None = None
  actionable: bool = True

  def to_dict(self) -> dict:
    return asdict(self)
