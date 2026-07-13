from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass
class OkxFunding:
  coin: str
  funding_period: float  # rate for current period (usually ~8h)
  funding_hourly: float
  inst_id: str


class OkxClient:
  """OKX public funding endpoints (no auth)."""

  BASE = "https://www.okx.com"

  def __init__(self, timeout: float = 20.0) -> None:
    self._timeout = timeout
    self._http: httpx.AsyncClient | None = None
    self._cache: dict[str, OkxFunding] = {}

  async def open(self) -> None:
    self._http = httpx.AsyncClient(
      timeout=self._timeout,
      headers={"User-Agent": "CryptoTrade/1.0"},
    )

  async def close(self) -> None:
    if self._http is not None:
      await self._http.aclose()
      self._http = None

  async def funding_for(self, coin: str) -> OkxFunding | None:
    if self._http is None:
      return None
    if coin in self._cache:
      return self._cache[coin]
    inst = f"{coin}-USDT-SWAP"
    try:
      response = await self._http.get(
        f"{self.BASE}/api/v5/public/funding-rate",
        params={"instId": inst},
      )
      response.raise_for_status()
      payload = response.json()
      rows = payload.get("data") or []
      if not rows:
        return None
      period = float(rows[0].get("fundingRate") or 0)
      # OKX period is typically 8 hours — normalize to hourly for HL comparison.
      hourly = period / 8.0
      out = OkxFunding(coin=coin, funding_period=period, funding_hourly=hourly, inst_id=inst)
      self._cache[coin] = out
      return out
    except Exception:
      return None

  def clear_cache(self) -> None:
    self._cache.clear()
