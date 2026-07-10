from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from src.config import AppConfig
from src.data.dexscreener import DexScreenerClient
from src.data.tokens import get_token
from src.models import DexPair, Opportunity


@dataclass
class MarketSpread:
  chain: str
  pair_label: str
  buy_dex: str
  sell_dex: str
  buy_price_usd: float
  sell_price_usd: float
  gross_spread_bps: float
  pool_count: int


class OpportunityScanner:
  def __init__(self, config: AppConfig, dexscreener: DexScreenerClient) -> None:
    self._config = config
    self._dexscreener = dexscreener

  async def scan_all(self) -> list[Opportunity]:
    opportunities: list[Opportunity] = []
    for chain_name in self._config.chains:
      chain_opps = await self.scan_chain(chain_name)
      opportunities.extend(chain_opps)
    return opportunities

  async def scan_market_summary(self) -> list[MarketSpread]:
    """Return best spreads per chain/pair, even if below the trade threshold."""
    summaries: list[MarketSpread] = []
    for chain_name in self._config.chains:
      chain_cfg = self._config.chains[chain_name]
      allowed_dexes = {dex.lower() for dex in chain_cfg.dexes}
      base_token = get_token(chain_name, self._config.pairs[0].base)
      all_pairs = await self._dexscreener.get_token_pairs(
        chain_cfg.dexscreener_chain,
        base_token.address,
      )
      for pair_cfg in self._config.pairs:
        for quote_symbol in pair_cfg.quotes:
          filtered = self._filter_pairs(
            all_pairs,
            base_symbol=pair_cfg.base,
            quote_symbol=quote_symbol,
            allowed_dexes=allowed_dexes,
          )
          if len(filtered) < 2:
            continue
          buy_pair = min(filtered, key=lambda p: p.price_usd)
          sell_pair = max(filtered, key=lambda p: p.price_usd)
          if buy_pair.pair_address == sell_pair.pair_address:
            continue
          spread_bps = (
            (sell_pair.price_usd - buy_pair.price_usd) / buy_pair.price_usd
          ) * 10_000
          summaries.append(
            MarketSpread(
              chain=chain_name,
              pair_label=f"{pair_cfg.base}/{quote_symbol}",
              buy_dex=buy_pair.dex_id,
              sell_dex=sell_pair.dex_id,
              buy_price_usd=buy_pair.price_usd,
              sell_price_usd=sell_pair.price_usd,
              gross_spread_bps=spread_bps,
              pool_count=len(filtered),
            )
          )
    return summaries

  async def scan_chain(self, chain_name: str) -> list[Opportunity]:
    chain_cfg = self._config.chains[chain_name]
    dexscreener_chain = chain_cfg.dexscreener_chain
    allowed_dexes = {dex.lower() for dex in chain_cfg.dexes}
    opportunities: list[Opportunity] = []

    for pair_cfg in self._config.pairs:
      base_token = get_token(chain_name, pair_cfg.base)
      all_pairs = await self._dexscreener.get_token_pairs(
        dexscreener_chain,
        base_token.address,
      )
      for quote_symbol in pair_cfg.quotes:
        filtered = self._filter_pairs(
          all_pairs,
          base_symbol=pair_cfg.base,
          quote_symbol=quote_symbol,
          allowed_dexes=allowed_dexes,
        )
        if len(filtered) < 2:
          continue
        opportunity = self._best_spread(chain_name, pair_cfg.base, quote_symbol, filtered)
        if opportunity is not None:
          opportunities.append(opportunity)

    return opportunities

  @staticmethod
  def _normalize_symbol(symbol: str) -> str:
    upper = symbol.upper().strip()
    mappings = {
      "USD₮0": "USDT",
      "USD₮": "USDT",
      "USDT0": "USDT",
      "USDC.E": "USDC",
      "USDBC": "USDC",
      "WETH": "WETH",
      "USDC": "USDC",
      "USDT": "USDT",
    }
    if upper in mappings:
      return mappings[upper]
    normalized = upper.replace("₮", "T")
    if normalized.endswith("T0") and "USD" in normalized:
      return "USDT"
    return normalized

  def _filter_pairs(
    self,
    pairs: list[DexPair],
    base_symbol: str,
    quote_symbol: str,
    allowed_dexes: set[str],
  ) -> list[DexPair]:
    target_base = base_symbol.upper()
    target_quote = quote_symbol.upper()
    best_per_dex: dict[str, DexPair] = {}

    for pair in pairs:
      if self._normalize_symbol(pair.base_symbol) != target_base:
        continue
      if self._normalize_symbol(pair.quote_symbol) != target_quote:
        continue
      if pair.dex_id not in allowed_dexes:
        continue
      if pair.liquidity_usd < self._config.min_liquidity_usd:
        continue
      if pair.volume_24h_usd < self._config.min_volume_24h_usd:
        continue
      existing = best_per_dex.get(pair.dex_id)
      if existing is None or pair.liquidity_usd > existing.liquidity_usd:
        best_per_dex[pair.dex_id] = pair

    return list(best_per_dex.values())

  def _best_spread(
    self,
    chain_name: str,
    base_symbol: str,
    quote_symbol: str,
    pairs: list[DexPair],
  ) -> Opportunity | None:
    buy_pair = min(pairs, key=lambda p: p.price_usd)
    sell_pair = max(pairs, key=lambda p: p.price_usd)
    if buy_pair.pair_address == sell_pair.pair_address:
      return None

    gross_spread_bps = ((sell_pair.price_usd - buy_pair.price_usd) / buy_pair.price_usd) * 10_000
    if gross_spread_bps < self._config.min_gross_spread_bps:
      return None

    return Opportunity(
      id=str(uuid.uuid4()),
      timestamp=datetime.now(timezone.utc),
      chain=chain_name,
      pair_label=f"{base_symbol}/{quote_symbol}",
      buy_dex=buy_pair.dex_id,
      sell_dex=sell_pair.dex_id,
      buy_price_usd=buy_pair.price_usd,
      sell_price_usd=sell_pair.price_usd,
      gross_spread_bps=gross_spread_bps,
      buy_pair=buy_pair,
      sell_pair=sell_pair,
    )
