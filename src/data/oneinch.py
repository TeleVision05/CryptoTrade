from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from src.http_client import RetryHttpClient
from src.data.tokens import from_raw_amount, to_raw_amount


@dataclass
class SwapQuote:
  src_amount_raw: int
  dst_amount_raw: int
  src_decimals: int
  dst_decimals: int
  gas_estimate: int | None = None

  @property
  def src_amount(self) -> float:
    return from_raw_amount(self.src_amount_raw, self.src_decimals)

  @property
  def dst_amount(self) -> float:
    return from_raw_amount(self.dst_amount_raw, self.dst_decimals)


class OneInchClient:
  BASE_URL = "https://api.1inch.com/swap/v6.1"

  def __init__(self, api_key: str, min_interval_sec: float = 1.05) -> None:
    self._api_key = api_key
    self._min_interval_sec = min_interval_sec
    self._lock = asyncio.Lock()
    self._last_request_at = 0.0
    self._http: RetryHttpClient | None = None

  @property
  def enabled(self) -> bool:
    return bool(self._api_key)

  async def open(self) -> None:
    self._http = RetryHttpClient(timeout=20.0)
    await self._http.__aenter__()

  async def close(self) -> None:
    if self._http is not None:
      await self._http.__aexit__(None, None, None)
      self._http = None

  async def _throttle(self) -> None:
    async with self._lock:
      now = asyncio.get_event_loop().time()
      elapsed = now - self._last_request_at
      if elapsed < self._min_interval_sec:
        await asyncio.sleep(self._min_interval_sec - elapsed)
      self._last_request_at = asyncio.get_event_loop().time()

  def _headers(self) -> dict[str, str]:
    return {
      "Authorization": f"Bearer {self._api_key}",
      "Accept": "application/json",
    }

  async def get_quote(
    self,
    chain_id: int,
    src_token: str,
    dst_token: str,
    amount_raw: int,
    src_decimals: int,
    dst_decimals: int,
  ) -> SwapQuote | None:
    if not self.enabled or self._http is None:
      return None

    await self._throttle()
    url = f"{self.BASE_URL}/{chain_id}/quote"
    params = {
      "src": src_token,
      "dst": dst_token,
      "amount": str(amount_raw),
    }
    try:
      response = await self._http.get(url, params=params, headers=self._headers())
      response.raise_for_status()
      payload = response.json()
    except Exception:
      return None

    dst_amount_raw = int(payload.get("dstAmount") or 0)
    if dst_amount_raw <= 0:
      return None

    gas_estimate = None
    if payload.get("gas"):
      try:
        gas_estimate = int(payload["gas"])
      except (TypeError, ValueError):
        gas_estimate = None

    return SwapQuote(
      src_amount_raw=amount_raw,
      dst_amount_raw=dst_amount_raw,
      src_decimals=src_decimals,
      dst_decimals=dst_decimals,
      gas_estimate=gas_estimate,
    )

  async def get_swap_transaction(
    self,
    chain_id: int,
    src_token: str,
    dst_token: str,
    amount_raw: int,
    from_address: str,
    src_decimals: int,
    dst_decimals: int,
    slippage_bps: float = 50.0,
    disable_estimate: bool = False,
  ) -> dict[str, Any] | None:
    if not self.enabled or self._http is None:
      return None

    await self._throttle()
    url = f"{self.BASE_URL}/{chain_id}/swap"
    params = {
      "src": src_token,
      "dst": dst_token,
      "amount": str(amount_raw),
      "from": from_address,
      "slippage": str(slippage_bps / 100),
      "disableEstimate": "true" if disable_estimate else "false",
    }
    try:
      response = await self._http.get(url, params=params, headers=self._headers())
      response.raise_for_status()
      payload = response.json()
    except Exception:
      return None

    tx = payload.get("tx")
    if not isinstance(tx, dict):
      return None

    dst_amount = payload.get("dstAmount")
    return {
      "to": tx["to"],
      "data": tx["data"],
      "value": tx.get("value", "0"),
      "gas": tx.get("gas"),
      "gasPrice": tx.get("gasPrice"),
      "chainId": chain_id,
      "dstAmount": int(dst_amount) if dst_amount is not None else None,
    }

  async def quote_usd_to_token(
    self,
    chain_id: int,
    usd_token: str,
    usd_decimals: int,
    dst_token: str,
    dst_decimals: int,
    usd_amount: float,
  ) -> SwapQuote | None:
    amount_raw = to_raw_amount(usd_amount, usd_decimals)
    return await self.get_quote(
      chain_id=chain_id,
      src_token=usd_token,
      dst_token=dst_token,
      amount_raw=amount_raw,
      src_decimals=usd_decimals,
      dst_decimals=dst_decimals,
    )

  async def quote_token_to_usd(
    self,
    chain_id: int,
    src_token: str,
    src_decimals: int,
    usd_token: str,
    usd_decimals: int,
    token_amount: float,
  ) -> SwapQuote | None:
    amount_raw = to_raw_amount(token_amount, src_decimals)
    return await self.get_quote(
      chain_id=chain_id,
      src_token=src_token,
      dst_token=usd_token,
      amount_raw=amount_raw,
      src_decimals=src_decimals,
      dst_decimals=usd_decimals,
    )
