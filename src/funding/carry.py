from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.config import AppConfig
from src.data.bitget import BitgetClient
from src.data.hyperliquid import HyperliquidClient, HyperliquidMarket
from src.data.okx import OkxClient
from src.ledger import Ledger
from src.models import PaperTrade

# Skip pathological oracle/meme basis.
BASIS_BLACKLIST = {"CASHCAT", "BIGTIME", "PURR", "kPEPE", "kBONK", "kSHIB", "STRAX", "FRIEND"}

WATCH_COINS = [
  "ETH", "BTC", "SOL", "ZRO", "ARB", "OP", "DOGE", "SUI", "AVAX",
  "LINK", "WLD", "ENA", "JUP", "INJ", "TIA", "ADA", "APT", "BNB",
  "UNI", "NEAR", "AAVE", "PENDLE", "ONDO", "SEI", "LTC", "XRP",
]


@dataclass
class CarryPosition:
  id: str
  coin: str
  # short_hl = short Hyperliquid / long hedge venue (when HL funding > hedge)
  side: str
  notional_usd: float
  entry_mark: float
  entry_spread_hourly: float
  opened_at_mono: float
  last_accrual_mono: float
  hedge_venue: str = "okx"
  accrued_usd: float = 0.0
  hours_held: float = 0.0
  open_fee_usd: float = 0.0
  booked_accrual_usd: float = 0.0


@dataclass
class BasisPosition:
  id: str
  coin: str
  side: str  # short if mark>oracle (rich perp)
  notional_usd: float
  entry_mark: float
  entry_oracle: float
  entry_basis_bps: float
  opened_at_mono: float
  open_fee_usd: float = 0.0


@dataclass
class StrategyTickResult:
  messages: list[str] = field(default_factory=list)
  opened: list[str] = field(default_factory=list)
  closed: list[tuple[str, float]] = field(default_factory=list)


class HonestCarryEngine:
  """
  Wall-clock-only strategies (no time acceleration):

  1) HL↔CEX funding spread — earn the *difference* in hourly funding vs the
     best of OKX/Bitget while assuming a hedge (delta-neutral). Accrues in
     real time only. Opens only when edge clears fee breakeven gate.

  2) HL mark–oracle basis snap — secondary; take-profit on partial compression.
     Funding spread is preferred when both exist on the same coin.
  """

  def __init__(
    self,
    config: AppConfig,
    ledger: Ledger,
    hl: HyperliquidClient,
    okx: OkxClient,
    bitget: BitgetClient | None = None,
  ) -> None:
    self._config = config
    self._ledger = ledger
    self._hl = hl
    self._okx = okx
    self._bitget = bitget
    self._carry: dict[str, CarryPosition] = {}
    self._basis: dict[str, BasisPosition] = {}
    self._hedge_refresh_at = 0.0

  @property
  def enabled(self) -> bool:
    return bool(getattr(self._config, "funding_enabled", True))

  @property
  def open_positions(self) -> list[dict]:
    out = []
    for p in self._carry.values():
      out.append(
        {
          "coin": p.coin,
          "side": p.side,
          "kind": "funding_spread",
          "notional_usd": p.notional_usd,
          "accrued_usd": p.accrued_usd,
          "sim_hours": p.hours_held,
          "hedge": p.hedge_venue,
        }
      )
    for p in self._basis.values():
      out.append(
        {
          "coin": p.coin,
          "side": p.side,
          "kind": "basis",
          "notional_usd": p.notional_usd,
          "accrued_usd": 0.0,
          "sim_hours": (time.monotonic() - p.opened_at_mono) / 3600.0,
          "entry_basis_bps": p.entry_basis_bps,
        }
      )
    return out

  def _time_scale(self) -> float:
    # HARD RULE: never accelerate. Cheat modes are disabled.
    return 1.0

  async def _spread_vs_hl(self, coin: str, hl_hourly: float) -> tuple[str, float] | None:
    """Pick hedge venue maximizing |HL - hedge| hourly spread."""
    candidates: list[tuple[str, float, float]] = []
    okx = await self._okx.funding_for(coin)
    if okx is not None:
      sp = hl_hourly - okx.funding_hourly
      candidates.append(("okx", okx.funding_hourly, sp))
    if self._bitget is not None:
      bg = await self._bitget.funding_for(coin)
      if bg is not None:
        sp = hl_hourly - bg.funding_hourly
        candidates.append(("bitget", bg.funding_hourly, sp))
    if not candidates:
      return None
    venue, _rate, spread = max(candidates, key=lambda r: abs(r[2]))
    return venue, spread

  def _round_trip_fee_usd(self, notional: float) -> float:
    fee_bps = float(getattr(self._config, "funding_fee_bps_per_leg", 1.5))
    # 2 venues × open+close = 4 legs
    return notional * (fee_bps / 10_000) * 4.0

  def _passes_fee_gate(self, notional: float, abs_spread_hourly: float) -> bool:
    edge_per_h = notional * abs_spread_hourly
    if edge_per_h <= 0:
      return False
    fees = self._round_trip_fee_usd(notional)
    max_be = float(getattr(self._config, "funding_max_breakeven_hours", 12.0))
    return (fees / edge_per_h) <= max_be

  async def tick(self) -> StrategyTickResult:
    result = StrategyTickResult()
    if not self.enabled:
      return result

    markets = await self._hl.fetch_markets()
    if not markets:
      result.messages.append("Hyperliquid fetch failed")
      return result
    by_coin = {m.coin: m for m in markets}

    if time.monotonic() - self._hedge_refresh_at > 60:
      self._okx.clear_cache()
      if self._bitget is not None:
        self._bitget.clear_cache()
      self._hedge_refresh_at = time.monotonic()

    # Funding first — largest real edge. Basis is secondary.
    await self._tick_funding_spread(by_coin, result)
    await self._tick_basis(by_coin, result)
    return result

  # ---------- basis snap (real-time, secondary) ----------

  async def _tick_basis(
    self, by_coin: dict[str, HyperliquidMarket], result: StrategyTickResult
  ) -> None:
    min_bps = float(getattr(self._config, "basis_min_bps", 12.0))
    exit_bps = float(getattr(self._config, "basis_exit_bps", 2.0))
    take_profit_bps = float(getattr(self._config, "basis_take_profit_bps", 4.0))
    min_oi = float(getattr(self._config, "basis_min_oi_usd", 5_000_000))
    max_basis = float(getattr(self._config, "basis_max_bps", 40.0))
    max_pos = int(getattr(self._config, "basis_max_positions", 1))
    notional = float(getattr(self._config, "funding_notional_usd", 250))
    fee_bps = float(getattr(self._config, "funding_fee_bps_per_leg", 1.5))

    for coin in list(self._basis.keys()):
      pos = self._basis[coin]
      m = by_coin.get(coin)
      if m is None or m.oracle_px <= 0:
        continue
      basis = (m.mark_px - m.oracle_px) / m.oracle_px * 10_000
      if pos.side == "short":
        captured = pos.entry_basis_bps - basis
        compressed = basis <= exit_bps
        adverse = basis > pos.entry_basis_bps + 15
      else:
        captured = basis - pos.entry_basis_bps
        compressed = basis >= -exit_bps
        adverse = basis < pos.entry_basis_bps - 15
      held_h = (time.monotonic() - pos.opened_at_mono) / 3600.0
      take_profit = captured >= take_profit_bps
      if compressed or take_profit or adverse or held_h > 24:
        reason = (
          "basis compressed"
          if compressed
          else (
            "take profit"
            if take_profit
            else ("adverse basis" if adverse else "max hold")
          )
        )
        net = await self._close_basis(pos, m, reason)
        result.closed.append((f"basis:{coin}", net))
        result.messages.append(
          f"BASIS CLOSE {coin} {pos.side} net=${net:+.4f} "
          f"(entry {pos.entry_basis_bps:+.1f}bps → now {basis:+.1f}bps, "
          f"captured {captured:+.1f}bps, {reason})"
        )

    if len(self._basis) >= max_pos:
      return

    cands: list[tuple[float, HyperliquidMarket, float]] = []
    for m in by_coin.values():
      if m.coin in BASIS_BLACKLIST or m.coin in self._basis or m.coin in self._carry:
        continue
      if m.oracle_px <= 0 or m.open_interest_usd < min_oi:
        continue
      basis = (m.mark_px - m.oracle_px) / m.oracle_px * 10_000
      if abs(basis) < min_bps or abs(basis) > max_basis:
        continue
      # Require funding to agree with the basis trade (avoids pure oracle fiction).
      if basis > 0 and m.funding_hourly <= 0:
        continue
      if basis < 0 and m.funding_hourly >= 0:
        continue
      score = abs(basis) * (m.open_interest_usd ** 0.2)
      cands.append((score, m, basis))
    cands.sort(key=lambda r: r[0], reverse=True)

    for _score, m, basis in cands:
      if len(self._basis) >= max_pos:
        break
      balance = await self._ledger.get_balance()
      size = min(notional, balance * float(getattr(self._config, "funding_max_capital_pct", 0.5)))
      if size < 50:
        break
      side = "short" if basis > 0 else "long"
      open_fee = size * (fee_bps / 10_000)
      pos = BasisPosition(
        id=str(uuid.uuid4()),
        coin=m.coin,
        side=side,
        notional_usd=size,
        entry_mark=m.mark_px,
        entry_oracle=m.oracle_px,
        entry_basis_bps=basis,
        opened_at_mono=time.monotonic(),
        open_fee_usd=open_fee,
      )
      self._basis[m.coin] = pos
      result.opened.append(m.coin)
      result.messages.append(
        f"BASIS OPEN {m.coin} {side} ${size:.0f} @ {basis:+.1f}bps "
        f"(mark {m.mark_px:.4g} vs oracle {m.oracle_px:.4g}, "
        f"reserved fee ${open_fee:.3f})"
      )

  async def _close_basis(
    self, pos: BasisPosition, m: HyperliquidMarket, reason: str
  ) -> float:
    fee_bps = float(getattr(self._config, "funding_fee_bps_per_leg", 1.5))
    close_fee = pos.notional_usd * (fee_bps / 10_000)
    basis_now = (m.mark_px - m.oracle_px) / m.oracle_px * 10_000 if m.oracle_px else 0
    if pos.side == "short":
      gross = pos.notional_usd * (pos.entry_basis_bps - basis_now) / 10_000
    else:
      gross = pos.notional_usd * (basis_now - pos.entry_basis_bps) / 10_000
    haircut = abs(gross) * 0.20
    total_fees = pos.open_fee_usd + close_fee + haircut
    net = gross - total_fees
    await self._book(
      pos.id,
      pos.coin,
      pos.side,
      pos.notional_usd,
      m.mark_px,
      gross,
      total_fees,
      net,
      f"{pos.coin}-BASIS close ({reason})",
    )
    self._basis.pop(pos.coin, None)
    return net

  # ---------- HL vs CEX funding spread (wall-clock) ----------

  async def _tick_funding_spread(
    self, by_coin: dict[str, HyperliquidMarket], result: StrategyTickResult
  ) -> None:
    min_spread = float(getattr(self._config, "funding_min_spread_bps_hourly", 0.40))
    exit_spread = float(getattr(self._config, "funding_exit_bps_hourly", 0.15))
    min_oi = float(getattr(self._config, "funding_min_oi_usd", 5_000_000))
    max_pos = int(getattr(self._config, "funding_max_positions", 2))
    notional = float(getattr(self._config, "funding_notional_usd", 400))
    fee_bps = float(getattr(self._config, "funding_fee_bps_per_leg", 1.5))
    open_fee_mult = 2.0
    max_hold = float(getattr(self._config, "funding_max_hold_hours", 72))

    for coin in list(self._carry.keys()):
      pos = self._carry[coin]
      m = by_coin.get(coin)
      if m is None:
        continue
      hedge = await self._spread_vs_hl(coin, m.funding_hourly)
      if hedge is None:
        continue
      venue, spread_h = hedge
      signed = spread_h if pos.side == "short_hl" else -spread_h
      now = time.monotonic()
      dt_h = max(0.0, (now - pos.last_accrual_mono) * self._time_scale() / 3600.0)
      accrued = pos.notional_usd * signed * dt_h
      pos.accrued_usd += accrued
      pos.hours_held += dt_h
      pos.last_accrual_mono = now
      pos.hedge_venue = venue

      pending = pos.accrued_usd - pos.booked_accrual_usd
      book_hours = float(getattr(self._config, "funding_accrual_book_hours", 0.25))
      if pending >= max(0.001, pos.notional_usd * 5e-6) and (
        pos.hours_held - getattr(pos, "_booked_hours", 0.0) >= book_hours
        or pending >= 0.01
      ):
        await self._book(
          pos.id,
          coin,
          pos.side,
          pos.notional_usd,
          m.mark_px,
          pending,
          0.0,
          pending,
          f"{coin}-SPREAD funding",
        )
        pos.booked_accrual_usd = pos.accrued_usd
        pos._booked_hours = pos.hours_held  # type: ignore[attr-defined]
        result.messages.append(
          f"SPREAD ACCRUE {coin} ${pending:+.4f} (total ${pos.accrued_usd:+.4f}, "
          f"spread={(spread_h*1e4):+.2f}bps/h vs {venue}, {pos.hours_held:.2f}h real)"
        )

      edge = spread_h * 10_000 if pos.side == "short_hl" else -spread_h * 10_000
      # Flatten if edge dies or fee gate would reject a new entry (don't bleed fees).
      weak = edge < exit_spread or not self._passes_fee_gate(
        pos.notional_usd, max(abs(spread_h), 1e-12)
      )
      if weak or pos.hours_held >= max_hold:
        reason = (
          "spread compressed"
          if edge < exit_spread
          else ("edge below fee gate" if weak else "max hold")
        )
        net = await self._close_carry(pos, m, reason)
        result.closed.append((f"spread:{coin}", net))
        result.messages.append(
          f"SPREAD CLOSE {coin} net=${net:+.4f} ({reason}, edge={edge:.2f}bps/h)"
        )

    if len(self._carry) >= max_pos:
      return

    cands: list[tuple[float, HyperliquidMarket, float, str]] = []
    for coin in WATCH_COINS:
      m = by_coin.get(coin)
      if m is None or m.coin in self._carry or m.coin in self._basis:
        continue
      if m.open_interest_usd < min_oi:
        continue
      hedge = await self._spread_vs_hl(coin, m.funding_hourly)
      if hedge is None:
        continue
      venue, spread_h = hedge
      if abs(spread_h) * 10_000 < min_spread:
        continue
      if not self._passes_fee_gate(notional, abs(spread_h)):
        continue
      score = abs(spread_h) * (m.open_interest_usd ** 0.15)
      cands.append((score, m, spread_h, venue))
    cands.sort(key=lambda r: r[0], reverse=True)

    for _score, m, spread_h, venue in cands:
      if len(self._carry) >= max_pos:
        break
      balance = await self._ledger.get_balance()
      # Scale size with edge strength (still capped).
      edge_bps = abs(spread_h) * 10_000
      size_mult = 1.0 + min(1.0, (edge_bps - min_spread) / max(min_spread, 0.01))
      size = min(
        notional * size_mult,
        balance * float(getattr(self._config, "funding_max_capital_pct", 0.5)),
      )
      if size < 50:
        break
      if not self._passes_fee_gate(size, abs(spread_h)):
        continue
      side = "short_hl" if spread_h > 0 else "long_hl"
      open_fee = size * (fee_bps / 10_000) * open_fee_mult
      pos = CarryPosition(
        id=str(uuid.uuid4()),
        coin=m.coin,
        side=side,
        notional_usd=size,
        entry_mark=m.mark_px,
        entry_spread_hourly=spread_h,
        opened_at_mono=time.monotonic(),
        last_accrual_mono=time.monotonic(),
        hedge_venue=venue,
        open_fee_usd=open_fee,
      )
      self._carry[m.coin] = pos
      # Open fees are reserved on the position and settled on close so the
      # cash book isn't fake-red before wall-clock accrual can catch up.
      result.opened.append(m.coin)
      be_h = self._round_trip_fee_usd(size) / (size * abs(spread_h))
      result.messages.append(
        f"SPREAD OPEN {m.coin} {side} ${size:.0f} @ "
        f"{spread_h*1e4:+.2f}bps/h (HL {m.funding_bps_hourly:+.2f} vs {venue}, "
        f"BE≈{be_h:.1f}h wall-clock, reserved open fee ${open_fee:.3f})"
      )

  async def _close_carry(
    self, pos: CarryPosition, m: HyperliquidMarket, reason: str
  ) -> float:
    fee_bps = float(getattr(self._config, "funding_fee_bps_per_leg", 1.5))
    close_fee = pos.notional_usd * (fee_bps / 10_000) * 2.0
    pending = pos.accrued_usd - pos.booked_accrual_usd
    total_fees = pos.open_fee_usd + close_fee
    net = pending - total_fees
    await self._book(
      pos.id,
      pos.coin,
      pos.side,
      pos.notional_usd,
      m.mark_px,
      pending,
      total_fees,
      net,
      f"{pos.coin}-SPREAD close ({reason})",
    )
    self._carry.pop(pos.coin, None)
    return net

  async def _book(
    self,
    opportunity_id: str,
    coin: str,
    side: str,
    notional: float,
    mark: float,
    gross: float,
    fees: float,
    net: float,
    label: str,
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
      buy_price_usd=mark,
      sell_price_usd=mark,
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
    if not self._carry and not self._basis:
      return None
    parts = []
    for p in self._carry.values():
      parts.append(
        f"{p.coin}/spread ${p.accrued_usd:+.4f} ({p.hours_held:.2f}h vs {p.hedge_venue})"
      )
    for p in self._basis.values():
      parts.append(f"{p.coin}/basis entry {p.entry_basis_bps:+.1f}bps")
    return "Open: " + ", ".join(parts)
