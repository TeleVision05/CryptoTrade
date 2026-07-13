from __future__ import annotations

from src.config import AppConfig
from src.mev import MevModel
from src.models import Opportunity


class CostModel:
  def __init__(self, config: AppConfig, mev_model: MevModel | None = None) -> None:
    self._config = config
    self._mev = mev_model or MevModel(config)

  def gas_cost_usd(self, chain: str) -> float:
    return self._config.chains[chain].gas_cost_usd

  def latency_cost_usd(self, trade_size_usd: float) -> float:
    return trade_size_usd * (self._config.latency_penalty_bps / 10_000)

  def lp_fees_usd(self, trade_size_usd: float, legs: int = 2) -> float:
    """LP fees are modeled inside pool AMM quotes; keep at zero to avoid double-counting."""
    return 0.0

  def mev_haircut_usd(
    self,
    opportunity: Opportunity,
    trade_size_usd: float,
    gross_pnl_usd: float,
    gross_spread_bps: float,
  ) -> float:
    return self._mev.execution_haircut_usd(gross_spread_bps, trade_size_usd, gross_pnl_usd)

  def compute_net_pnl(
    self,
    opportunity: Opportunity,
    trade_size_usd: float,
    buy_input_usd: float,
    sell_output_usd: float,
    extra_fees_usd: float = 0.0,
    gross_spread_bps: float | None = None,
  ) -> tuple[float, float, float, float, float]:
    gross_pnl = sell_output_usd - buy_input_usd
    gas_cost = self.gas_cost_usd(opportunity.chain)
    latency_cost = self.latency_cost_usd(trade_size_usd)
    spread_bps = gross_spread_bps if gross_spread_bps is not None else opportunity.gross_spread_bps
    mev_haircut = self.mev_haircut_usd(opportunity, trade_size_usd, gross_pnl, spread_bps)
    fees = latency_cost + extra_fees_usd
    net_pnl = gross_pnl - gas_cost - fees - mev_haircut
    return gross_pnl, fees, gas_cost, mev_haircut, net_pnl
