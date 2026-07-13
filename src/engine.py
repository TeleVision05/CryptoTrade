from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Callable, Optional

from src.activity import ActivityBuffer
from src.config import AppConfig, EnvSettings, load_config, resolve_db_path
from src.cost_model import CostModel
from src.data.dexscreener import DexScreenerClient
from src.data.bitget import BitgetClient
from src.data.defillama import DefiLlamaYieldClient
from src.data.hyperliquid import HyperliquidClient
from src.data.okx import OkxClient
from src.data.oneinch import OneInchClient
from src.evaluator import TradeEvaluator
from src.execution import create_executor
from src.funding import HonestCarryEngine
from src.ledger import Ledger
from src.mev import MevModel
from src.models import Opportunity
from src.quoting.onchain_quoter import OnchainPoolQuoter
from src.quoting.pool_fee_enricher import PoolFeeEnricher
from src.risk import RiskManager
from src.scanner import OpportunityScanner
from src.triangular import TriangularScanner
from src.funding_harvest import FundingHarvestEngine
from src.momentum import MomentumEngine
from src.yield_carry import StableYieldEngine


class ArbitrageEngine:
  def __init__(
    self,
    config: AppConfig,
    env: EnvSettings,
    ledger: Ledger,
    on_event: Optional[Callable[[str], None]] = None,
  ) -> None:
    self._config = config
    self._env = env
    self._ledger = ledger
    self._on_event = on_event or print
    self.activity = ActivityBuffer()

    self._dexscreener = DexScreenerClient(api_key=env.dexscreener_api_key)
    self._oneinch = OneInchClient(api_key=env.oneinch_api_key)
    self._hyperliquid = HyperliquidClient()
    self._okx = OkxClient()
    self._bitget = BitgetClient()
    self._llama = DefiLlamaYieldClient()
    self._onchain = OnchainPoolQuoter(env)
    self._fee_enricher = PoolFeeEnricher(env)
    self._scanner = OpportunityScanner(
      config, self._dexscreener, self._fee_enricher, onchain_quoter=self._onchain
    )
    self._triangular = TriangularScanner(config, self._dexscreener)
    self._yield = StableYieldEngine(config, ledger, self._llama)
    self._harvest = FundingHarvestEngine(config, ledger, self._hyperliquid)
    self._momentum = MomentumEngine(config, ledger, self._hyperliquid)
    self._funding = HonestCarryEngine(
      config, ledger, self._hyperliquid, self._okx, self._bitget
    )
    self._mev = MevModel(config)
    self._cost_model = CostModel(config, self._mev)
    self._evaluator = TradeEvaluator(
      config, self._cost_model, self._oneinch, self._mev, onchain_quoter=self._onchain
    )
    self._risk = RiskManager(config)
    self._executor = create_executor(
      config, env, ledger, self._oneinch, self._cost_model, onchain_quoter=self._onchain
    )
    self._running = False
    self._clients_open = False
    self._route_cooldown_until: dict[str, float] = {}
    self._spatial_every_n = 8
    self._cycle = 0

  @classmethod
  async def create(
    cls,
    config: AppConfig | None = None,
    env: EnvSettings | None = None,
    on_event: Optional[Callable[[str], None]] = None,
  ) -> "ArbitrageEngine":
    cfg = config or load_config()
    environment = env or EnvSettings()
    ledger = Ledger(resolve_db_path(cfg), cfg)
    await ledger.connect()
    engine = cls(cfg, environment, ledger, on_event)
    await engine._open_clients()
    balance = await ledger.get_balance()
    engine._risk.sync_balance(balance)
    return engine

  async def _open_clients(self) -> None:
    if not self._clients_open:
      await self._dexscreener.open()
      await self._oneinch.open()
      await self._hyperliquid.open()
      await self._okx.open()
      await self._bitget.open()
      await self._llama.open()
      self._clients_open = True

  async def close(self) -> None:
    await self._dexscreener.close()
    await self._oneinch.close()
    await self._hyperliquid.close()
    await self._okx.close()
    await self._bitget.close()
    await self._llama.close()
    await self._ledger.close()
    self._clients_open = False

  @property
  def oneinch_ready(self) -> bool:
    return self._oneinch.enabled

  @property
  def rpc_ready(self) -> bool:
    return bool(self._env.base_rpc_url and self._env.arbitrum_rpc_url)

  async def run_forever(self) -> None:
    self._running = True
    mode = self._config.execution_mode.upper()
    oneinch_status = "ready" if self._oneinch.enabled else "MISSING KEY"
    rpc_status = "ready" if self.rpc_ready else "MISSING RPC"
    self._emit(
      f"Engine started | mode={mode} | capital=${self._config.starting_capital_usd:,.2f} | "
      f"chains={', '.join(self._config.chains)} | 1inch={oneinch_status} | rpc={rpc_status}"
    )
    if self._config.execution_mode == "simulate":
      quote_mode = self._config.simulate_quote_mode
      if quote_mode == "oneinch" and not self._oneinch.enabled:
        self._emit("WARNING: simulate_quote_mode=oneinch requires ONEINCH_API_KEY")
      else:
        self._emit(
          f"Online dry-run active (no broadcast) | quote={quote_mode} | "
          f"from={self._env.resolve_simulate_from_address()}"
        )
      if self._config.yield_enabled:
        self._emit(
          f"Stable yield ON | live DefiLlama Aave/Fluid/Compound APY | "
          f"capital={self._config.yield_capital_pct:.0%} | wall-clock ONLY"
        )
      if getattr(self._config, "harvest_enabled", False):
        self._emit(
          f"Funding harvest ON | extreme HL funding | "
          f"min={self._config.harvest_min_bps_hourly:.2f}bps/h | "
          f"notional=${self._config.harvest_notional_usd:.0f} | HIGH RISK"
        )
      if getattr(self._config, "momentum_enabled", False):
        self._emit(
          f"Momentum ON | HL trend+funding perps | "
          f"notional=${self._config.momentum_notional_usd:.0f} | "
          f"stop={self._config.momentum_stop_pct:.1%}/take={self._config.momentum_take_pct:.1%} | "
          f"HIGHER RISK"
        )
      if self._config.funding_enabled:
        self._emit(
          f"Honest carry ON | HL↔OKX/Bitget funding (fee-gated) + basis TP | "
          f"notional=${self._config.funding_notional_usd:.0f} | "
          f"min_spread={self._config.funding_min_spread_bps_hourly:.2f}bps/h | "
          f"time_scale=1.0 (wall-clock ONLY)"
        )
    if self._config.execution_mode == "live":
      if not self._config.live_enabled:
        self._emit(
          "LIVE LOCKED (live_enabled=false) — will not broadcast. "
          "Prove profit in simulate with 1inch first."
        )
      elif not self._env.wallet_private_key:
        self._emit("WARNING: live mode requires WALLET_PRIVATE_KEY in .env")
      elif not self._oneinch.enabled:
        self._emit("WARNING: live mode requires ONEINCH_API_KEY in .env")
      else:
        self._emit(f"LIVE mode | max trade ${self._config.live_max_trade_usd:.2f}")

    while self._running:
      try:
        await self.run_once()
      except Exception as exc:
        self._emit(f"[{self._timestamp()}] Scan error: {exc}")
      await asyncio.sleep(self._config.scan_interval_sec)

  def stop(self) -> None:
    self._running = False

  async def run_once(self) -> None:
    self._cycle += 1
    self.activity.push(
      "scan",
      "Scanning yield + markets…",
      outcome="checking",
    )
    self._emit(f"[{self._timestamp()}] Scanning markets…")

    # --- Primary: stablecoin lending (real APY, works with real money) ---
    if self._yield.enabled:
      y = await self._yield.tick()
      for msg in y.messages:
        self._emit(f"[{self._timestamp()}] {msg}")
        kind = "trade" if any(k in msg for k in ("OPEN", "ACCRUE", "MIGRATE")) else "scan"
        outcome = "executed" if kind == "trade" else "checking"
        self.activity.push(kind, msg, chain="defi", outcome=outcome)
      snap = await self._yield.status_line()
      if snap and not y.messages:
        self._emit(f"[{self._timestamp()}] {snap}")

    # --- Extreme funding harvest (fast accrual when rates are fat) ---
    if self._harvest.enabled:
      h = await self._harvest.tick()
      for msg in h.messages:
        self._emit(f"[{self._timestamp()}] {msg}")
        kind = "trade" if any(k in msg for k in ("OPEN", "CLOSE", "ACCRUE")) else "scan"
        outcome = "executed" if kind == "trade" else "checking"
        self.activity.push(kind, msg, chain="hyperliquid", outcome=outcome)
      snap = await self._harvest.status_line()
      if snap and not h.messages:
        self._emit(f"[{self._timestamp()}] {snap}")

    # --- Primary risk: HL momentum (trend + funding tilt) ---
    if self._momentum.enabled:
      mom = await self._momentum.tick()
      for msg in mom.messages:
        self._emit(f"[{self._timestamp()}] {msg}")
        kind = "trade" if any(k in msg for k in ("OPEN", "CLOSE")) else "scan"
        outcome = "executed" if kind == "trade" else "checking"
        self.activity.push(kind, msg, chain="hyperliquid", outcome=outcome)
      snap = await self._momentum.status_line()
      if snap and not mom.messages:
        self._emit(f"[{self._timestamp()}] {snap}")

    # --- Optional: Hyperliquid funding carry ---
    if self._funding.enabled:
      funding = await self._funding.tick()
      for msg in funding.messages:
        self._emit(f"[{self._timestamp()}] {msg}")
        kind = "trade" if any(k in msg for k in ("CLOSE", "ACCRUE", "OPEN")) else "scan"
        outcome = "executed" if kind == "trade" else "checking"
        self.activity.push(kind, msg, chain="hyperliquid", outcome=outcome)
      snap = await self._funding.status_line()
      if snap and not funding.messages:
        self._emit(f"[{self._timestamp()}] {snap}")

    # Spatial DEX arb is secondary — expensive RPC, rare edge. Run every N cycles.
    if not getattr(self._config, "spatial_enabled", True):
      return
    if self._cycle % self._spatial_every_n != 1:
      return

    import time

    # Fast path: re-quote hot near-miss routes every tick (~1–2s).
    hot = await self._scanner.requote_hot_routes(probe_usd=25.0)
    do_full = (time.monotonic() - self._scanner._last_full_hunt_at) > 20.0 or not self._scanner._hot_routes

    executable: list[Opportunity] = list(hot)
    if do_full:
      fresh = await self._scanner.scan_executable_edges(probe_usd=25.0)
      self._scanner._last_full_hunt_at = time.monotonic()
      seen = {
        (o.buy_pair.pair_address.lower(), o.sell_pair.pair_address.lower())
        for o in executable
      }
      for opp in fresh:
        key = (
          opp.buy_pair.pair_address.lower(),
          opp.sell_pair.pair_address.lower(),
        )
        if key not in seen:
          executable.append(opp)
          seen.add(key)

    if executable:
      best = max(executable, key=lambda o: o.gross_spread_bps)
      self._emit(
        f"[{self._timestamp()}] On-chain hunter: {len(executable)} near-edge route(s), "
        f"best {best.pair_label} {best.gross_spread_bps:+.1f}bps "
        f"{best.buy_dex}->{best.sell_dex}"
      )

    opportunities = executable
    if not opportunities:
      # Don't spam DexScreener fallback every cycle — funding is the main book.
      return

    # Prefer best on-chain edge first; only snipe the closest few (speed = edge capture).
    opportunities = sorted(opportunities, key=lambda o: -o.gross_spread_bps)
    # Keep near-misses the hunter already measured (hot path uses ≥ -2.5bps).
    opportunities = [o for o in opportunities if o.gross_spread_bps >= -2.5][:3]
    if not opportunities:
      self.activity.push(
        "idle",
        "Near-edge DEX routes cooled off — funding carry still running",
        outcome="not_worth",
      )
      return

    self.activity.push(
      "found",
      f"Found {len(opportunities)} DEX candidate"
      f"{'s' if len(opportunities) != 1 else ''} — checking if worth it…",
      outcome="checking",
    )

    balance = await self._ledger.get_balance()
    self._risk.sync_balance(balance)

    for opportunity in opportunities:
      chain_balance = self._risk.available_capital(opportunity.chain, balance)
      await self._process_opportunity(opportunity, chain_balance)
      balance = await self._ledger.get_balance()

  async def _process_opportunity(self, opportunity: Opportunity, balance: float) -> None:
    chain_label = opportunity.chain.upper()
    spread = opportunity.gross_spread_bps
    header = (
      f"[{self._timestamp()}] {chain_label:<9} {opportunity.pair_label:<14} "
      f"[{opportunity.strategy}] "
      f"buy={opportunity.buy_dex}@{opportunity.buy_price_usd:.2f} "
      f"sell={opportunity.sell_dex}@{opportunity.sell_price_usd:.2f} "
      f"spread={spread:.0f}bps"
    )

    self.activity.push(
      "found",
      f"Oh — spotted {opportunity.pair_label} on {chain_label}",
      chain=opportunity.chain,
      pair=opportunity.pair_label,
      spread_bps=spread,
      detail=(
        f"Buy {opportunity.buy_dex} @ ${opportunity.buy_price_usd:.4g} → "
        f"sell {opportunity.sell_dex} @ ${opportunity.sell_price_usd:.4g}"
      ),
      outcome="checking",
    )

    route_key = self._route_key(opportunity)
    cooldown_until = self._route_cooldown_until.get(route_key, 0.0)
    if time.monotonic() < cooldown_until:
      remaining = int(cooldown_until - time.monotonic())
      rejected = TradeEvaluator.mark_rejected(
        opportunity, f"Route cooldown ({remaining}s left)"
      )
      await self._ledger.save_opportunity(rejected)
      self.activity.push(
        "skip",
        "Already checked this route — cooling down",
        chain=opportunity.chain,
        pair=opportunity.pair_label,
        spread_bps=spread,
        detail=f"{remaining}s left before re-check",
        outcome="cooldown",
      )
      self._emit(f"{header}  SKIP: Route cooldown ({remaining}s)")
      return

    self.activity.push(
      "evaluate",
      f"Checking quotes on {opportunity.pair_label}…",
      chain=opportunity.chain,
      pair=opportunity.pair_label,
      spread_bps=spread,
      detail=f"Mode={self._config.simulate_quote_mode} · size up to ${balance * self._config.max_position_pct:.0f}",
      outcome="checking",
    )

    evaluation_outcome = await self._evaluator.evaluate(opportunity, balance)
    if evaluation_outcome.evaluation is None:
      reason = evaluation_outcome.rejection_reason or "Below net profit threshold"
      rejected = TradeEvaluator.mark_rejected(opportunity, reason)
      await self._ledger.save_opportunity(rejected)
      eff = evaluation_outcome.effective_spread_bps
      eff_str = f"{eff:.0f}bps effective" if eff is not None else "n/a"
      self.activity.push(
        "skip",
        "Not worth it",
        chain=opportunity.chain,
        pair=opportunity.pair_label,
        spread_bps=spread,
        detail=(
          f"{reason} · spot {evaluation_outcome.spot_spread_bps:.0f}bps, "
          f"{eff_str}, fees ~{evaluation_outcome.fee_drag_bps:.0f}bps"
        ),
        outcome="not_worth",
      )
      self._emit(
        f"{header}  SKIP: {reason} "
        f"(spot {evaluation_outcome.spot_spread_bps:.0f}bps, {eff_str}, "
        f"fees ~{evaluation_outcome.fee_drag_bps:.0f}bps)"
      )
      return

    evaluation = evaluation_outcome.evaluation

    risk_block = self._risk.check_can_trade(balance, evaluation.trade_size_usd)
    if risk_block is not None:
      rejected = TradeEvaluator.mark_rejected(opportunity, risk_block)
      await self._ledger.save_opportunity(rejected)
      self.activity.push(
        "skip",
        "Risk gate blocked the trade",
        chain=opportunity.chain,
        pair=opportunity.pair_label,
        spread_bps=spread,
        detail=risk_block,
        outcome="not_worth",
      )
      self._emit(f"{header}  SKIP (risk: {risk_block})")
      return

    if not self._evaluator.passes_mev_gate(spread, evaluation.trade_size_usd):
      missed = TradeEvaluator.mark_missed(opportunity)
      await self._ledger.save_opportunity(missed)
      self.activity.push(
        "skip",
        "MEV would likely snatch this",
        chain=opportunity.chain,
        pair=opportunity.pair_label,
        spread_bps=spread,
        outcome="not_worth",
      )
      self._emit(f"{header}  MISSED (MEV)")
      return

    self.activity.push(
      "evaluate",
      f"Looks worth it — net ~${evaluation.net_pnl_usd:+.3f}",
      chain=opportunity.chain,
      pair=opportunity.pair_label,
      spread_bps=spread,
      detail=f"Size ${evaluation.trade_size_usd:.0f} · quote={evaluation.quote_source}",
      outcome="worth_it",
    )

    executed_opp = TradeEvaluator.mark_executed(opportunity)
    await self._ledger.save_opportunity(executed_opp)

    try:
      trade = await self._executor.execute(evaluation)
    except Exception as exc:
      rejected = TradeEvaluator.mark_rejected(opportunity, f"Execution failed: {exc}")
      await self._ledger.save_opportunity(rejected)
      self.activity.push(
        "error",
        "Execution failed",
        chain=opportunity.chain,
        pair=opportunity.pair_label,
        spread_bps=spread,
        detail=str(exc),
        outcome="not_worth",
      )
      self._emit(f"{header}  FAILED ({exc})")
      return

    self._route_cooldown_until[route_key] = (
      time.monotonic() + max(0, self._config.route_cooldown_sec)
    )
    mode_label = self._config.execution_mode.upper()
    self._risk.record_trade_result(trade.net_pnl_usd)
    self.activity.push(
      "trade",
      f"Booked trade · net ${trade.net_pnl_usd:+.3f}",
      chain=opportunity.chain,
      pair=opportunity.pair_label,
      spread_bps=spread,
      detail=f"Balance ${trade.balance_after_usd:,.2f} · {evaluation.quote_source}",
      outcome="executed",
    )
    self._emit(
      f"{header}  NET={trade.net_pnl_usd:+.2f}  {mode_label}  "
      f"balance=${trade.balance_after_usd:,.2f}  "
      f"quote={evaluation.quote_source}"
    )

  @staticmethod
  def _route_key(opportunity: Opportunity) -> str:
    buy = opportunity.buy_pair.pair_address.lower()
    sell = opportunity.sell_pair.pair_address.lower()
    return f"{opportunity.chain}:{buy}:{sell}"

  @property
  def risk_manager(self) -> RiskManager:
    return self._risk

  def _emit(self, message: str) -> None:
    self._on_event(message)

  @staticmethod
  def _timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S")
