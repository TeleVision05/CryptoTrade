from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.config import AppConfig
from src.data.hyperliquid import HyperliquidClient, HyperliquidMarket
from src.ledger import Ledger
from src.models import PaperTrade

DEFAULT_UNIVERSE = ["BTC", "ETH", "SOL", "ZRO", "WLD", "SUI", "LINK", "NEAR", "DOGE", "AVAX"]


@dataclass
class MomentumPosition:
  id: str
  coin: str
  side: str  # long | short
  notional_usd: float
  entry_px: float
  stop_px: float
  take_px: float
  opened_at_mono: float
  last_mark: float
  open_fee_usd: float
  funding_hourly_at_entry: float
  unrealized_usd: float = 0.0
  accrued_funding_usd: float = 0.0
  last_funding_mono: float = 0.0


@dataclass
class MomentumTickResult:
  messages: list[str] = field(default_factory=list)


class MomentumEngine:
  """
  Higher risk/reward: Hyperliquid directional trend + funding tilt.

  Real 15m candles + live marks. Pays taker fees + slippage. Can lose.
  Live path: HL API wallet trading perps.
  """

  def __init__(
    self,
    config: AppConfig,
    ledger: Ledger,
    hl: HyperliquidClient,
  ) -> None:
    self._config = config
    self._ledger = ledger
    self._hl = hl
    self._pos: dict[str, MomentumPosition] = {}
    self._cooldown_until: dict[str, float] = {}
    self._candle_cache: dict[str, tuple[float, list[float]]] = {}

  @property
  def enabled(self) -> bool:
    return bool(getattr(self._config, "momentum_enabled", False))

  @property
  def open_positions(self) -> list[dict]:
    out = []
    for p in self._pos.values():
      out.append(
        {
          "coin": p.coin,
          "side": p.side,
          "kind": "momentum",
          "notional_usd": p.notional_usd,
          "accrued_usd": p.unrealized_usd + p.accrued_funding_usd,
          "sim_hours": (time.monotonic() - p.opened_at_mono) / 3600.0,
          "entry_px": p.entry_px,
          "mark_px": p.last_mark,
        }
      )
    return out

  async def tick(self) -> MomentumTickResult:
    result = MomentumTickResult()
    if not self.enabled:
      return result

    markets = await self._hl.fetch_markets()
    if not markets:
      result.messages.append("Momentum: Hyperliquid fetch failed")
      return result
    by_coin = {m.coin: m for m in markets}

    await self._manage_open(by_coin, result)
    await self._hunt_entries(by_coin, result)
    return result

  async def _closes(self, coin: str) -> list[float]:
    now = time.monotonic()
    cached = self._candle_cache.get(coin)
    if cached and now - cached[0] < 60:
      return cached[1]
    bars = await self._hl.fetch_candles(coin, interval="15m", lookback=48)
    closes = [b["c"] for b in bars]
    self._candle_cache[coin] = (now, closes)
    return closes

  def _signal(self, closes: list[float], funding_h: float) -> str | None:
    """Return long | short | None."""
    sma_n = int(getattr(self._config, "momentum_sma_bars", 20))
    roc_n = int(getattr(self._config, "momentum_roc_bars", 4))
    min_roc_bps = float(getattr(self._config, "momentum_min_roc_bps", 15.0))
    max_pay_bps = float(getattr(self._config, "momentum_max_funding_pay_bps", 0.35))
    short_fund_bps = float(getattr(self._config, "momentum_short_funding_bps", 0.20))

    if len(closes) < max(sma_n, roc_n) + 1:
      return None
    px = closes[-1]
    sma = sum(closes[-sma_n:]) / sma_n
    roc = (px / closes[-1 - roc_n] - 1.0) * 10_000
    fund_bps = funding_h * 10_000

    # Trend long: above SMA + positive ROC, and not paying crazy funding
    if px > sma and roc >= min_roc_bps and fund_bps <= max_pay_bps:
      return "long"
    # Trend short: below SMA + negative ROC
    if px < sma and roc <= -min_roc_bps:
      return "short"
    # Funding tilt short when rich funding even if mild trend
    if fund_bps >= short_fund_bps and px <= sma * 1.002:
      return "short"
    return None

  async def _manage_open(
    self, by_coin: dict[str, HyperliquidMarket], result: MomentumTickResult
  ) -> None:
    stop_pct = float(getattr(self._config, "momentum_stop_pct", 0.012))
    take_pct = float(getattr(self._config, "momentum_take_pct", 0.024))
    max_hold_h = float(getattr(self._config, "momentum_max_hold_hours", 24.0))
    fee_bps = float(getattr(self._config, "momentum_fee_bps_per_leg", 2.5))
    slip_bps = float(getattr(self._config, "momentum_slippage_bps", 2.0))

    for coin in list(self._pos.keys()):
      pos = self._pos[coin]
      m = by_coin.get(coin)
      if m is None or m.mark_px <= 0:
        continue
      mark = m.mark_px
      pos.last_mark = mark

      # Accrue HL funding (longs pay when funding>0)
      now = time.monotonic()
      if pos.last_funding_mono <= 0:
        pos.last_funding_mono = pos.opened_at_mono
      dt_h = max(0.0, (now - pos.last_funding_mono) / 3600.0)
      pos.last_funding_mono = now
      signed_fund = -m.funding_hourly if pos.side == "long" else m.funding_hourly
      pos.accrued_funding_usd += pos.notional_usd * signed_fund * dt_h

      if pos.side == "long":
        move = (mark - pos.entry_px) / pos.entry_px
        hit_stop = mark <= pos.stop_px
        hit_take = mark >= pos.take_px
      else:
        move = (pos.entry_px - mark) / pos.entry_px
        hit_stop = mark >= pos.stop_px
        hit_take = mark <= pos.take_px

      pos.unrealized_usd = pos.notional_usd * move
      held_h = (now - pos.opened_at_mono) / 3600.0

      reason = None
      if hit_stop:
        reason = "stop"
      elif hit_take:
        reason = "take profit"
      elif held_h >= max_hold_h:
        reason = "max hold"
      # Flip signal exit
      if reason is None and held_h >= 0.25:
        closes = await self._closes(coin)
        sig = self._signal(closes, m.funding_hourly)
        if sig and sig != pos.side:
          reason = "signal flip"

      if reason is None:
        continue

      close_cost = pos.notional_usd * ((fee_bps + slip_bps) / 10_000)
      gross = pos.notional_usd * move + pos.accrued_funding_usd
      net = gross - close_cost
      await self._book(
        pos.id,
        f"{coin}-MOM {pos.side} close ({reason})",
        coin,
        pos.side,
        pos.notional_usd,
        mark,
        gross,
        close_cost,
        net,
      )
      self._pos.pop(coin, None)
      self._cooldown_until[coin] = time.monotonic() + float(
        getattr(self._config, "momentum_cooldown_sec", 900)
      )
      result.messages.append(
        f"MOM CLOSE {coin} {pos.side} net=${net:+.4f} "
        f"({reason}, move={move*1e4:+.1f}bps, fund=${pos.accrued_funding_usd:+.4f})"
      )

  async def _hunt_entries(
    self, by_coin: dict[str, HyperliquidMarket], result: MomentumTickResult
  ) -> None:
    max_pos = int(getattr(self._config, "momentum_max_positions", 2))
    if len(self._pos) >= max_pos:
      return

    notional = float(getattr(self._config, "momentum_notional_usd", 250))
    capital_pct = float(getattr(self._config, "momentum_max_capital_pct", 0.40))
    stop_pct = float(getattr(self._config, "momentum_stop_pct", 0.012))
    take_pct = float(getattr(self._config, "momentum_take_pct", 0.024))
    fee_bps = float(getattr(self._config, "momentum_fee_bps_per_leg", 2.5))
    slip_bps = float(getattr(self._config, "momentum_slippage_bps", 2.0))
    min_oi = float(getattr(self._config, "momentum_min_oi_usd", 15_000_000))
    universe = list(getattr(self._config, "momentum_universe", DEFAULT_UNIVERSE))

    balance = await self._ledger.get_balance()
    size = min(notional, balance * capital_pct)
    if size < 50:
      return

    # Rank candidates by |ROC| strength
    scored: list[tuple[float, str, str, HyperliquidMarket, float]] = []
    now = time.monotonic()
    for coin in universe:
      if coin in self._pos:
        continue
      if now < self._cooldown_until.get(coin, 0):
        continue
      m = by_coin.get(coin)
      if m is None or m.open_interest_usd < min_oi or m.mark_px <= 0:
        continue
      closes = await self._closes(coin)
      sig = self._signal(closes, m.funding_hourly)
      if not sig:
        continue
      sma_n = int(getattr(self._config, "momentum_sma_bars", 20))
      roc_n = int(getattr(self._config, "momentum_roc_bars", 4))
      if len(closes) < max(sma_n, roc_n) + 1:
        continue
      roc = abs((closes[-1] / closes[-1 - roc_n] - 1.0) * 10_000)
      # Boost when funding agrees with short
      boost = abs(m.funding_bps_hourly) if sig == "short" and m.funding_hourly > 0 else 0
      scored.append((roc + boost, coin, sig, m, closes[-1]))

    scored.sort(reverse=True)
    for _score, coin, side, m, px in scored:
      if len(self._pos) >= max_pos:
        break
      # Re-check size vs live balance
      balance = await self._ledger.get_balance()
      size = min(notional, balance * capital_pct)
      if size < 50:
        break

      open_cost = size * ((fee_bps + slip_bps) / 10_000)
      if side == "long":
        entry = px * (1 + slip_bps / 10_000)
        stop = entry * (1 - stop_pct)
        take = entry * (1 + take_pct)
      else:
        entry = px * (1 - slip_bps / 10_000)
        stop = entry * (1 + stop_pct)
        take = entry * (1 - take_pct)

      pos = MomentumPosition(
        id=str(uuid.uuid4()),
        coin=coin,
        side=side,
        notional_usd=size,
        entry_px=entry,
        stop_px=stop,
        take_px=take,
        opened_at_mono=time.monotonic(),
        last_mark=m.mark_px,
        open_fee_usd=open_cost,
        funding_hourly_at_entry=m.funding_hourly,
        last_funding_mono=time.monotonic(),
      )
      self._pos[coin] = pos
      await self._book(
        pos.id,
        f"{coin}-MOM {side} open",
        coin,
        side,
        size,
        entry,
        -open_cost,
        open_cost,
        -open_cost,
      )
      result.messages.append(
        f"MOM OPEN {coin} {side} ${size:.0f} @ {entry:.4g} "
        f"(stop {stop:.4g} / take {take:.4g}, fund {m.funding_bps_hourly:+.2f}bps/h)"
      )

  async def _book(
    self,
    opportunity_id: str,
    label: str,
    coin: str,
    side: str,
    notional: float,
    px: float,
    gross: float,
    fees: float,
    net: float,
  ) -> None:
    balance = await self._ledger.get_balance()
    new_balance = balance + net
    await self._ledger.update_balance(new_balance)
    trade = PaperTrade(
      id=str(uuid.uuid4()),
      opportunity_id=opportunity_id,
      timestamp=datetime.now(timezone.utc),
      chain="hyperliquid",
      pair_label=label,
      buy_dex="hyperliquid",
      sell_dex=side,
      trade_size_usd=notional,
      buy_price_usd=px,
      sell_price_usd=px,
      gross_pnl_usd=gross,
      fees_usd=fees,
      gas_cost_usd=0.0,
      mev_haircut_usd=0.0,
      net_pnl_usd=net,
      balance_after_usd=new_balance,
    )
    await self._ledger.save_trade(trade)
    await self._ledger.maybe_record_snapshot()

  async def status_line(self) -> str | None:
    if not self._pos:
      return None
    parts = []
    for p in self._pos.values():
      parts.append(
        f"{p.coin}/{p.side} uPnL=${p.unrealized_usd:+.3f} "
        f"fund=${p.accrued_funding_usd:+.3f}"
      )
    return "Momentum: " + ", ".join(parts)
