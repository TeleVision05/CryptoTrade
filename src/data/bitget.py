from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass
class BitgetFunding:
  coin: str
  funding_period: float
  funding_hourly: float
  symbol: str


class BitgetClient:
  """Bitget public USDT-futures funding (no auth)."""

  BASE = "https://api.bitget.com"

  def __init__(self, timeout: float = 20.0) -> None:
    self._timeout = timeout
    self._http: httpx.AsyncClient | None = None
    self._cache: dict[str, BitgetFunding] = {}

  async def open(self) -> None:
    self._http = httpx.AsyncClient(
      timeout=self._timeout,
      headers={"User-Agent": "CryptoTrade/1.0"},
    )

  async def close(self) -> None:
    if self._http is not None:
      await self._http.aclose()
      self._http = None

  async def funding_for(self, coin: str) -> BitgetFunding | None:
    if self._http is None:
      return None
    if coin in self._cache:
      return self._cache[coin]
    symbol = f"{coin}USDT"
    try:
      response = await self._http.get(
        f"{self.BASE}/api/v2/mix/market/current-fund-rate",
        params={"productType": "USDT-FUTURES", "symbol": symbol},
      )
      response.raise_for_status()
      payload = response.json()
      rows = payload.get("data") or []
      if not rows:
        return None
      period = float(rows[0].get("fundingRate") or 0)
      # Bitget funding interval is typically 8h — normalize to hourly.
      hourly = period / 8.0
      out = BitgetFunding(
        coin=coin,
        funding_period=period,
        funding_hourly=hourly,
        symbol=symbol,
      )
      self._cache[coin] = out
      return out
    except Exception:
      return None

  def clear_cache(self) -> None:
    self._cache.clear()
