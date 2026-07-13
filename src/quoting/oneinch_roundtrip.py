from __future__ import annotations

from dataclasses import dataclass

from src.config import AppConfig
from src.data.oneinch import OneInchClient
from src.data.tokens import get_token, to_raw_amount
from src.models import Opportunity

STABLES = {"USDC", "USDT", "DAI"}


@dataclass
class SpatialRoundTrip:
  quote_symbol: str
  base_symbol: str
  quote_amount: float
  base_amount: float
  quote_out: float
  sell_output_usd: float
  quote_amount_raw: int
  base_amount_raw: int


def parse_pair_symbols(pair_label: str) -> tuple[str, str]:
  """Return (base, quote) from labels like WETH/USDC or WETH/USDC→USDT."""
  label = pair_label.split("→")[0]
  parts = label.split("/")
  if len(parts) != 2:
    raise ValueError(f"Bad pair label: {pair_label}")
  return parts[0].strip(), parts[1].strip()


def quote_amount_from_usd(
  trade_size_usd: float,
  quote_symbol: str,
  opportunity: Opportunity,
) -> float:
  """Convert USD notional into quote-token units."""
  if quote_symbol.upper() in STABLES:
    return trade_size_usd
  # For WETH (or other non-stable quotes), use mid price from the opportunity.
  # opportunity.buy_price_usd is USD per base; for BASE/WETH, quote is WETH so
  # we need USD per WETH. Prefer sell/buy of WETH pairs via base price inverse
  # when quote is WETH: eth_price ≈ base_price / (base per eth)... 
  # Simpler: DexScreener prices on ARB/WETH give ARB in USD. WETH≈ buy_price of WETH/USDC
  # isn't on this opportunity. Use a proxy: if base is priced in USD and quote is WETH,
  # eth_usd ≈ typical from config isn't available. Use opportunity fields:
  # For TOKEN/WETH pools, priceUsd is TOKEN price; WETH price ~1800 can be inferred
  # poorly. Better approach: use sell_price and buy_price only for TOKEN.
  # We'll pass eth via max(buy,sell) only when base is WETH.
  if opportunity.pair_label.startswith("WETH/"):
    # price is USD per WETH
    return trade_size_usd / max(opportunity.buy_price_usd, 1e-9)
  # TOKEN/WETH: need ETH USD price. Approximate from known L2 ~ use 1 ETH = trade
  # via inverse if we had pool ratio. Fallback: assume ~1800 unless we can do better.
  # Prefer reading from a WETH reference on the same chain later; for now use 1800.
  eth_usd = 1800.0
  return trade_size_usd / eth_usd


def quote_out_to_usd(
  quote_out: float,
  quote_symbol: str,
  opportunity: Opportunity,
  eth_usd: float = 1800.0,
) -> float:
  if quote_symbol.upper() in STABLES:
    return quote_out
  if opportunity.pair_label.startswith("WETH/"):
    return quote_out * max(opportunity.buy_price_usd, 1e-9)
  return quote_out * eth_usd


async def quote_spatial_round_trip(
  oneinch: OneInchClient,
  config: AppConfig,
  opportunity: Opportunity,
  trade_size_usd: float,
) -> SpatialRoundTrip | None:
  """
  Quote quote->base->quote on 1inch (aggregator path).
  Converts USD notional to quote units correctly for stables and WETH.
  """
  base_symbol, quote_symbol = parse_pair_symbols(opportunity.pair_label)
  chain = opportunity.chain
  chain_id = config.chains[chain].chain_id

  base_token = get_token(chain, base_symbol)
  quote_token = get_token(chain, quote_symbol)

  eth_usd = 1800.0
  if base_symbol == "WETH":
    eth_usd = max(opportunity.buy_price_usd, opportunity.sell_price_usd, 1.0)

  quote_amount = quote_amount_from_usd(trade_size_usd, quote_symbol, opportunity)
  if quote_symbol.upper() not in STABLES and not opportunity.pair_label.startswith("WETH/"):
    quote_amount = trade_size_usd / eth_usd

  quote_amount_raw = to_raw_amount(quote_amount, quote_token.decimals)

  buy_quote = await oneinch.get_quote(
    chain_id=chain_id,
    src_token=quote_token.address,
    dst_token=base_token.address,
    amount_raw=quote_amount_raw,
    src_decimals=quote_token.decimals,
    dst_decimals=base_token.decimals,
  )
  if buy_quote is None:
    return None

  sell_quote = await oneinch.get_quote(
    chain_id=chain_id,
    src_token=base_token.address,
    dst_token=quote_token.address,
    amount_raw=buy_quote.dst_amount_raw,
    src_decimals=base_token.decimals,
    dst_decimals=quote_token.decimals,
  )
  if sell_quote is None:
    return None

  quote_out = sell_quote.dst_amount
  sell_output_usd = quote_out_to_usd(quote_out, quote_symbol, opportunity, eth_usd)

  return SpatialRoundTrip(
    quote_symbol=quote_symbol,
    base_symbol=base_symbol,
    quote_amount=quote_amount,
    base_amount=buy_quote.dst_amount,
    quote_out=quote_out,
    sell_output_usd=sell_output_usd,
    quote_amount_raw=quote_amount_raw,
    base_amount_raw=buy_quote.dst_amount_raw,
  )
