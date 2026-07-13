from __future__ import annotations

from src.amm.math import price_impact_bps, quote_buy_base_with_quote, quote_sell_base_for_quote
from src.models import Opportunity
from src.quoting.pool_quotes import PoolRoundTripQuote, apply_spread_decay

STABLE_SWAP_FEE_BPS = 5.0


def triangular_round_trip_usd(
  opportunity: Opportunity,
  trade_size_usd: float,
) -> PoolRoundTripQuote | None:
  """
  USDC -> WETH (buy pool) -> USDT (sell pool) -> USDC (stable swap).
  """
  if trade_size_usd <= 0:
    return None

  buy_pair = opportunity.buy_pair
  sell_pair = opportunity.sell_pair

  weth_amount = quote_buy_base_with_quote(
    quote_amount=trade_size_usd,
    liquidity_usd=buy_pair.liquidity_usd,
    price_usd=buy_pair.price_usd,
    fee_bps=buy_pair.fee_bps,
  )
  if weth_amount <= 0:
    return None

  usdt_out = quote_sell_base_for_quote(
    base_amount=weth_amount,
    liquidity_usd=sell_pair.liquidity_usd,
    price_usd=sell_pair.price_usd,
    fee_bps=sell_pair.fee_bps,
  )
  if usdt_out <= 0:
    return None

  usdc_out = usdt_out * (1 - STABLE_SWAP_FEE_BPS / 10_000)
  buy_impact = price_impact_bps(trade_size_usd, buy_pair.liquidity_usd)
  sell_impact = price_impact_bps(trade_size_usd, sell_pair.liquidity_usd)
  effective_spread_bps = ((usdc_out - trade_size_usd) / trade_size_usd) * 10_000

  return PoolRoundTripQuote(
    trade_size_usd=trade_size_usd,
    weth_amount=weth_amount,
    sell_output_usd=usdc_out,
    buy_impact_bps=buy_impact,
    sell_impact_bps=sell_impact,
    effective_spread_bps=effective_spread_bps,
    quote_source="triangular",
  )
