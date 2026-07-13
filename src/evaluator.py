from __future__ import annotations

from dataclasses import dataclass

from src.config import AppConfig
from src.cost_model import CostModel
from src.data.oneinch import OneInchClient
from src.mev import MevModel
from src.models import Opportunity, OpportunityStatus, TradeEvaluation
from src.quoting.fees import fee_drag_bps
from src.quoting.onchain_quoter import OnchainPoolQuoter
from src.quoting.pool_quotes import apply_spread_decay, pool_round_trip_usd
from src.quoting.spot_estimator import effective_spread_bps, spot_estimated_sell_output
from src.quoting.triangular_quotes import triangular_round_trip_usd


@dataclass
class EvaluationOutcome:
  evaluation: TradeEvaluation | None
  rejection_reason: str | None = None
  spot_spread_bps: float = 0.0
  effective_spread_bps: float | None = None
  fee_drag_bps: float = 0.0
  best_net_pnl_usd: float | None = None


class TradeEvaluator:
  def __init__(
    self,
    config: AppConfig,
    cost_model: CostModel,
    oneinch: OneInchClient,
    mev_model: MevModel | None = None,
    onchain_quoter: OnchainPoolQuoter | None = None,
  ) -> None:
    self._config = config
    self._cost_model = cost_model
    self._oneinch = oneinch
    self._mev = mev_model or MevModel(config)
    self._onchain = onchain_quoter

  @property
  def uses_online_quotes(self) -> bool:
    return self._config.execution_mode in {"simulate", "live"}

  @property
  def uses_oneinch_quotes(self) -> bool:
    if self._config.execution_mode == "live":
      # Live still uses 1inch until direct-pool SwapRouter path ships.
      return True
    if self._config.execution_mode == "simulate":
      return self._config.simulate_quote_mode == "oneinch"
    return False

  @property
  def uses_onchain_quotes(self) -> bool:
    return (
      self._config.execution_mode == "simulate"
      and self._config.simulate_quote_mode == "onchain"
    )

  def candidate_sizes(
    self, available_capital: float, opportunity: Opportunity | None = None
  ) -> list[float]:
    fractions = [0.01, 0.025, 0.05, 0.10, 0.20, 0.30, self._config.max_position_pct]
    sizes = sorted({round(available_capital * frac, 2) for frac in fractions})
    if self._config.execution_mode == "live":
      sizes = [s for s in sizes if s <= self._config.live_max_trade_usd]
      if self._config.live_max_trade_usd not in sizes and self._config.live_max_trade_usd > 0:
        sizes.append(round(self._config.live_max_trade_usd, 2))
        sizes = sorted(set(sizes))
    # Edge often clears only at small size on L2 CLAMM pools.
    if self.uses_onchain_quotes:
      preferred = [5.0, 10.0, 15.0, 25.0, 50.0, 75.0, 100.0]
      if opportunity is not None and opportunity.gross_spread_bps >= 0:
        preferred = [5.0, 10.0, 15.0, 25.0, 50.0]
      sizes = [s for s in preferred if s <= available_capital * self._config.max_position_pct]
      if not sizes:
        sizes = [s for s in preferred if s <= available_capital][:3]
    return [size for size in sizes if size > 0]

  async def evaluate(
    self,
    opportunity: Opportunity,
    available_capital: float,
  ) -> EvaluationOutcome:
    if opportunity.strategy == "triangular":
      drag = opportunity.buy_pair.fee_bps + opportunity.sell_pair.fee_bps + 5.0
    else:
      drag = fee_drag_bps(opportunity.buy_pair, opportunity.sell_pair)

    if self.uses_oneinch_quotes and not self._oneinch.enabled:
      return EvaluationOutcome(
        evaluation=None,
        rejection_reason="ONEINCH_API_KEY required for simulate/live mode",
        spot_spread_bps=opportunity.gross_spread_bps,
        fee_drag_bps=drag,
      )

    best: TradeEvaluation | None = None
    best_effective_bps: float | None = None
    last_reason = "No viable trade size"

    sizes = self.candidate_sizes(available_capital, opportunity)
    # Parallelize on-chain size probes — fleeting edge dies in seconds.
    if self.uses_onchain_quotes and len(sizes) > 1:
      import asyncio

      results = await asyncio.gather(
        *[self._evaluate_size(opportunity, size) for size in sizes]
      )
    else:
      results = [
        await self._evaluate_size(opportunity, trade_size) for trade_size in sizes
      ]

    for evaluation, reason, effective_bps in results:
      if effective_bps is not None and (
        best_effective_bps is None or effective_bps > best_effective_bps
      ):
        best_effective_bps = effective_bps
      if evaluation is None:
        last_reason = reason or last_reason
        continue
      if best is None or evaluation.net_pnl_usd > best.net_pnl_usd:
        best = evaluation
        best_effective_bps = effective_bps

    if best is None or best.net_pnl_usd < self._config.min_net_profit_usd:
      return EvaluationOutcome(
        evaluation=None,
        rejection_reason=last_reason,
        spot_spread_bps=opportunity.gross_spread_bps,
        effective_spread_bps=best_effective_bps,
        fee_drag_bps=drag,
        best_net_pnl_usd=best.net_pnl_usd if best else None,
      )

    return EvaluationOutcome(
      evaluation=best,
      spot_spread_bps=opportunity.gross_spread_bps,
      effective_spread_bps=best_effective_bps,
      fee_drag_bps=drag,
      best_net_pnl_usd=best.net_pnl_usd,
    )

  async def _evaluate_size(
    self,
    opportunity: Opportunity,
    trade_size_usd: float,
  ) -> tuple[TradeEvaluation | None, str | None, float | None]:
    if self.uses_oneinch_quotes:
      return await self._evaluate_size_oneinch(opportunity, trade_size_usd)
    if self.uses_onchain_quotes:
      return await self._evaluate_size_onchain(opportunity, trade_size_usd)
    return await self._evaluate_size_local(opportunity, trade_size_usd)

  async def _evaluate_size_onchain(
    self,
    opportunity: Opportunity,
    trade_size_usd: float,
  ) -> tuple[TradeEvaluation | None, str | None, float | None]:
    if self._onchain is None:
      return None, "On-chain quoter not configured", None
    if not self._onchain.supports_opportunity(opportunity):
      buy = opportunity.buy_pair.dex_id
      sell = opportunity.sell_pair.dex_id
      return (
        None,
        f"No on-chain quoter for {buy}→{sell} on {opportunity.chain}",
        None,
      )

    result = await self._onchain.quote_round_trip(opportunity, trade_size_usd)
    if result is None:
      detail = self._onchain.last_error or "unknown"
      return None, f"On-chain QuoterV2 round-trip failed ({detail})", None

    # Broken / empty-pool quotes (wrong fee tier etc.) look like -9900bps.
    if result.sell_output_usd < trade_size_usd * 0.90:
      return (
        None,
        f"On-chain quote garbage (out ${result.sell_output_usd:.4f})",
        result.effective_spread_bps,
      )

    effective_bps = result.effective_spread_bps
    # Major WETH pools almost never have >50bps executable edge; treat as bad quote.
    if opportunity.pair_label.startswith("WETH/") and effective_bps > 50:
      return (
        None,
        f"On-chain edge implausible ({effective_bps:.0f}bps) — skipping",
        effective_bps,
      )
    if effective_bps < 0:
      return (
        None,
        f"On-chain round-trip negative ({effective_bps:.0f}bps)",
        effective_bps,
      )

    sell_output_usd = result.sell_output_usd
    # Prefer Quoter gas estimates when available.
    gas_units = result.buy_gas_estimate + result.sell_gas_estimate
    eth_usd = 1800.0
    if result.base_symbol == "WETH":
      eth_usd = max(opportunity.buy_price_usd, opportunity.sell_price_usd, 1.0)
    # Rough L2 gas: gas_units * gas_price; use configured chain gas as floor.
    gas_cost = max(
      self._cost_model.gas_cost_usd(opportunity.chain),
      (gas_units * 0.05e-9 * eth_usd) if gas_units > 0 else 0.0,
    )
    gross_pnl = sell_output_usd - trade_size_usd
    fees = self._cost_model.latency_cost_usd(trade_size_usd)
    net_pnl = gross_pnl - gas_cost - fees

    if net_pnl < self._config.min_net_profit_usd:
      return (
        None,
        f"Net ${net_pnl:.4f} below min ${self._config.min_net_profit_usd:.2f} "
        f"(onchain out ${sell_output_usd:.4f})",
        effective_bps,
      )

    return (
      TradeEvaluation(
        opportunity=opportunity,
        trade_size_usd=trade_size_usd,
        buy_input_usd=trade_size_usd,
        sell_output_usd=sell_output_usd,
        gross_pnl_usd=gross_pnl,
        fees_usd=fees,
        gas_cost_usd=gas_cost,
        latency_cost_usd=fees,
        mev_haircut_usd=0.0,
        net_pnl_usd=net_pnl,
        quote_source="onchain_quoter",
      ),
      None,
      effective_bps,
    )

  async def _evaluate_size_oneinch(
    self,
    opportunity: Opportunity,
    trade_size_usd: float,
  ) -> tuple[TradeEvaluation | None, str | None, float | None]:
    from src.quoting.oneinch_roundtrip import quote_spatial_round_trip

    result = await quote_spatial_round_trip(
      oneinch=self._oneinch,
      config=self._config,
      opportunity=opportunity,
      trade_size_usd=trade_size_usd,
    )
    if result is None:
      return None, "1inch round-trip quote failed", None

    sell_output_usd = result.sell_output_usd
    effective_bps = effective_spread_bps(sell_output_usd, trade_size_usd)
    if effective_bps < 0:
      return (
        None,
        f"1inch round-trip negative ({effective_bps:.0f}bps)",
        effective_bps,
      )

    gross_pnl, fees, gas_cost, mev_haircut, net_pnl = self._cost_model.compute_net_pnl(
      opportunity=opportunity,
      trade_size_usd=trade_size_usd,
      buy_input_usd=trade_size_usd,
      sell_output_usd=sell_output_usd,
      gross_spread_bps=opportunity.gross_spread_bps,
    )
    mev_haircut = 0.0
    net_pnl = gross_pnl - gas_cost - fees

    if net_pnl < self._config.min_net_profit_usd:
      return (
        None,
        f"Net ${net_pnl:.2f} below min ${self._config.min_net_profit_usd:.2f} "
        f"(1inch out ${sell_output_usd:.4f})",
        effective_bps,
      )

    return (
      TradeEvaluation(
        opportunity=opportunity,
        trade_size_usd=trade_size_usd,
        buy_input_usd=trade_size_usd,
        sell_output_usd=sell_output_usd,
        gross_pnl_usd=gross_pnl,
        fees_usd=fees,
        gas_cost_usd=gas_cost,
        latency_cost_usd=self._cost_model.latency_cost_usd(trade_size_usd),
        mev_haircut_usd=mev_haircut,
        net_pnl_usd=net_pnl,
        quote_source="1inch",
      ),
      None,
      effective_bps,
    )

  async def _evaluate_size_local(
    self,
    opportunity: Opportunity,
    trade_size_usd: float,
  ) -> tuple[TradeEvaluation | None, str | None, float | None]:
    if opportunity.strategy == "triangular":
      pool_quote = triangular_round_trip_usd(opportunity, trade_size_usd)
      if pool_quote is None:
        return None, "Pool quote failed", None
      drag = opportunity.buy_pair.fee_bps + opportunity.sell_pair.fee_bps + 5.0
      sell_output_usd = pool_quote.sell_output_usd
      quote_source = pool_quote.quote_source
    else:
      pool_quote = pool_round_trip_usd(opportunity, trade_size_usd)
      drag = fee_drag_bps(opportunity.buy_pair, opportunity.sell_pair)
      amm_output = pool_quote.sell_output_usd if pool_quote else 0.0
      spot_output = spot_estimated_sell_output(opportunity, trade_size_usd)
      # Simulate/paper: prefer conservative pool AMM (fees+impact baked in).
      # Spot-only can overstate edge when DexScreener mid prices disagree with depth.
      if self._config.execution_mode in {"simulate", "live"}:
        if pool_quote is None:
          return None, "Pool quote failed", None
        sell_output_usd = amm_output
        quote_source = "pool_direct"
      else:
        sell_output_usd = max(amm_output, spot_output)
        quote_source = "pool_amm+spot" if spot_output > amm_output else "pool_amm"
        if pool_quote is None and spot_output <= trade_size_usd:
          return None, "Pool quote failed", None

    effective_bps = effective_spread_bps(sell_output_usd, trade_size_usd)
    if effective_bps < 0:
      return (
        None,
        f"Spot {opportunity.gross_spread_bps:.0f}bps wiped by {drag:.0f}bps fees "
        f"→ {effective_bps:.0f}bps effective",
        effective_bps,
      )

    sell_output_usd = apply_spread_decay(
      sell_output_usd,
      trade_size_usd,
      self._config.spread_decay_bps,
    )
    if sell_output_usd <= 0:
      return None, "Spread decay eliminated output", effective_bps

    effective_bps = effective_spread_bps(sell_output_usd, trade_size_usd)
    gross_pnl, fees, gas_cost, mev_haircut, net_pnl = self._cost_model.compute_net_pnl(
      opportunity=opportunity,
      trade_size_usd=trade_size_usd,
      buy_input_usd=trade_size_usd,
      sell_output_usd=sell_output_usd,
      gross_spread_bps=opportunity.gross_spread_bps,
    )
    if self._config.execution_mode in {"simulate", "live"}:
      mev_haircut = 0.0
      net_pnl = gross_pnl - gas_cost - fees

    if net_pnl < self._config.min_net_profit_usd:
      return (
        None,
        f"Net ${net_pnl:.2f} below min ${self._config.min_net_profit_usd:.2f} "
        f"(gas ${gas_cost:.2f}, latency ${fees:.2f}, mev ${mev_haircut:.2f})",
        effective_bps,
      )

    return (
      TradeEvaluation(
        opportunity=opportunity,
        trade_size_usd=trade_size_usd,
        buy_input_usd=trade_size_usd,
        sell_output_usd=sell_output_usd,
        gross_pnl_usd=gross_pnl,
        fees_usd=fees,
        gas_cost_usd=gas_cost,
        latency_cost_usd=self._cost_model.latency_cost_usd(trade_size_usd),
        mev_haircut_usd=mev_haircut,
        net_pnl_usd=net_pnl,
        quote_source=quote_source,
      ),
      None,
      effective_bps,
    )

  def passes_mev_gate(self, gross_spread_bps: float, trade_size_usd: float) -> bool:
    # Educational noise only for local paper mode
    if self._config.execution_mode != "paper":
      return True
    return self._mev.passes_gate(gross_spread_bps, trade_size_usd)

  @staticmethod
  def mark_rejected(opportunity: Opportunity, reason: str) -> Opportunity:
    opportunity.status = OpportunityStatus.REJECTED
    opportunity.rejection_reason = reason
    return opportunity

  @staticmethod
  def mark_missed(opportunity: Opportunity) -> Opportunity:
    opportunity.status = OpportunityStatus.MISSED_MEV
    opportunity.rejection_reason = "MEV bot captured opportunity"
    return opportunity

  @staticmethod
  def mark_executed(opportunity: Opportunity) -> Opportunity:
    opportunity.status = OpportunityStatus.EXECUTED
    return opportunity
