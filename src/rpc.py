from __future__ import annotations

import time
from typing import Callable, TypeVar

from web3 import Web3

T = TypeVar("T")

# Public endpoints — rotate on 429 / connection errors.
DEFAULT_RPCS: dict[str, list[str]] = {
  "base": [
    "https://base.publicnode.com",
    "https://1rpc.io/base",
    "https://base.drpc.org",
    "https://mainnet.base.org",
  ],
  "arbitrum": [
    "https://arbitrum.publicnode.com",
    "https://1rpc.io/arb",
    "https://arbitrum.drpc.org",
    "https://arb1.arbitrum.io/rpc",
  ],
}


class ResilientWeb3:
  """Web3 wrapper that fails over across RPC URLs and retries on 429."""

  def __init__(self, chain: str, preferred: str = "", timeout: float = 25.0) -> None:
    self.chain = chain
    urls = list(DEFAULT_RPCS.get(chain, []))
    if preferred:
      preferred = preferred.rstrip("/")
      urls = [preferred] + [u for u in urls if u.rstrip("/") != preferred]
    if not urls:
      raise RuntimeError(f"No RPC URLs for {chain}")
    self._urls = urls
    self._timeout = timeout
    self._index = 0
    self._w3: Web3 | None = None

  @property
  def url(self) -> str:
    return self._urls[self._index]

  def connect(self) -> Web3:
    if self._w3 is not None:
      return self._w3
    last_err: Exception | None = None
    for _ in range(len(self._urls)):
      url = self._urls[self._index]
      try:
        w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": self._timeout}))
        if w3.is_connected():
          self._w3 = w3
          return w3
      except Exception as exc:
        last_err = exc
      self._rotate()
    raise RuntimeError(f"No reachable RPC for {self.chain}: {last_err}")

  def _rotate(self) -> None:
    self._index = (self._index + 1) % len(self._urls)
    self._w3 = None

  def call(self, fn: Callable[[], T], retries: int = 4) -> T:
    last_err: Exception | None = None
    for attempt in range(retries):
      try:
        self.connect()
        return fn()
      except Exception as exc:
        last_err = exc
        msg = str(exc).lower()
        rate_limited = "429" in msg or "too many" in msg
        if attempt < retries - 1:
          if rate_limited:
            self._rotate()
            time.sleep(0.4 * (2**attempt))
          else:
            time.sleep(0.2 * (attempt + 1))
            if "connection" in msg or "timeout" in msg or "503" in msg:
              self._rotate()
          continue
        raise
    assert last_err is not None
    raise last_err
