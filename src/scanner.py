from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import combinations

from src.config import AppConfig
from src.data.dexscreener import DexScreenerClient
from src.data.tokens import get_token, has_token
from src.models import DexPair, Opportunity
from src.quoting.fees import min_viable_spread_bps
from src.quoting.onchain_quoter import QUOTER_COMPATIBLE_DEXES, OnchainPoolQuoter
from src.quoting.pool_fee_enricher import PoolFeeEnricher
from src.quoting.pool_quotes import pool_round_trip_usd
from src.quoting.spot_estimator import effective_spread_bps, spot_estimated_sell_output


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
  buy_fee_bps: float = 30.0
  sell_fee_bps: float = 30.0
  required_bps: float = 67.0


class OpportunityScanner:
  def __init__(
    self,
    config: AppConfig,
    dexscreener: DexScreenerClient,
    fee_enricher: PoolFeeEnricher | None = None,
    onchain_quoter: OnchainPoolQuoter | None = None,
  ) -> None:
    self._config = config
    self._dexscreener = dexscreener
    self._fee_enricher = fee_enricher
    self._onchain = onchain_quoter
    # Cached healthy pools: (chain, base, quote) -> (monotonic_ts, pools)
    self._healthy_cache: dict[tuple[str, str, str], tuple[float, list[DexPair]]] = {}
    self._hot_routes: list[Opportunity] = []
    self._last_full_hunt_at = 0.0

  @property
  def _prefer_onchain_routes(self) -> bool:
    return (
      self._config.execution_mode == "simulate"
      and self._config.simulate_quote_mode == "onchain"
    )

  async def scan_all(self) -> list[Opportunity]:
    opportunities: list[Opportunity] = []
    for chain_name in self._config.chains:
      chain_opps = await self.scan_chain(chain_name)
      opportunities.extend(chain_opps)
    return sorted(opportunities, key=lambda o: -o.gross_spread_bps)

  async def scan_market_summary(self) -> list[MarketSpread]:
    summaries: list[MarketSpread] = []
    for chain_name in self._config.chains:
      chain_cfg = self._config.chains[chain_name]
      allowed_dexes = {dex.lower() for dex in chain_cfg.dexes}
      for pair_cfg in self._config.pairs:
        if not has_token(chain_name, pair_cfg.base):
          continue
        base_token = get_token(chain_name, pair_cfg.base)
        all_pairs = await self._dexscreener.get_token_pairs(
          chain_cfg.dexscreener_chain,
          base_token.address,
        )
        for quote_symbol in pair_cfg.quotes:
          if not has_token(chain_name, quote_symbol):
            continue
          filtered = self._filter_pairs(
            all_pairs,
            base_symbol=pair_cfg.base,
            quote_symbol=quote_symbol,
            allowed_dexes=allowed_dexes,
          )
          if self._fee_enricher is not None and filtered:
            filtered = await self._fee_enricher.enrich_pairs(chain_name, filtered)
          if len(filtered) < 2:
            continue
          buy_pair, sell_pair, spread_bps = self._best_pool_pair(filtered, reference_size_usd=100.0)
          if buy_pair is None or sell_pair is None:
            continue
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
              buy_fee_bps=buy_pair.fee_bps,
              sell_fee_bps=sell_pair.fee_bps,
              required_bps=min_viable_spread_bps(self._config, buy_pair, sell_pair),
            )
          )
    return summaries

  async def scan_chain(self, chain_name: str) -> list[Opportunity]:
    chain_cfg = self._config.chains[chain_name]
    dexscreener_chain = chain_cfg.dexscreener_chain
    allowed_dexes = {dex.lower() for dex in chain_cfg.dexes}
    opportunities: list[Opportunity] = []

    for pair_cfg in self._config.pairs:
      if not has_token(chain_name, pair_cfg.base):
        continue
      base_token = get_token(chain_name, pair_cfg.base)
      all_pairs = await self._dexscreener.get_token_pairs(
        dexscreener_chain,
        base_token.address,
      )
      for quote_symbol in pair_cfg.quotes:
        if not has_token(chain_name, quote_symbol):
          continue
        filtered = self._filter_pairs(
          all_pairs,
          base_symbol=pair_cfg.base,
          quote_symbol=quote_symbol,
          allowed_dexes=allowed_dexes,
        )
        if self._fee_enricher is not None and filtered:
          filtered = await self._fee_enricher.enrich_pairs(chain_name, filtered)
        if self._prefer_onchain_routes:
          filtered = await self._healthy_pools(
            chain_name, pair_cfg.base, quote_symbol, filtered
          )
        if len(filtered) < 2:
          continue
        for opportunity in self._build_opportunities(
          chain_name, pair_cfg.base, quote_symbol, filtered
        ):
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
      "DAI": "DAI",
      "VIRTUAL": "VIRTUAL",
      "AERO": "AERO",
      "BRETT": "BRETT",
      "DEGEN": "DEGEN",
      "TOSHI": "TOSHI",
      "CBBTC": "cbBTC",
      "ARB": "ARB",
      "GMX": "GMX",
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
    filtered: list[DexPair] = []

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
      if self._prefer_onchain_routes:
        labels = {label.lower() for label in pair.labels}
        if pair.dex_id.lower() in {"uniswap", "pancakeswap"} and "v2" in labels and "v3" not in labels:
          continue
        if pair.chain == "arbitrum" and pair.dex_id.lower() not in {"uniswap", "pancakeswap"}:
          continue
        if pair.dex_id.lower() not in QUOTER_COMPATIBLE_DEXES:
          continue
      filtered.append(pair)

    return filtered

  async def _healthy_pools(
    self,
    chain_name: str,
    base_symbol: str,
    quote_symbol: str,
    pools: list[DexPair],
    cache_ttl_sec: float = 90.0,
  ) -> list[DexPair]:
    if self._onchain is None or not self._prefer_onchain_routes:
      return pools

    import time

    cache_key = (chain_name, base_symbol.upper(), quote_symbol.upper())
    cached = self._healthy_cache.get(cache_key)
    if cached and time.monotonic() - cached[0] < cache_ttl_sec:
      # Refresh slot0 mids only (cheap) on cached healthy set.
      refreshed: list[DexPair] = []
      for pair in cached[1]:
        mid = self._onchain.read_mid_price_usd(
          chain_name, pair, base_symbol, quote_symbol
        )
        if mid and mid > 0:
          refreshed.append(pair.model_copy(update={"price_usd": mid}))
        else:
          refreshed.append(pair)
      return refreshed

    def _check(pair: DexPair) -> DexPair | None:
      if not self._onchain.supports_pair(pair):
        return None
      if not self._onchain.pool_holds_registry_tokens(
        chain_name, pair, base_symbol, quote_symbol
      ):
        return None
      if not self._onchain.is_pool_healthy(
        chain_name, pair, base_symbol, quote_symbol
      ):
        return None
      mid = self._onchain.read_mid_price_usd(
        chain_name, pair, base_symbol, quote_symbol
      )
      if mid and mid > 0:
        return pair.model_copy(update={"price_usd": mid})
      return pair

    out: list[DexPair] = []
    for pair in pools:
      checked = await asyncio.to_thread(_check, pair)
      if checked is not None:
        out.append(checked)
    self._healthy_cache[cache_key] = (time.monotonic(), out)
    return out

  async def requote_hot_routes(
    self, probe_usd: float = 25.0
  ) -> list[Opportunity]:
    """Fast path: re-quote previously near-miss routes without DexScreener."""
    if not self._hot_routes or self._onchain is None:
      return []
    jobs = [(opp, probe_usd) for opp in self._hot_routes[:12]]
    # Also probe $10 — lower impact can flip a -2bps miss.
    jobs.extend((opp, 10.0) for opp in self._hot_routes[:8])
    results = await self._onchain.quote_round_trips_parallel(jobs)
    scored: list[tuple[float, Opportunity]] = []
    for (opp, size), rt in zip(jobs, results):
      if rt is None or rt.sell_output_usd < size * 0.90:
        continue
      if rt.effective_spread_bps < -2.5:
        continue
      refreshed = opp.model_copy(update={"gross_spread_bps": rt.effective_spread_bps})
      scored.append((rt.effective_spread_bps, refreshed))
    scored.sort(key=lambda row: row[0], reverse=True)
    # Deduplicate by route, keep best size.
    seen: set[tuple[str, str]] = set()
    out: list[Opportunity] = []
    for bps, opp in scored:
      key = (
        opp.buy_pair.pair_address.lower(),
        opp.sell_pair.pair_address.lower(),
      )
      if key in seen:
        continue
      seen.add(key)
      out.append(opp)
    return out[:6]

  def _best_pool_pair(
    self,
    pairs: list[DexPair],
    reference_size_usd: float = 100.0,
    require_quoter_compatible: bool = False,
  ) -> tuple[DexPair | None, DexPair | None, float]:
    from src.quoting.onchain_quoter import QUOTER_COMPATIBLE_DEXES

    best_buy: DexPair | None = None
    best_sell: DexPair | None = None
    best_spread = 0.0
    best_score = float("-inf")

    for left, right in combinations(pairs, 2):
      if left.pair_address == right.pair_address:
        continue
      if left.price_usd == right.price_usd:
        continue
      if require_quoter_compatible:
        if (
          left.dex_id.lower() not in QUOTER_COMPATIBLE_DEXES
          or right.dex_id.lower() not in QUOTER_COMPATIBLE_DEXES
        ):
          continue
      # Orient buy=cheaper / sell=dearer regardless of DexScreener list order.
      buy_pair, sell_pair = (
        (left, right) if left.price_usd < right.price_usd else (right, left)
      )
      spread_bps = ((sell_pair.price_usd - buy_pair.price_usd) / buy_pair.price_usd) * 10_000
      fee_drag = buy_pair.fee_bps + sell_pair.fee_bps
      net_edge = spread_bps - fee_drag
      effective_bps = self._effective_spread_bps(
        buy_pair, sell_pair, reference_size_usd, spread_bps
      )
      # Rank by executable pool-AMM edge first; spot-fee is a tie-break only.
      score = effective_bps * 1000.0 + net_edge
      if score > best_score:
        best_score = score
        best_spread = spread_bps
        best_buy = buy_pair
        best_sell = sell_pair

    return best_buy, best_sell, best_spread

  @staticmethod
  def _effective_spread_bps(
    buy_pair: DexPair,
    sell_pair: DexPair,
    trade_size_usd: float,
    spot_spread_bps: float,
  ) -> float:
    probe = Opportunity(
      id="probe",
      timestamp=datetime.now(timezone.utc),
      chain=buy_pair.chain,
      pair_label="probe",
      buy_dex=buy_pair.dex_id,
      sell_dex=sell_pair.dex_id,
      buy_price_usd=buy_pair.price_usd,
      sell_price_usd=sell_pair.price_usd,
      gross_spread_bps=spot_spread_bps,
      buy_pair=buy_pair,
      sell_pair=sell_pair,
    )
    quote = pool_round_trip_usd(probe, trade_size_usd)
    spot_out = spot_estimated_sell_output(probe, trade_size_usd)
    if quote is None and spot_out <= trade_size_usd:
      return float("-inf")
    if quote is None:
      return effective_spread_bps(spot_out, trade_size_usd)
    return max(quote.effective_spread_bps, effective_spread_bps(spot_out, trade_size_usd))

  def _pair_is_viable(
    self,
    buy_pair: DexPair,
    sell_pair: DexPair,
    gross_spread_bps: float,
    ref_size: float,
  ) -> bool:
    # Thin sell-side pools often show stale DexScreener mids (phantom arb).
    min_side = max(self._config.min_liquidity_usd, ref_size * 20)
    if buy_pair.liquidity_usd < min_side or sell_pair.liquidity_usd < min_side:
      return False
    # Avoid pairing a deep pool against a much thinner one — price feed drift.
    # Keep soft for onchain mode: QuoterV2 already prices impact.
    deeper = max(buy_pair.liquidity_usd, sell_pair.liquidity_usd)
    thinner = min(buy_pair.liquidity_usd, sell_pair.liquidity_usd)
    min_ratio = 0.02 if self._prefer_onchain_routes else 0.05
    if deeper > 0 and thinner / deeper < min_ratio:
      return False
    if self._prefer_onchain_routes and self._onchain is not None:
      if not self._onchain.supports_pair(buy_pair) or not self._onchain.supports_pair(
        sell_pair
      ):
        return False
      # DexScreener mids are often wrong; for onchain mode we still surface
      # low-fee routes and let QuoterV2 decide. Only drop obvious fee death.
      fee_drag = buy_pair.fee_bps + sell_pair.fee_bps
      if fee_drag > 40:
        return False
      return True
    fee_drag = buy_pair.fee_bps + sell_pair.fee_bps
    if gross_spread_bps - fee_drag < self._config.min_gross_spread_bps:
      return False
    return self._effective_spread_bps(buy_pair, sell_pair, ref_size, gross_spread_bps) >= 0

  def _build_opportunities(
    self,
    chain_name: str,
    base_symbol: str,
    quote_symbol: str,
    pairs: list[DexPair],
    limit: int = 5,
  ) -> list[Opportunity]:
    """Return up to `limit` buy/sell pool routes, best first (both directions)."""
    ref_size = self._config.starting_capital_usd / max(len(self._config.chains), 1) * 0.2
    ranked: list[tuple[float, DexPair, DexPair, float]] = []
    for left, right in combinations(pairs, 2):
      if left.pair_address.lower() == right.pair_address.lower():
        continue
      # Try both directions — DexScreener mid can be inverted vs executable price.
      for buy_pair, sell_pair in ((left, right), (right, left)):
        if buy_pair.price_usd <= 0:
          continue
        spread_bps = (
          (sell_pair.price_usd - buy_pair.price_usd) / buy_pair.price_usd
        ) * 10_000
        if not self._pair_is_viable(buy_pair, sell_pair, spread_bps, ref_size):
          continue
        if self._prefer_onchain_routes:
          # Prefer low fee-drag routes; spot is only a weak signal.
          fee_drag = buy_pair.fee_bps + sell_pair.fee_bps
          score = -fee_drag * 10.0 + min(spread_bps, 50.0)
        else:
          score = self._effective_spread_bps(buy_pair, sell_pair, ref_size, spread_bps)
        ranked.append((score, buy_pair, sell_pair, spread_bps))
    ranked.sort(key=lambda row: row[0], reverse=True)

    opps: list[Opportunity] = []
    seen: set[tuple[str, str]] = set()
    for _score, buy_pair, sell_pair, gross_spread_bps in ranked:
      key = (buy_pair.pair_address.lower(), sell_pair.pair_address.lower())
      if key in seen:
        continue
      seen.add(key)
      opps.append(
        Opportunity(
          id=str(uuid.uuid4()),
          timestamp=datetime.now(timezone.utc),
          chain=chain_name,
          pair_label=f"{base_symbol}/{quote_symbol}",
          buy_dex=f"{buy_pair.dex_id}({buy_pair.fee_bps:.0f}bp)",
          sell_dex=f"{sell_pair.dex_id}({sell_pair.fee_bps:.0f}bp)",
          buy_price_usd=buy_pair.price_usd,
          sell_price_usd=sell_pair.price_usd,
          gross_spread_bps=gross_spread_bps,
          buy_pair=buy_pair,
          sell_pair=sell_pair,
          strategy="spatial",
        )
      )
      if len(opps) >= limit:
        break
    return opps

  async def scan_tight_fee_routes(self) -> list[Opportunity]:
    """Low-fee Uni/Aero/PCS WETH/USDC routes both directions — ranked by on-chain mid."""
    if not self._prefer_onchain_routes:
      return []
    opps: list[Opportunity] = []
    for chain_name, chain_cfg in self._config.chains.items():
      if not has_token(chain_name, "WETH") or not has_token(chain_name, "USDC"):
        continue
      allowed = {dex.lower() for dex in chain_cfg.dexes} & QUOTER_COMPATIBLE_DEXES
      if not allowed:
        continue
      weth = get_token(chain_name, "WETH")
      all_pairs = await self._dexscreener.get_token_pairs(
        chain_cfg.dexscreener_chain, weth.address
      )
      filtered = self._filter_pairs(all_pairs, "WETH", "USDC", allowed)
      if self._fee_enricher is not None and filtered:
        filtered = await self._fee_enricher.enrich_pairs(chain_name, filtered)
      pools = [
        p
        for p in filtered
        if p.fee_bps <= 5.5
        and p.liquidity_usd >= max(self._config.min_liquidity_usd * 0.5, 50_000)
        and p.volume_24h_usd >= self._config.min_volume_24h_usd * 0.5
      ]
      pools = await self._healthy_pools(chain_name, "WETH", "USDC", pools)
      pools = sorted(pools, key=lambda p: (p.fee_bps, -p.liquidity_usd))[:6]
      for left, right in combinations(pools, 2):
        for buy, sell in ((left, right), (right, left)):
          fee_drag = buy.fee_bps + sell.fee_bps
          if fee_drag > 12:
            continue
          spread_bps = (
            (sell.price_usd - buy.price_usd) / max(buy.price_usd, 1e-12)
          ) * 10_000
          # Rank by on-chain mid edge after fees (DexScreener mids lie).
          score = spread_bps - fee_drag
          opps.append(
            (
              score,
              Opportunity(
                id=str(uuid.uuid4()),
                timestamp=datetime.now(timezone.utc),
                chain=chain_name,
                pair_label="WETH/USDC",
                buy_dex=f"{buy.dex_id}({buy.fee_bps:.1f}bp)",
                sell_dex=f"{sell.dex_id}({sell.fee_bps:.1f}bp)",
                buy_price_usd=buy.price_usd,
                sell_price_usd=sell.price_usd,
                gross_spread_bps=spread_bps,
                buy_pair=buy,
                sell_pair=sell,
                strategy="spatial",
              ),
            )
          )
    opps.sort(key=lambda row: row[0], reverse=True)
    return [opp for _score, opp in opps[:16]]

  async def scan_executable_edges(self, probe_usd: float = 25.0) -> list[Opportunity]:
    """
    Quote-first hunter: probe healthy low-fee routes on-chain and keep only
    near-break-even or better (≥ -3bps). This catches fleeting edge DexScreener misses.
    """
    if not self._prefer_onchain_routes or self._onchain is None:
      return []

    candidates = await self.scan_tight_fee_routes()

    # High-vol Base alts: only low fee-drag pairs with on-chain mid dislocation.
    alt_specs = [
      ("base", "USDC", "USDT"),
      ("base", "VIRTUAL", "WETH"),
      ("base", "VIRTUAL", "USDC"),
      ("base", "AERO", "USDC"),
      ("base", "cbBTC", "USDC"),
      ("base", "BRETT", "WETH"),
      ("base", "DEGEN", "WETH"),
    ]
    for chain_name, base, quote in alt_specs:
      if chain_name not in self._config.chains:
        continue
      if not has_token(chain_name, base) or not has_token(chain_name, quote):
        continue
      chain_cfg = self._config.chains[chain_name]
      allowed = {dex.lower() for dex in chain_cfg.dexes} & QUOTER_COMPATIBLE_DEXES
      token = get_token(chain_name, base)
      all_pairs = await self._dexscreener.get_token_pairs(
        chain_cfg.dexscreener_chain, token.address
      )
      # Slightly looser liquidity for alts — edge lives in thinner books.
      filtered: list[DexPair] = []
      for pair in all_pairs:
        if self._normalize_symbol(pair.base_symbol) != base.upper():
          continue
        if self._normalize_symbol(pair.quote_symbol) != quote.upper():
          continue
        if pair.dex_id.lower() not in allowed:
          continue
        if pair.liquidity_usd < max(40_000, self._config.min_liquidity_usd * 0.4):
          continue
        if pair.volume_24h_usd < max(3_000, self._config.min_volume_24h_usd * 0.3):
          continue
        filtered.append(pair)
      if self._fee_enricher is not None and filtered:
        filtered = await self._fee_enricher.enrich_pairs(chain_name, filtered)
      filtered = [p for p in filtered if p.fee_bps <= 30]
      filtered = await self._healthy_pools(chain_name, base, quote, filtered)
      filtered = sorted(filtered, key=lambda p: (p.fee_bps, -p.liquidity_usd))[:5]
      for left, right in combinations(filtered, 2):
        for buy, sell in ((left, right), (right, left)):
          fee_drag = buy.fee_bps + sell.fee_bps
          if fee_drag > 40:
            continue
          spread_bps = (
            (sell.price_usd - buy.price_usd) / max(buy.price_usd, 1e-12)
          ) * 10_000
          if spread_bps - fee_drag < -10:
            continue
          candidates.append(
            Opportunity(
              id=str(uuid.uuid4()),
              timestamp=datetime.now(timezone.utc),
              chain=chain_name,
              pair_label=f"{base}/{quote}",
              buy_dex=f"{buy.dex_id}({buy.fee_bps:.1f}bp)",
              sell_dex=f"{sell.dex_id}({sell.fee_bps:.1f}bp)",
              buy_price_usd=buy.price_usd,
              sell_price_usd=sell.price_usd,
              gross_spread_bps=spread_bps,
              buy_pair=buy,
              sell_pair=sell,
              strategy="spatial",
            )
          )

    seen: set[tuple[str, str]] = set()
    unique: list[Opportunity] = []
    for opp in candidates:
      key = (
        opp.buy_pair.pair_address.lower(),
        opp.sell_pair.pair_address.lower(),
      )
      if key in seen:
        continue
      seen.add(key)
      unique.append(opp)
    unique = unique[:20]

    jobs = [(opp, probe_usd) for opp in unique]
    results = await self._onchain.quote_round_trips_parallel(jobs)

    scored: list[tuple[float, Opportunity]] = []
    for opp, rt in zip(unique, results):
      if rt is None:
        continue
      if rt.sell_output_usd < probe_usd * 0.90:
        continue
      if rt.effective_spread_bps < -3.0:
        continue
      refreshed = opp.model_copy(update={"gross_spread_bps": rt.effective_spread_bps})
      scored.append((rt.effective_spread_bps, refreshed))

    scored.sort(key=lambda row: row[0], reverse=True)
    result = [opp for _bps, opp in scored[:8]]
    # Remember near-miss routes for fast re-quotes next tick.
    if result:
      self._hot_routes = result
    elif unique:
      # Keep the closest misses for hot requote even if below -3 threshold.
      near = []
      for opp, rt in zip(unique, results):
        if rt is None or rt.sell_output_usd < probe_usd * 0.90:
          continue
        if rt.effective_spread_bps >= -8:
          near.append(
            (rt.effective_spread_bps, opp.model_copy(update={"gross_spread_bps": rt.effective_spread_bps}))
          )
      near.sort(key=lambda r: -r[0])
      self._hot_routes = [o for _, o in near[:10]]
    return result
