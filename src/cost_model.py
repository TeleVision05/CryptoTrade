from __future__ import annotations

from src.config import AppConfig
from src.models import Opportunity


class CostModel:
  def __init__(self, config: AppConfig) -> None:
    self._config = config

  def gas_cost_usd(self, chain: str) -> float:
    return self._config.chains[chain].gas_cost_usd

  def latency_cost_usd(self, trade_size_usd: float) -> float:
    return trade_size_usd * (self._config.latency_penalty_bps / 10_000)

  def lp_fees_usd(self, trade_size_usd: float, legs: int = 2) -> float:
    fee_rate = (self._config.lp_fee_bps_per_leg * legs) / 10_000
    return trade_size_usd * fee_rate

  def mev_haircut_usd(self, opportunity: Opportunity, trade_size_usd: float) -> float:
    # MEV competition is modeled via the probabilistic capture gate in the evaluator.
    # Executed paper trades assume the modeled fill prices already reflect competition.
    return 0.0

  def compute_net_pnl(
    self,
    opportunity: Opportunity,
    trade_size_usd: float,
    buy_input_usd: float,
    sell_output_usd: float,
    extra_fees_usd: float = 0.0,
  ) -> tuple[float, float, float, float, float]:
    gross_pnl = sell_output_usd - buy_input_usd
    gas_cost = self.gas_cost_usd(opportunity.chain)
    latency_cost = self.latency_cost_usd(trade_size_usd)
    lp_fees = self.lp_fees_usd(trade_size_usd)
    mev_haircut = self.mev_haircut_usd(opportunity, trade_size_usd)
    fees = lp_fees + extra_fees_usd + latency_cost
    net_pnl = gross_pnl - gas_cost - fees - mev_haircut
    return gross_pnl, fees, gas_cost, mev_haircut, net_pnl
