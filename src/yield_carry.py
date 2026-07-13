from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.config import AppConfig
from src.data.defillama import DefiLlamaYieldClient, YieldPool
from src.ledger import Ledger
from src.models import PaperTrade


@dataclass
class YieldPosition:
  id: str
  pool_id: str
  project: str
  chain: str
  symbol: str
  principal_usd: float
  apy_pct: float
  opened_at_mono: float
  last_accrual_mono: float
  accrued_usd: float = 0.0
  booked_usd: float = 0.0
  deposit_gas_usd: float = 0.0


@dataclass
class YieldTickResult:
  messages: list[str] = field(default_factory=list)


class StableYieldEngine:
  """
  Wall-clock USDC/USDT lending using live DefiLlama APYs.

  This is the honest baseline that works with real money: deposit stables into
  Aave/Compound/Fluid on Base/Arbitrum/Ethereum and earn supply interest.
  No time acceleration. Gas modeled once on deposit.
  """

  def __init__(
    self,
    config: AppConfig,
    ledger: Ledger,
    llama: DefiLlamaYieldClient,
  ) -> None:
    self._config = config
    self._ledger = ledger
    self._llama = llama
    self._pos: YieldPosition | None = None

  @property
  def enabled(self) -> bool:
    return bool(getattr(self._config, "yield_enabled", True))

  @property
  def open_positions(self) -> list[dict]:
    if self._pos is None:
      return []
    p = self._pos
    held_h = (time.monotonic() - p.opened_at_mono) / 3600.0
    return [
      {
        "coin": f"{p.symbol}",
        "side": "lend",
        "kind": "stable_yield",
        "notional_usd": p.principal_usd,
        "accrued_usd": p.accrued_usd,
        "sim_hours": held_h,
        "apy_pct": p.apy_pct,
        "venue": f"{p.project}/{p.chain}",
      }
    ]

  async def tick(self) -> YieldTickResult:
    result = YieldTickResult()
    if not self.enabled:
      return result

    min_tvl = float(getattr(self._config, "yield_min_tvl_usd", 20_000_000))
    prefer = list(getattr(self._config, "yield_prefer_chains", ["Base", "Arbitrum"]))
    pools = await self._llama.fetch_stable_pools(min_tvl_usd=min_tvl, prefer_chains=prefer)
    if not pools:
      result.messages.append("Yield: no liquid stable pools from DefiLlama")
      return result

    best = pools[0]
    min_apy = float(getattr(self._config, "yield_min_apy_pct", 1.5))
    if best.honest_apy_pct < min_apy:
      result.messages.append(
        f"Yield: best pool {best.project}/{best.chain} {best.symbol} "
        f"APY {best.honest_apy_pct:.2f}% below min {min_apy:.2f}%"
      )
      return result

    if self._pos is None:
      await self._open(best, result)
    else:
      # Refresh live APY on same pool when possible.
      match = next((p for p in pools if p.pool_id == self._pos.pool_id), None)
      if match is not None:
        self._pos.apy_pct = match.honest_apy_pct
      await self._accrue(result)
      # Optional migrate if another pool is meaningfully better after gas.
      await self._maybe_migrate(best, pools, result)
    return result

  async def _open(self, pool: YieldPool, result: YieldTickResult) -> None:
    balance = await self._ledger.get_balance()
    pct = float(getattr(self._config, "yield_capital_pct", 0.85))
    size = balance * pct
    min_size = float(getattr(self._config, "yield_min_deposit_usd", 100))
    if size < min_size:
      result.messages.append(f"Yield: balance ${balance:.2f} too small to deposit")
      return

    gas = self._deposit_gas(pool.chain)
    # Live gas is paid in ETH/native, not from the USDC deposit. Log it, don't
    # debit the USD lending book (that was the fake "always red" bleed).
    pos = YieldPosition(
      id=str(uuid.uuid4()),
      pool_id=pool.pool_id,
      project=pool.project,
      chain=pool.chain,
      symbol=pool.symbol,
      principal_usd=size,
      apy_pct=pool.honest_apy_pct,
      opened_at_mono=time.monotonic(),
      last_accrual_mono=time.monotonic(),
      deposit_gas_usd=gas,
    )
    self._pos = pos
    day = pos.principal_usd * (pos.apy_pct / 100.0) / 365.0
    result.messages.append(
      f"YIELD OPEN {pool.project}/{pool.chain} {pool.symbol} "
      f"${pos.principal_usd:.0f} @ {pos.apy_pct:.2f}% APR "
      f"(~${day:.4f}/day wall-clock, est. native gas ~${gas:.2f}, "
      f"tvl ${pool.tvl_usd/1e6:.0f}M)"
    )

  def _deposit_gas(self, chain: str) -> float:
    # Honest one-time deposit cost by chain (approx mainnet / L2).
    c = chain.lower()
    if c == "ethereum":
      return float(getattr(self._config, "yield_gas_ethereum_usd", 3.0))
    if c == "arbitrum":
      return float(getattr(self._config, "yield_gas_arbitrum_usd", 0.15))
    if c == "base":
      return float(getattr(self._config, "yield_gas_base_usd", 0.05))
    return 0.5

  async def _accrue(self, result: YieldTickResult) -> None:
    pos = self._pos
    if pos is None:
      return
    now = time.monotonic()
    dt_h = max(0.0, (now - pos.last_accrual_mono) / 3600.0)
    pos.last_accrual_mono = now
    # Continuous compounding approximation: APR / 365 / 24 per hour
    rate_h = (pos.apy_pct / 100.0) / 365.0 / 24.0
    earned = pos.principal_usd * rate_h * dt_h
    pos.accrued_usd += earned

    pending = pos.accrued_usd - pos.booked_usd
    book_every = float(getattr(self._config, "yield_accrual_book_hours", 0.05))
    held_h = (now - pos.opened_at_mono) / 3600.0
    # Book often so the dashboard reflects live wall-clock interest.
    min_book = float(getattr(self._config, "yield_min_book_usd", 0.0001))
    if pending >= min_book and (
      held_h - getattr(pos, "_booked_hours", 0.0) >= book_every or pending >= 0.005
    ):
      await self._book(
        pos.id,
        f"{pos.symbol}-YIELD interest ({pos.project}/{pos.chain})",
        pos.chain,
        pos.principal_usd,
        pending,
        0.0,
        pending,
      )
      pos.booked_usd = pos.accrued_usd
      pos._booked_hours = held_h  # type: ignore[attr-defined]
      # Interest compounds into principal for honest continuous yield.
      pos.principal_usd += pending
      result.messages.append(
        f"YIELD ACCRUE {pos.symbol} ${pending:+.4f} "
        f"(total ${pos.accrued_usd:+.4f}, {pos.apy_pct:.2f}% APR, {held_h:.2f}h real)"
      )

  async def _maybe_migrate(
    self, best: YieldPool, pools: list[YieldPool], result: YieldTickResult
  ) -> None:
    pos = self._pos
    if pos is None:
      return
    if best.pool_id == pos.pool_id:
      return
    # Only migrate if APY edge covers gas twice (exit+enter).
    edge = best.honest_apy_pct - pos.apy_pct
    min_edge = float(getattr(self._config, "yield_migrate_min_apy_pct", 1.0))
    if edge < min_edge:
      return
    gas = self._deposit_gas(best.chain) + self._deposit_gas(pos.chain)
    # Approximate days to recover migration gas from extra APR.
    extra_day = pos.principal_usd * (edge / 100.0) / 365.0
    if extra_day <= 0 or gas / extra_day > 14:
      return
    # Close old interest, then switch pool. Native gas estimated but not
    # debited from the USDC book (paid in ETH on live).
    await self._accrue(result)
    pending = pos.accrued_usd - pos.booked_usd
    if pending > 0:
      await self._book(
        pos.id,
        f"{pos.symbol}-YIELD interest final",
        pos.chain,
        pos.principal_usd,
        pending,
        0.0,
        pending,
      )
      pos.principal_usd += pending
      pos.booked_usd = pos.accrued_usd
    old = f"{pos.project}/{pos.chain}"
    pos.pool_id = best.pool_id
    pos.project = best.project
    pos.chain = best.chain
    pos.symbol = best.symbol
    pos.apy_pct = best.honest_apy_pct
    pos.opened_at_mono = time.monotonic()
    pos.last_accrual_mono = time.monotonic()
    pos.accrued_usd = 0.0
    pos.booked_usd = 0.0
    pos.deposit_gas_usd = gas
    result.messages.append(
      f"YIELD MIGRATE {old} → {best.project}/{best.chain} "
      f"{best.symbol} @ {best.honest_apy_pct:.2f}% (+{edge:.2f}pp, "
      f"est. native gas ~${gas:.2f})"
    )

  async def _book(
    self,
    opportunity_id: str,
    label: str,
    chain: str,
    notional: float,
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
      chain=chain.lower(),
      pair_label=label,
      buy_dex="defillama",
      sell_dex="lend",
      trade_size_usd=notional,
      buy_price_usd=1.0,
      sell_price_usd=1.0,
      gross_pnl_usd=gross,
      fees_usd=fees,
      gas_cost_usd=fees if fees > 0 and gross < 0 else 0.0,
      mev_haircut_usd=0.0,
      net_pnl_usd=net,
      balance_after_usd=new_balance,
    )
    await self._ledger.save_trade(trade)
    await self._ledger.maybe_record_snapshot()

  async def status_line(self) -> str | None:
    if self._pos is None:
      return None
    p = self._pos
    return (
      f"Yield: {p.project}/{p.chain} {p.symbol} ${p.principal_usd:.0f} "
      f"@ {p.apy_pct:.2f}% accrued ${p.accrued_usd:+.4f}"
    )
