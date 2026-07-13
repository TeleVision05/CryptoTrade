from __future__ import annotations

from datetime import datetime, timezone

from src.sports.models import BookOdds, SportsEvent
from src.sports.odds_math import three_way_arb, two_way_arb
from src.sports.regions import infer_book_region, normalize_region
from src.sports.scanner import SportsArbScanner


def test_two_way_arb_sizes_stakes():
  out = two_way_arb(2.10, 2.10, 100.0)
  assert out is not None
  assert out["profit_pct"] > 0
  assert abs(out["stake_a"] + out["stake_b"] - 100.0) < 0.02


def test_three_way_needed_for_true_soccer_edge():
  # Fake 2-way on soccer looks huge; true 3-way with draw is not an arb.
  fake = two_way_arb(3.17, 2.50, 100.0)
  assert fake is not None and fake["profit_pct"] > 20
  real = three_way_arb([3.17, 2.50, 3.40], 100.0)
  assert real is None  # inv > 1 with typical draw


def test_region_hints():
  assert normalize_region("us2") == "us"
  assert infer_book_region("1xBet") == "eu"
  assert infer_book_region("William Hill") == "uk"
  assert infer_book_region("DraftKings") == "us"


def _odd(book, outcome, american, region="us", market="h2h", line=None):
  from src.sports.odds_math import american_to_decimal

  dec = american_to_decimal(american)
  assert dec is not None
  return BookOdds(
    book=book,
    source="test",
    outcome=outcome,
    american=american,
    decimal=dec,
    line=line,
    market=market,
    region=region,
    fetched_at=datetime.now(timezone.utc).isoformat(),
  )


def test_scanner_rejects_cross_region_and_soccer_two_way():
  scanner = SportsArbScanner(min_profit_pct=0.1, max_profit_pct=5.0)
  now = datetime.now(timezone.utc)

  # Cross-region home/away would look juicy but must not produce an arb.
  nba = SportsEvent(
    event_id="1",
    sport="basketball",
    league="NBA",
    home="Home",
    away="Away",
    commence_time=None,
    odds=[
      _odd("DraftKings", "home", -110, "us"),
      _odd("1xBet", "away", -105, "eu"),
    ],
  )
  found = scanner._arb_for_market(nba, "h2h", now)
  # Legs are region-split so each region is incomplete → no arb.
  assert all(item.get("arb") is None for item in found)

  # Soccer without draw must not invent a 2-way arb.
  epl = SportsEvent(
    event_id="2",
    sport="soccer",
    league="EPL",
    home="Brentford",
    away="Tottenham Hotspur",
    commence_time=None,
    odds=[
      _odd("William Hill", "home", 150, "uk"),
      _odd("PaddyPower", "away", 217, "uk"),
    ],
  )
  soccer_found = scanner._arb_for_market(epl, "h2h", now)
  assert soccer_found == []

  # Same-region NBA 2-way with a real tiny edge.
  tight = SportsEvent(
    event_id="3",
    sport="basketball",
    league="NBA",
    home="A",
    away="B",
    commence_time=None,
    odds=[
      _odd("DraftKings", "home", 105, "us"),
      _odd("FanDuel", "away", 105, "us"),
    ],
  )
  ok = scanner._arb_for_market(tight, "h2h", now)
  arbs = [i["arb"] for i in ok if i.get("arb")]
  assert len(arbs) == 1
  assert arbs[0].region == "us"
  assert arbs[0].n_way == 2
  assert arbs[0].legs[0].region == arbs[0].legs[1].region == "us"


def test_scanner_three_way_soccer_same_region():
  scanner = SportsArbScanner(min_profit_pct=0.1, max_profit_pct=8.0)
  now = datetime.now(timezone.utc)
  # Decimals 2.2, 2.2, 2.2 → inv = 1.363 — no arb
  # Need inv < 1: e.g. 3.2, 3.2, 3.2 → inv = 0.9375 → ~6.25%
  ev = SportsEvent(
    event_id="4",
    sport="soccer",
    league="EPL",
    home="Home",
    away="Away",
    commence_time=None,
    odds=[
      _odd("William Hill", "home", 220, "uk"),  # 3.2
      _odd("PaddyPower", "away", 220, "uk"),
      _odd("Sky Bet", "draw", 220, "uk"),
    ],
  )
  found = scanner._arb_for_market(ev, "h2h", now)
  arbs = [i["arb"] for i in found if i.get("arb")]
  assert len(arbs) == 1
  assert arbs[0].n_way == 3
  assert len(arbs[0].legs) == 3
  assert arbs[0].region == "uk"
