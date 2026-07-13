from __future__ import annotations

import random

from src.config import AppConfig


class MevModel:
  def __init__(self, config: AppConfig) -> None:
    self._config = config

  def capture_probability(self, gross_spread_bps: float, trade_size_usd: float) -> float:
    """
    Wider spreads and larger trades attract more searcher competition.
    Returns probability this bot captures the opportunity.
    """
    base = self._config.mev_capture_rate
    spread_excess = max(0.0, gross_spread_bps - self._config.min_gross_spread_bps)
    spread_penalty = spread_excess * self._config.mev_spread_sensitivity
    size_penalty = (trade_size_usd / max(self._config.starting_capital_usd, 1.0)) * 0.10
    return max(0.05, min(0.95, base - spread_penalty - size_penalty))

  def passes_gate(self, gross_spread_bps: float, trade_size_usd: float) -> bool:
    return random.random() < self.capture_probability(gross_spread_bps, trade_size_usd)

  def execution_haircut_usd(
    self,
    gross_spread_bps: float,
    trade_size_usd: float,
    gross_pnl_usd: float,
  ) -> float:
    """Partial P&L haircut on executed trades when competition is likely."""
    if gross_pnl_usd <= 0:
      return 0.0
    competition = 1.0 - self.capture_probability(gross_spread_bps, trade_size_usd)
    return gross_pnl_usd * competition * self._config.mev_execution_haircut_pct
