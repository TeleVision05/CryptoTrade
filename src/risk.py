from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from src.config import AppConfig


@dataclass
class RiskState:
  peak_balance_usd: float = 0.0
  daily_pnl_usd: float = 0.0
  daily_reset_date: date = field(default_factory=lambda: datetime.now(timezone.utc).date())
  consecutive_losses: int = 0


class RiskManager:
  def __init__(self, config: AppConfig) -> None:
    self._config = config
    self._state = RiskState()

  @property
  def state(self) -> RiskState:
    return self._state

  def sync_balance(self, balance_usd: float) -> None:
    self._maybe_reset_daily()
    if balance_usd > self._state.peak_balance_usd:
      self._state.peak_balance_usd = balance_usd
    if self._state.peak_balance_usd <= 0:
      self._state.peak_balance_usd = balance_usd

  def available_capital(self, chain: str, total_balance_usd: float) -> float:
    if not self._config.per_chain_capital:
      return total_balance_usd
    chain_count = max(len(self._config.chains), 1)
    return total_balance_usd / chain_count

  def check_can_trade(self, balance_usd: float, trade_size_usd: float) -> str | None:
    """Return rejection reason if trading should be blocked."""
    self.sync_balance(balance_usd)
    self._maybe_reset_daily()

    if trade_size_usd > balance_usd * self._config.max_position_pct + 0.01:
      return "Position exceeds max size"

    if self._state.daily_pnl_usd <= -self._config.max_daily_loss_usd:
      return f"Daily loss limit reached (${self._config.max_daily_loss_usd:.2f})"

    if self._state.peak_balance_usd > 0:
      drawdown = (self._state.peak_balance_usd - balance_usd) / self._state.peak_balance_usd
      if drawdown >= self._config.max_drawdown_pct:
        return f"Max drawdown reached ({self._config.max_drawdown_pct * 100:.0f}%)"

    if self._state.consecutive_losses >= self._config.max_consecutive_losses:
      return f"Cooldown after {self._config.max_consecutive_losses} consecutive losses"

    return None

  def record_trade_result(self, net_pnl_usd: float) -> None:
    self._maybe_reset_daily()
    self._state.daily_pnl_usd += net_pnl_usd
    if net_pnl_usd < 0:
      self._state.consecutive_losses += 1
    else:
      self._state.consecutive_losses = 0

  def _maybe_reset_daily(self) -> None:
    today = datetime.now(timezone.utc).date()
    if today != self._state.daily_reset_date:
      self._state.daily_pnl_usd = 0.0
      self._state.daily_reset_date = today
