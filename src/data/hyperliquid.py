from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass
class HyperliquidMarket:
  coin: str
  mark_px: float
  oracle_px: float
  funding_hourly: float  # fraction per hour (e.g. 0.0001 = 1bp/h)
  open_interest_usd: float
  day_ntl_vlm_usd: float
  premium: float

  @property
  def funding_bps_hourly(self) -> float:
    return self.funding_hourly * 10_000

  @property
  def funding_apr_pct(self) -> float:
    return self.funding_hourly * 24 * 365 * 100


class HyperliquidClient:
  """Read-only Hyperliquid info API (no auth)."""

  INFO_URL = "https://api.hyperliquid.xyz/info"

  def __init__(self, timeout: float = 20.0) -> None:
    self._timeout = timeout
    self._http: httpx.AsyncClient | None = None

  async def open(self) -> None:
    self._http = httpx.AsyncClient(timeout=self._timeout)

  async def close(self) -> None:
    if self._http is not None:
      await self._http.aclose()
      self._http = None

  async def fetch_markets(self) -> list[HyperliquidMarket]:
    if self._http is None:
      return []
    try:
      response = await self._http.post(
        self.INFO_URL,
        json={"type": "metaAndAssetCtxs"},
      )
      response.raise_for_status()
      payload = response.json()
    except Exception:
      return []

    if not isinstance(payload, list) or len(payload) < 2:
      return []
    meta, ctxs = payload[0], payload[1]
    universe = meta.get("universe") or []
    if not isinstance(universe, list) or not isinstance(ctxs, list):
      return []

    markets: list[HyperliquidMarket] = []
    for asset, ctx in zip(universe, ctxs):
      try:
        coin = str(asset.get("name") or "")
        if not coin:
          continue
        mark = float(ctx.get("markPx") or 0)
        oracle = float(ctx.get("oraclePx") or mark or 0)
        funding = float(ctx.get("funding") or 0)
        oi = float(ctx.get("openInterest") or 0)
        day_vlm = float(ctx.get("dayNtlVlm") or 0)
        premium = float(ctx.get("premium") or 0)
        oi_usd = oi * mark if mark > 0 else 0.0
        markets.append(
          HyperliquidMarket(
            coin=coin,
            mark_px=mark,
            oracle_px=oracle,
            funding_hourly=funding,
            open_interest_usd=oi_usd,
            day_ntl_vlm_usd=day_vlm,
            premium=premium,
          )
        )
      except (TypeError, ValueError):
        continue
    return markets

  async def fetch_candles(
    self,
    coin: str,
    *,
    interval: str = "15m",
    lookback: int = 48,
  ) -> list[dict]:
    """Return OHLCV bars newest-last. Each bar: t,o,h,l,c,v as floats."""
    if self._http is None:
      return []
    import time

    end = int(time.time() * 1000)
    # interval minutes
    mins = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240}.get(interval, 15)
    start = end - lookback * mins * 60 * 1000
    try:
      response = await self._http.post(
        self.INFO_URL,
        json={
          "type": "candleSnapshot",
          "req": {
            "coin": coin,
            "interval": interval,
            "startTime": start,
            "endTime": end,
          },
        },
      )
      response.raise_for_status()
      rows = response.json()
    except Exception:
      return []
    if not isinstance(rows, list):
      return []
    out: list[dict] = []
    for row in rows:
      try:
        out.append(
          {
            "t": int(row.get("t") or 0),
            "o": float(row.get("o") or 0),
            "h": float(row.get("h") or 0),
            "l": float(row.get("l") or 0),
            "c": float(row.get("c") or 0),
            "v": float(row.get("v") or 0),
          }
        )
      except (TypeError, ValueError):
        continue
    return out
