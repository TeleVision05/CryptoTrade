from __future__ import annotations

# Inferred LP fee tiers in basis points (fallback only — prefer on-chain fee()).
DEFAULT_FEE_BPS = 30.0
V3_LOW_FEE_BPS = 5.0
V3_MID_FEE_BPS = 30.0


def infer_pool_fee_bps(
  dex_id: str,
  labels: list[str],
  liquidity_usd: float,
  base_symbol: str,
  quote_symbol: str,
) -> float:
  """
  Infer swap fee from DEX labels when on-chain fee() is unavailable.
  Do NOT guess 1bps from depth — the deepest Base WETH/USDC Uniswap pool is 30bps.
  """
  dex = dex_id.lower()
  label_set = {label.lower() for label in labels}
  majors = {"WETH", "USDC", "USDT", "DAI", "WBTC"}
  is_major = base_symbol.upper() in majors or quote_symbol.upper() in majors

  # Explicit fee labels from DexScreener when present (e.g. "0.01%", "1", "100").
  for label in label_set:
    if label in {"0.01%", "1bps", "1"}:
      return 1.0
    if label in {"0.05%", "5bps", "5"}:
      return 5.0
    if label in {"0.3%", "30bps", "30"}:
      return 30.0
    if label in {"1%", "100bps", "100"}:
      return 100.0

  if dex == "uniswap" and "v3" in label_set:
    if is_major and liquidity_usd >= 1_000_000:
      return V3_LOW_FEE_BPS
    return V3_MID_FEE_BPS

  if dex == "uniswap" and is_major and liquidity_usd >= 5_000_000:
    return V3_LOW_FEE_BPS

  if dex in {"sushiswap", "pancakeswap"} and "v3" in label_set:
    if liquidity_usd >= 500_000:
      return V3_LOW_FEE_BPS
    return V3_MID_FEE_BPS

  if dex == "aerodrome" and is_major:
    if liquidity_usd >= 5_000_000:
      return 10.0
    if liquidity_usd >= 1_000_000:
      return 15.0
    return 20.0

  if dex in {"alien-base", "baseswap", "camelot"} and is_major and liquidity_usd >= 100_000:
    return 25.0

  if dex == "quickswap" and "v4" in label_set:
    return V3_LOW_FEE_BPS

  return DEFAULT_FEE_BPS
