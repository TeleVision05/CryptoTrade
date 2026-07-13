from __future__ import annotations

from src.amm.math import price_impact_bps
from src.models import Opportunity


def spot_estimated_sell_output(
  opportunity: Opportunity,
  trade_size_usd: float,
) -> float:
  """
  Fast cross-pool estimate: spot spread minus per-pool fees and impact.
  More accurate than full AMM re-simulation when DexScreener prices are fresh.
  """
  if trade_size_usd <= 0:
    return 0.0

  buy_pair = opportunity.buy_pair
  sell_pair = opportunity.sell_pair
  impact_bps = price_impact_bps(trade_size_usd, buy_pair.liquidity_usd)
  impact_bps += price_impact_bps(trade_size_usd, sell_pair.liquidity_usd)
  fee_bps = buy_pair.fee_bps + sell_pair.fee_bps
  net_bps = opportunity.gross_spread_bps - fee_bps - impact_bps
  return trade_size_usd * (1 + net_bps / 10_000)


def effective_spread_bps(sell_output_usd: float, trade_size_usd: float) -> float:
  if trade_size_usd <= 0:
    return 0.0
  return ((sell_output_usd - trade_size_usd) / trade_size_usd) * 10_000
