from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Callable, Optional

from src.config import AppConfig, EnvSettings, load_config, resolve_db_path
from src.cost_model import CostModel
from src.data.dexscreener import DexScreenerClient
from src.data.oneinch import OneInchClient
from src.evaluator import TradeEvaluator
from src.ledger import Ledger
from src.models import Opportunity, OpportunityStatus, PaperTrade
from src.scanner import OpportunityScanner
from src.simulator import PaperExecutor


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

    self._dexscreener = DexScreenerClient(api_key=env.dexscreener_api_key)
    self._oneinch = OneInchClient(api_key=env.oneinch_api_key)
    self._scanner = OpportunityScanner(config, self._dexscreener)
    self._cost_model = CostModel(config)
    self._evaluator = TradeEvaluator(config, self._cost_model, self._oneinch)
    self._executor = PaperExecutor(ledger)
    self._running = False

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
    return cls(cfg, environment, ledger, on_event)

  async def close(self) -> None:
    await self._ledger.close()

  async def run_forever(self) -> None:
    self._running = True
    self._emit(
      f"Engine started | capital=${self._config.starting_capital_usd:,.2f} | "
      f"chains={', '.join(self._config.chains)} | "
      f"1inch={'enabled' if self._oneinch.enabled else 'AMM fallback'}"
    )
    while self._running:
      await self.run_once()
      await asyncio.sleep(self._config.scan_interval_sec)

  def stop(self) -> None:
    self._running = False

  async def run_once(self) -> None:
    opportunities = await self._scanner.scan_all()
    if not opportunities:
      summaries = await self._scanner.scan_market_summary()
      if summaries:
        best = max(summaries, key=lambda s: s.gross_spread_bps)
        self._emit(
          f"[{self._timestamp()}] No tradeable opportunities "
          f"(need {self._config.min_gross_spread_bps:.0f} bps) | "
          f"best: {best.chain.upper()} {best.pair_label} "
          f"{best.gross_spread_bps:.1f} bps ({best.buy_dex} -> {best.sell_dex})"
        )
        for spread in summaries:
          if spread is not best:
            self._emit(
              f"  {spread.chain.upper():<9} {spread.pair_label:<10} "
              f"{spread.gross_spread_bps:.1f} bps ({spread.buy_dex} -> {spread.sell_dex})"
            )
      else:
        self._emit(
          f"[{self._timestamp()}] No spreads found — "
          f"not enough liquid pools on watched DEXes"
        )
      return

    balance = await self._ledger.get_balance()
    for opportunity in opportunities:
      await self._process_opportunity(opportunity, balance)
      balance = await self._ledger.get_balance()

  async def _process_opportunity(self, opportunity: Opportunity, balance: float) -> None:
    chain_label = opportunity.chain.upper()
    spread = opportunity.gross_spread_bps
    header = (
      f"[{self._timestamp()}] {chain_label:<9} {opportunity.pair_label:<10} "
      f"buy={opportunity.buy_dex}@{opportunity.buy_price_usd:.2f} "
      f"sell={opportunity.sell_dex}@{opportunity.sell_price_usd:.2f} "
      f"spread={spread:.0f}bps"
    )

    evaluation = await self._evaluator.evaluate(opportunity, balance)
    if evaluation is None:
      rejected = TradeEvaluator.mark_rejected(opportunity, "Below net profit threshold")
      await self._ledger.save_opportunity(rejected)
      self._emit(f"{header}  SKIP (below net threshold)")
      return

    if not self._evaluator.passes_mev_gate():
      missed = TradeEvaluator.mark_missed(opportunity)
      await self._ledger.save_opportunity(missed)
      self._emit(f"{header}  MISSED (MEV)")
      return

    executed_opp = TradeEvaluator.mark_executed(opportunity)
    await self._ledger.save_opportunity(executed_opp)
    trade = await self._executor.execute(evaluation)
    self._emit(
      f"{header}  NET={trade.net_pnl_usd:+.2f}  EXECUTED  "
      f"balance=${trade.balance_after_usd:,.2f}"
    )

  def _emit(self, message: str) -> None:
    self._on_event(message)

  @staticmethod
  def _timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S")
