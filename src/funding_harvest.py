from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.config import AppConfig
from src.data.hyperliquid import HyperliquidClient, HyperliquidMarket
from src.ledger import Ledger
from src.models import PaperTrade

# Illiquid / oracle-weird names we still skip even if funding is fat.
HARVEST_BLACKLIST = {"CASHCAT", "PURR", "kSHIB", "FRIEND", "STRAX", "SHIA", "NFTI"}


@dataclass
class HarvestPosition:
  id: str
  coin: str
  side: str  # short if funding>0 (get paid); long if funding<0
  notional_usd: float
  entry_px: float
  stop_px: float
  take_px: float
  opened_at_mono: float
  last_mark: float
  entry_funding_hourly: float
  last_accrual_mono: float
  accrued_funding_usd: float = 0.0
  booked_funding_usd: float = 0.0
  open_fee_usd: float = 0.0


@dataclass
class HarvestTickResult:
  messages: list[str] = field(default_factory=list)


class FundingHarvestEngine:
  """
  Extreme funding harvest (higher risk / higher reward).

  When HL hourly funding is abnormally rich/cheap on a liquid coin, take the
  *receiving* side of funding with a hard stop. Real marks + real funding.
  Live path: Hyperliquid perps.

  Example: +7bps/h funding → short → ~$0.17/h per $250 while it lasts.
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
    self._pos: dict[str, HarvestPosition] = {}
    self._cooldown_until: dict[str, float] = {}

  @property
  def enabled(self) -> bool:
    return bool(getattr(self._config, "harvest_enabled", False))

  @property
  def open_positions(self) -> list[dict]:
    out = []
    for p in self._pos.values():
      out.append(
        {
          "coin": p.coin,
          "side": p.side,
          "kind": "funding_harvest",
          "notional_usd": p.notional_usd,
          "accrued_usd": p.accrued_funding_usd,
          "sim_hours": (time.monotonic() - p.opened_at_mono) / 3600.0,
          "entry_px": p.entry_px,
          "mark_px": p.last_mark,
          "funding_bps_h": p.entry_funding_hourly * 10_000,
        }
      )
    return out

  async def tick(self) -> HarvestTickResult:
    result = HarvestTickResult()
    if not self.enabled:
      return result
    markets = await self._hl.fetch_markets()
    if not markets:
      result.messages.append("Harvest: HL fetch failed")
      return result
    by = {m.coin: m for m in markets}
    await self._manage(by, result)
    await self._hunt(by, result)
    return result

  async def _manage(
    self, by: dict[str, HyperliquidMarket], result: HarvestTickResult
  ) -> None:
    exit_fund = float(getattr(self._config, "harvest_exit_bps_hourly", 0.25))
    max_hold = float(getattr(self._config, "harvest_max_hold_hours", 36.0))
    fee_bps = float(getattr(self._config, "harvest_fee_bps_per_leg", 2.5))
    slip_bps = float(getattr(self._config, "harvest_slippage_bps", 3.0))
    book_h = float(getattr(self._config, "harvest_accrual_book_hours", 0.05))

    for coin in list(self._pos.keys()):
      pos = self._pos[coin]
      m = by.get(coin)
      if m is None or m.mark_px <= 0:
        continue
      mark = m.mark_px
      pos.last_mark = mark

      now = time.monotonic()
      dt_h = max(0.0, (now - pos.last_accrual_mono) / 3600.0)
      pos.last_accrual_mono = now
      # Receiving side: short earns +funding when funding>0; long earns -funding when funding<0
      signed = m.funding_hourly if pos.side == "short" else -m.funding_hourly
      pos.accrued_funding_usd += pos.notional_usd * signed * dt_h

      pending = pos.accrued_funding_usd - pos.booked_funding_usd
      held_h = (now - pos.opened_at_mono) / 3600.0
      if pending >= 0.0005 and (
        held_h - getattr(pos, "_booked_h", 0.0) >= book_h or pending >= 0.01
      ):
        await self._book(
          pos.id,
          f"{coin}-HARVEST funding",
          coin,
          pos.side,
          pos.notional_usd,
          mark,
          pending,
          0.0,
          pending,
        )
        pos.booked_funding_usd = pos.accrued_funding_usd
        pos._booked_h = held_h  # type: ignore[attr-defined]
        result.messages.append(
          f"HARVEST ACCRUE {coin} ${pending:+.4f} "
          f"(total ${pos.accrued_funding_usd:+.4f}, "
          f"live fund {m.funding_bps_hourly:+.2f}bps/h, {held_h:.2f}h)"
        )

      if pos.side == "short":
        move = (pos.entry_px - mark) / pos.entry_px
        hit_stop = mark >= pos.stop_px
        hit_take = mark <= pos.take_px
      else:
        move = (mark - pos.entry_px) / pos.entry_px
        hit_stop = mark <= pos.stop_px
        hit_take = mark >= pos.take_px

      fund_edge = m.funding_bps_hourly if pos.side == "short" else -m.funding_bps_hourly
      reason = None
      if hit_stop:
        reason = "stop"
      elif hit_take:
        reason = "take profit"
      elif fund_edge < exit_fund:
        reason = "funding normalized"
      elif held_h >= max_hold:
        reason = "max hold"

      if reason is None:
        continue

      close_cost = pos.notional_usd * ((fee_bps + slip_bps) / 10_000)
      # Realize remaining funding not yet booked + price move; settle reserved open fee.
      pending_fund = pos.accrued_funding_usd - pos.booked_funding_usd
      price_pnl = pos.notional_usd * move
      gross = pending_fund + price_pnl
      total_fees = pos.open_fee_usd + close_cost
      net = gross - total_fees
      await self._book(
        pos.id,
        f"{coin}-HARVEST close ({reason})",
        coin,
        pos.side,
        pos.notional_usd,
        mark,
        gross,
        total_fees,
        net,
      )
      self._pos.pop(coin, None)
      self._cooldown_until[coin] = now + float(
        getattr(self._config, "harvest_cooldown_sec", 1200)
      )
      result.messages.append(
        f"HARVEST CLOSE {coin} {pos.side} net=${net:+.4f} "
        f"({reason}, move={move*1e4:+.1f}bps, fund_total=${pos.accrued_funding_usd:+.4f})"
      )

  async def _hunt(
    self, by: dict[str, HyperliquidMarket], result: HarvestTickResult
  ) -> None:
    max_pos = int(getattr(self._config, "harvest_max_positions", 2))
    if len(self._pos) >= max_pos:
      return

    min_bps = float(getattr(self._config, "harvest_min_bps_hourly", 0.50))
    min_oi = float(getattr(self._config, "harvest_min_oi_usd", 3_000_000))
    max_basis = float(getattr(self._config, "harvest_max_basis_bps", 80.0))
    notional = float(getattr(self._config, "harvest_notional_usd", 250))
    capital_pct = float(getattr(self._config, "harvest_max_capital_pct", 0.30))
    stop_pct = float(getattr(self._config, "harvest_stop_pct", 0.03))
    take_pct = float(getattr(self._config, "harvest_take_pct", 0.04))
    fee_bps = float(getattr(self._config, "harvest_fee_bps_per_leg", 2.5))
    slip_bps = float(getattr(self._config, "harvest_slippage_bps", 3.0))

    cands: list[tuple[float, HyperliquidMarket]] = []
    now = time.monotonic()
    for m in by.values():
      if m.coin in HARVEST_BLACKLIST or m.coin in self._pos:
        continue
      if now < self._cooldown_until.get(m.coin, 0):
        continue
      if m.open_interest_usd < min_oi or m.mark_px <= 0 or m.oracle_px <= 0:
        continue
      if abs(m.funding_bps_hourly) < min_bps:
        continue
      basis = (m.mark_px - m.oracle_px) / m.oracle_px * 10_000
      if abs(basis) > max_basis:
        continue
      # Prefer funding that agrees with basis (rich perp → short)
      agree = 1.0
      if m.funding_hourly > 0 and basis < -5:
        agree = 0.5
      if m.funding_hourly < 0 and basis > 5:
        agree = 0.5
      score = abs(m.funding_hourly) * (m.open_interest_usd ** 0.15) * agree
      cands.append((score, m))
    cands.sort(key=lambda r: r[0], reverse=True)

    for _score, m in cands:
      if len(self._pos) >= max_pos:
        break
      balance = await self._ledger.get_balance()
      size = min(notional, balance * capital_pct)
      if size < 50:
        break
      side = "short" if m.funding_hourly > 0 else "long"
      open_cost = size * ((fee_bps + slip_bps) / 10_000)
      px = m.mark_px
      if side == "short":
        entry = px * (1 - slip_bps / 10_000)
        stop = entry * (1 + stop_pct)
        take = entry * (1 - take_pct)
      else:
        entry = px * (1 + slip_bps / 10_000)
        stop = entry * (1 - stop_pct)
        take = entry * (1 + take_pct)

      pos = HarvestPosition(
        id=str(uuid.uuid4()),
        coin=m.coin,
        side=side,
        notional_usd=size,
        entry_px=entry,
        stop_px=stop,
        take_px=take,
        opened_at_mono=time.monotonic(),
        last_mark=px,
        entry_funding_hourly=m.funding_hourly,
        last_accrual_mono=time.monotonic(),
        open_fee_usd=open_cost,
      )
      self._pos[m.coin] = pos
      # Reserve open cost on the position; settle on close with price + funding.
      per_h = size * abs(m.funding_hourly)
      result.messages.append(
        f"HARVEST OPEN {m.coin} {side} ${size:.0f} @ {entry:.4g} "
        f"(fund {m.funding_bps_hourly:+.2f}bps/h ≈ ${per_h:.3f}/h wall-clock, "
        f"stop {stop_pct:.0%}/take {take_pct:.0%}, reserved fee ${open_cost:.3f})"
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
    parts = [
      f"{p.coin}/{p.side} fund=${p.accrued_funding_usd:+.4f}"
      for p in self._pos.values()
    ]
    return "Harvest: " + ", ".join(parts)
