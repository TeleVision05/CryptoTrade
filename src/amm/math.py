from __future__ import annotations


def get_amount_out(
  amount_in: float,
  reserve_in: float,
  reserve_out: float,
  fee_bps: float = 30.0,
) -> float:
  """Uniswap V2 constant-product swap with fee in basis points."""
  if amount_in <= 0 or reserve_in <= 0 or reserve_out <= 0:
    return 0.0

  fee_multiplier = 10_000 - fee_bps
  amount_in_with_fee = amount_in * fee_multiplier
  numerator = amount_in_with_fee * reserve_out
  denominator = reserve_in * 10_000 + amount_in_with_fee
  return numerator / denominator


def estimate_reserves_from_liquidity(
  liquidity_usd: float,
  price_usd: float,
  base_is_token0: bool = True,
) -> tuple[float, float]:
  """
  Approximate pool reserves from TVL and price.
  For WETH/USDC with price in USD per WETH:
  reserve_weth * price + reserve_usdc ~= liquidity_usd (assuming balanced pool).
  """
  if liquidity_usd <= 0 or price_usd <= 0:
    return 0.0, 0.0

  half = liquidity_usd / 2
  reserve_base = half / price_usd
  reserve_quote = half
  if base_is_token0:
    return reserve_base, reserve_quote
  return reserve_quote, reserve_base


def quote_buy_base_with_quote(
  quote_amount: float,
  liquidity_usd: float,
  price_usd: float,
  fee_bps: float = 30.0,
) -> float:
  """Spend quote (USDC) to buy base (WETH)."""
  reserve_base, reserve_quote = estimate_reserves_from_liquidity(liquidity_usd, price_usd)
  return get_amount_out(quote_amount, reserve_quote, reserve_base, fee_bps)


def quote_sell_base_for_quote(
  base_amount: float,
  liquidity_usd: float,
  price_usd: float,
  fee_bps: float = 30.0,
) -> float:
  """Sell base (WETH) for quote (USDC)."""
  reserve_base, reserve_quote = estimate_reserves_from_liquidity(liquidity_usd, price_usd)
  return get_amount_out(base_amount, reserve_base, reserve_quote, fee_bps)


def price_impact_bps(trade_size_usd: float, liquidity_usd: float) -> float:
  """Linear price impact estimate from trade size relative to pool depth."""
  if liquidity_usd <= 0:
    return 100.0
  return min((trade_size_usd / liquidity_usd) * 5_000, 200.0)


def spot_round_trip_usd(
  trade_size_usd: float,
  buy_price_usd: float,
  sell_price_usd: float,
  buy_liquidity_usd: float,
  sell_liquidity_usd: float,
) -> float:
  """
  Simulate cross-DEX round trip using spot prices with liquidity-based impact.
  Returns USDC received after buying WETH and selling it.
  LP fees are applied separately by the cost model.
  """
  if trade_size_usd <= 0 or buy_price_usd <= 0 or sell_price_usd <= 0:
    return 0.0

  buy_impact = price_impact_bps(trade_size_usd, buy_liquidity_usd) / 10_000
  sell_impact = price_impact_bps(trade_size_usd, sell_liquidity_usd) / 10_000

  effective_buy_price = buy_price_usd * (1 + buy_impact)
  weth_amount = trade_size_usd / effective_buy_price

  effective_sell_price = sell_price_usd * (1 - sell_impact)
  return weth_amount * effective_sell_price
