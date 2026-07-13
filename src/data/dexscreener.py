from __future__ import annotations

import asyncio

from src.fees import infer_pool_fee_bps
from src.http_client import RetryHttpClient
from src.models import DexPair


class DexScreenerClient:
  BASE_URL = "https://api.dexscreener.com"

  def __init__(self, api_key: str = "", min_interval_sec: float = 0.25) -> None:
    self._api_key = api_key
    self._min_interval_sec = min_interval_sec
    self._lock = asyncio.Lock()
    self._last_request_at = 0.0
    self._http: RetryHttpClient | None = None

  async def open(self) -> None:
    self._http = RetryHttpClient(timeout=20.0)
    await self._http.__aenter__()

  async def close(self) -> None:
    if self._http is not None:
      await self._http.__aexit__(None, None, None)
      self._http = None

  def _headers(self) -> dict[str, str]:
    headers: dict[str, str] = {"Accept": "application/json"}
    if self._api_key:
      headers["Authorization"] = f"Bearer {self._api_key}"
    return headers

  async def _throttle(self) -> None:
    async with self._lock:
      now = asyncio.get_event_loop().time()
      elapsed = now - self._last_request_at
      if elapsed < self._min_interval_sec:
        await asyncio.sleep(self._min_interval_sec - elapsed)
      self._last_request_at = asyncio.get_event_loop().time()

  async def get_token_pairs(self, chain: str, token_address: str) -> list[DexPair]:
    if self._http is None:
      return []

    await self._throttle()
    url = f"{self.BASE_URL}/token-pairs/v1/{chain}/{token_address}"
    try:
      response = await self._http.get(url, headers=self._headers())
      response.raise_for_status()
      payload = response.json()
    except Exception:
      return []

    if not isinstance(payload, list):
      return []

    pairs: list[DexPair] = []
    for item in payload:
      pair = self._parse_pair(chain, item)
      if pair is not None:
        pairs.append(pair)
    return pairs

  def _parse_pair(self, chain: str, item: dict) -> DexPair | None:
    try:
      base = item.get("baseToken") or {}
      quote = item.get("quoteToken") or {}
      liquidity = item.get("liquidity") or {}
      volume = item.get("volume") or {}
      price_usd = float(item.get("priceUsd") or 0)
      if price_usd <= 0:
        return None
      labels = [str(label) for label in (item.get("labels") or [])]
      base_symbol = str(base.get("symbol") or "").upper()
      quote_symbol = str(quote.get("symbol") or "").upper()
      liquidity_usd = float(liquidity.get("usd") or 0)
      dex_id = str(item.get("dexId") or "").lower()
      fee_bps = infer_pool_fee_bps(
        dex_id=dex_id,
        labels=labels,
        liquidity_usd=liquidity_usd,
        base_symbol=base_symbol,
        quote_symbol=quote_symbol,
      )
      return DexPair(
        chain=chain,
        dex_id=dex_id,
        pair_address=str(item.get("pairAddress") or ""),
        base_symbol=base_symbol,
        quote_symbol=quote_symbol,
        price_usd=price_usd,
        liquidity_usd=liquidity_usd,
        volume_24h_usd=float(volume.get("h24") or 0),
        base_token_address=str(base.get("address") or ""),
        quote_token_address=str(quote.get("address") or ""),
        labels=labels,
        fee_bps=fee_bps,
      )
    except (TypeError, ValueError):
      return None
