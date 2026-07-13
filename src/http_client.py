from __future__ import annotations

import asyncio
from typing import Any

import httpx


class RetryHttpClient:
  """Shared async HTTP client with simple retry logic."""

  def __init__(self, timeout: float = 20.0, max_retries: int = 3) -> None:
    self._timeout = timeout
    self._max_retries = max_retries
    self._client: httpx.AsyncClient | None = None

  async def __aenter__(self) -> "RetryHttpClient":
    self._client = httpx.AsyncClient(timeout=self._timeout)
    return self

  async def __aexit__(self, *args: Any) -> None:
    if self._client is not None:
      await self._client.aclose()
      self._client = None

  async def get(
    self,
    url: str,
    *,
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
  ) -> httpx.Response:
    if self._client is None:
      raise RuntimeError("RetryHttpClient is not initialized")

    last_error: Exception | None = None
    for attempt in range(self._max_retries):
      try:
        response = await self._client.get(url, params=params, headers=headers)
        if response.status_code >= 500 and attempt < self._max_retries - 1:
          await asyncio.sleep(0.5 * (2**attempt))
          continue
        return response
      except httpx.HTTPError as exc:
        last_error = exc
        if attempt < self._max_retries - 1:
          await asyncio.sleep(0.5 * (2**attempt))
          continue
        raise
    if last_error is not None:
      raise last_error
    raise RuntimeError("HTTP request failed")
