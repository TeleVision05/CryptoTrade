from __future__ import annotations

from src.fees import infer_pool_fee_bps
from src.models import DexPair, Opportunity


def pair_fee_bps(pair: DexPair) -> float:
  return pair.fee_bps


def fee_drag_bps(buy_pair: DexPair, sell_pair: DexPair) -> float:
  return pair_fee_bps(buy_pair) + pair_fee_bps(sell_pair)


def fee_drag_bps_from_dexes(buy_dex: str, sell_dex: str) -> float:
  """Fallback when only DEX ids are known."""
  from src.quoting.pool_quotes import dex_fee_bps
  return dex_fee_bps(buy_dex) + dex_fee_bps(sell_dex)


def min_viable_spread_bps(config, buy_pair: DexPair, sell_pair: DexPair) -> float:
  overhead = config.spread_decay_bps + config.latency_penalty_bps
  return max(config.min_gross_spread_bps, fee_drag_bps(buy_pair, sell_pair) + overhead)
