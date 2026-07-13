from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass
class YieldPool:
  pool_id: str
  project: str
  chain: str
  symbol: str
  apy_pct: float  # total advertised APY
  apy_base_pct: float  # supply APY without reward emissions
  tvl_usd: float

  @property
  def honest_apy_pct(self) -> float:
    """Prefer base supply APY; fall back to total if base missing."""
    if self.apy_base_pct > 0:
      return self.apy_base_pct
    return self.apy_pct


class DefiLlamaYieldClient:
  """Live DeFi lending yields via DefiLlama (public, no auth)."""

  POOLS_URL = "https://yields.llama.fi/pools"
  # Plain stable lending only — not LP / leveraged morpho oddities.
  ALLOWED_PROJECTS = {"aave-v3", "compound-v3", "fluid-lending"}
  ALLOWED_SYMBOLS = {"USDC", "USDT", "USDC.e", "DAI"}
  ALLOWED_CHAINS = {"Base", "Arbitrum", "Ethereum"}

  def __init__(self, timeout: float = 25.0) -> None:
    self._timeout = timeout
    self._http: httpx.AsyncClient | None = None
    self._cache: list[YieldPool] = []
    self._cache_mono = 0.0

  async def open(self) -> None:
    self._http = httpx.AsyncClient(
      timeout=self._timeout,
      headers={"User-Agent": "CryptoTrade/1.0"},
    )

  async def close(self) -> None:
    if self._http is not None:
      await self._http.aclose()
      self._http = None

  async def fetch_stable_pools(
    self,
    *,
    min_tvl_usd: float = 20_000_000,
    prefer_chains: list[str] | None = None,
    force: bool = False,
  ) -> list[YieldPool]:
    import time

    if (
      not force
      and self._cache
      and time.monotonic() - self._cache_mono < 120
    ):
      return list(self._cache)
    if self._http is None:
      return list(self._cache)

    try:
      response = await self._http.get(self.POOLS_URL)
      response.raise_for_status()
      rows = response.json().get("data") or []
    except Exception:
      return list(self._cache)

    prefer = {c.lower() for c in (prefer_chains or ["base", "arbitrum"])}
    out: list[YieldPool] = []
    for row in rows:
      try:
        project = str(row.get("project") or "")
        symbol = str(row.get("symbol") or "")
        chain = str(row.get("chain") or "")
        if project not in self.ALLOWED_PROJECTS:
          continue
        if symbol not in self.ALLOWED_SYMBOLS:
          continue
        if chain not in self.ALLOWED_CHAINS:
          continue
        tvl = float(row.get("tvlUsd") or 0)
        if tvl < min_tvl_usd:
          continue
        apy = float(row.get("apy") or 0)
        apy_base = float(row.get("apyBase") or 0)
        if apy <= 0 and apy_base <= 0:
          continue
        out.append(
          YieldPool(
            pool_id=str(row.get("pool") or ""),
            project=project,
            chain=chain,
            symbol=symbol,
            apy_pct=apy,
            apy_base_pct=apy_base,
            tvl_usd=tvl,
          )
        )
      except (TypeError, ValueError):
        continue

    # Prefer higher honest APY on preferred chains; gas is native (not USDC).
    def score(p: YieldPool) -> tuple:
      chain_boost = 2 if p.chain.lower() in prefer else 0
      # Mild preference for Base/Arbitrum over Ethereum for cheaper live gas.
      l2 = 1 if p.chain.lower() in {"base", "arbitrum"} else 0
      return (chain_boost + l2, p.honest_apy_pct, p.tvl_usd)

    out.sort(key=score, reverse=True)
    self._cache = out
    self._cache_mono = time.monotonic()
    return list(out)
