from __future__ import annotations

import uuid
from datetime import datetime, timezone

from src.ledger import Ledger
from src.models import PaperTrade, TradeEvaluation


class PaperExecutor:
  def __init__(self, ledger: Ledger) -> None:
    self._ledger = ledger

  async def execute(self, evaluation: TradeEvaluation) -> PaperTrade:
    current_balance = await self._ledger.get_balance()
    new_balance = current_balance + evaluation.net_pnl_usd
    await self._ledger.update_balance(new_balance)

    trade = PaperTrade(
      id=str(uuid.uuid4()),
      opportunity_id=evaluation.opportunity.id,
      timestamp=datetime.now(timezone.utc),
      chain=evaluation.opportunity.chain,
      pair_label=evaluation.opportunity.pair_label,
      buy_dex=evaluation.opportunity.buy_dex,
      sell_dex=evaluation.opportunity.sell_dex,
      trade_size_usd=evaluation.trade_size_usd,
      buy_price_usd=evaluation.opportunity.buy_price_usd,
      sell_price_usd=evaluation.opportunity.sell_price_usd,
      gross_pnl_usd=evaluation.gross_pnl_usd,
      fees_usd=evaluation.fees_usd,
      gas_cost_usd=evaluation.gas_cost_usd,
      mev_haircut_usd=evaluation.mev_haircut_usd,
      net_pnl_usd=evaluation.net_pnl_usd,
      balance_after_usd=new_balance,
    )
    await self._ledger.save_trade(trade)
    await self._ledger.maybe_record_snapshot()
    return trade
