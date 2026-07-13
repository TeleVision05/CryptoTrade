from __future__ import annotations

from dataclasses import dataclass

from src.amm.math import (
  price_impact_bps,
  quote_buy_base_with_quote,
  quote_sell_base_for_quote,
)
from src.models import DexPair, Opportunity
from src.quoting.fees import pair_fee_bps


@dataclass
class PoolRoundTripQuote:
  trade_size_usd: float
  weth_amount: float
  sell_output_usd: float
  buy_impact_bps: float
  sell_impact_bps: float
  effective_spread_bps: float
  quote_source: str = "pool_amm"


def pool_round_trip_usd(
  opportunity: Opportunity,
  trade_size_usd: float,
) -> PoolRoundTripQuote | None:
  if trade_size_usd <= 0:
    return None

  buy_pair = opportunity.buy_pair
  sell_pair = opportunity.sell_pair

  weth_amount = quote_buy_base_with_quote(
    quote_amount=trade_size_usd,
    liquidity_usd=buy_pair.liquidity_usd,
    price_usd=buy_pair.price_usd,
    fee_bps=pair_fee_bps(buy_pair),
  )
  if weth_amount <= 0:
    return None

  sell_output_usd = quote_sell_base_for_quote(
    base_amount=weth_amount,
    liquidity_usd=sell_pair.liquidity_usd,
    price_usd=sell_pair.price_usd,
    fee_bps=pair_fee_bps(sell_pair),
  )
  if sell_output_usd <= 0:
    return None

  buy_impact = price_impact_bps(trade_size_usd, buy_pair.liquidity_usd)
  sell_impact = price_impact_bps(trade_size_usd, sell_pair.liquidity_usd)
  effective_spread_bps = ((sell_output_usd - trade_size_usd) / trade_size_usd) * 10_000

  return PoolRoundTripQuote(
    trade_size_usd=trade_size_usd,
    weth_amount=weth_amount,
    sell_output_usd=sell_output_usd,
    buy_impact_bps=buy_impact,
    sell_impact_bps=sell_impact,
    effective_spread_bps=effective_spread_bps,
  )


def apply_spread_decay(
  sell_output_usd: float,
  trade_size_usd: float,
  decay_bps: float,
) -> float:
  if decay_bps <= 0 or trade_size_usd <= 0:
    return sell_output_usd
  haircut = trade_size_usd * (decay_bps / 10_000)
  return max(0.0, sell_output_usd - haircut)
