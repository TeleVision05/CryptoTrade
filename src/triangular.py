from __future__ import annotations

import uuid
from datetime import datetime, timezone

from src.config import AppConfig
from src.data.dexscreener import DexScreenerClient
from src.data.tokens import get_token
from src.models import DexPair, Opportunity
from src.scanner import OpportunityScanner


class TriangularScanner:
  """USDC -> WETH -> USDT -> USDC path on a single chain."""

  def __init__(self, config: AppConfig, dexscreener: DexScreenerClient) -> None:
    self._config = config
    self._dexscreener = dexscreener
    self._spatial = OpportunityScanner(config, dexscreener)

  async def scan_all(self) -> list[Opportunity]:
    opportunities: list[Opportunity] = []
    for chain_name in self._config.chains:
      opp = await self._scan_chain(chain_name)
      if opp is not None:
        opportunities.append(opp)
    return opportunities

  async def _scan_chain(self, chain_name: str) -> Opportunity | None:
    chain_cfg = self._config.chains[chain_name]
    allowed = {dex.lower() for dex in chain_cfg.dexes}
    weth = get_token(chain_name, "WETH")
    all_pairs = await self._dexscreener.get_token_pairs(
      chain_cfg.dexscreener_chain,
      weth.address,
    )

    usdc_pools = self._spatial._filter_pairs(all_pairs, "WETH", "USDC", allowed)
    usdt_pools = self._spatial._filter_pairs(all_pairs, "WETH", "USDT", allowed)
    if not usdc_pools or not usdt_pools:
      return None

    buy_weth = min(usdc_pools, key=lambda p: p.price_usd)
    sell_weth = max(usdt_pools, key=lambda p: p.price_usd)
    if sell_weth.price_usd <= buy_weth.price_usd:
      return None

    gross_spread_bps = ((sell_weth.price_usd - buy_weth.price_usd) / buy_weth.price_usd) * 10_000
    total_fees = buy_weth.fee_bps + sell_weth.fee_bps + 5.0
    required = max(
      self._config.min_gross_spread_bps,
      total_fees + self._config.spread_decay_bps + self._config.latency_penalty_bps,
    )
    if gross_spread_bps < required:
      return None

    return Opportunity(
      id=str(uuid.uuid4()),
      timestamp=datetime.now(timezone.utc),
      chain=chain_name,
      pair_label="WETH/USDC→USDT",
      buy_dex=f"{buy_weth.dex_id}({buy_weth.fee_bps:.0f}bp)",
      sell_dex=f"{sell_weth.dex_id}({sell_weth.fee_bps:.0f}bp)",
      buy_price_usd=buy_weth.price_usd,
      sell_price_usd=sell_weth.price_usd,
      gross_spread_bps=gross_spread_bps,
      buy_pair=buy_weth,
      sell_pair=sell_weth,
      strategy="triangular",
    )
