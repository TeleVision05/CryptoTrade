from __future__ import annotations

import random
from typing import Optional

from src.amm.math import spot_round_trip_usd
from src.config import AppConfig
from src.cost_model import CostModel
from src.data.oneinch import OneInchClient
from src.data.tokens import get_token
from src.models import Opportunity, OpportunityStatus, TradeEvaluation


class TradeEvaluator:
  def __init__(
    self,
    config: AppConfig,
    cost_model: CostModel,
    oneinch: OneInchClient,
  ) -> None:
    self._config = config
    self._cost_model = cost_model
    self._oneinch = oneinch

  def candidate_sizes(self, available_capital: float) -> list[float]:
    fractions = [0.10, 0.20, 0.30, self._config.max_position_pct]
    sizes = sorted({round(available_capital * frac, 2) for frac in fractions})
    return [size for size in sizes if size > 0]

  async def evaluate(
    self,
    opportunity: Opportunity,
    available_capital: float,
  ) -> Optional[TradeEvaluation]:
    best: Optional[TradeEvaluation] = None

    for trade_size in self.candidate_sizes(available_capital):
      evaluation = await self._evaluate_size(opportunity, trade_size)
      if evaluation is None:
        continue
      if best is None or evaluation.net_pnl_usd > best.net_pnl_usd:
        best = evaluation

    if best is None or best.net_pnl_usd < self._config.min_net_profit_usd:
      return None
    return best

  async def _evaluate_size(
    self,
    opportunity: Opportunity,
    trade_size_usd: float,
  ) -> Optional[TradeEvaluation]:
    quote_symbol = opportunity.pair_label.split("/")[1]
    chain = opportunity.chain
    chain_id = self._config.chains[chain].chain_id

    weth = get_token(chain, "WETH")
    quote_token = get_token(chain, quote_symbol)

    buy_input_usd = trade_size_usd
    weth_amount: float | None = None
    sell_output_usd: float | None = None

    if self._oneinch.enabled:
      buy_quote = await self._oneinch.quote_usd_to_token(
        chain_id=chain_id,
        usd_token=quote_token.address,
        usd_decimals=quote_token.decimals,
        dst_token=weth.address,
        dst_decimals=weth.decimals,
        usd_amount=trade_size_usd,
      )
      if buy_quote is not None:
        weth_amount = buy_quote.dst_amount
        sell_quote = await self._oneinch.quote_token_to_usd(
          chain_id=chain_id,
          src_token=weth.address,
          src_decimals=weth.decimals,
          usd_token=quote_token.address,
          usd_decimals=quote_token.decimals,
          token_amount=weth_amount,
        )
        if sell_quote is not None:
          sell_output_usd = sell_quote.dst_amount

    if weth_amount is None or sell_output_usd is None:
      sell_output_usd = spot_round_trip_usd(
        trade_size_usd=trade_size_usd,
        buy_price_usd=opportunity.buy_pair.price_usd,
        sell_price_usd=opportunity.sell_pair.price_usd,
        buy_liquidity_usd=opportunity.buy_pair.liquidity_usd,
        sell_liquidity_usd=opportunity.sell_pair.liquidity_usd,
      )
      if sell_output_usd <= 0:
        return None

    gross_pnl, fees, gas_cost, mev_haircut, net_pnl = self._cost_model.compute_net_pnl(
      opportunity=opportunity,
      trade_size_usd=trade_size_usd,
      buy_input_usd=buy_input_usd,
      sell_output_usd=sell_output_usd,
    )

    return TradeEvaluation(
      opportunity=opportunity,
      trade_size_usd=trade_size_usd,
      buy_input_usd=buy_input_usd,
      sell_output_usd=sell_output_usd,
      gross_pnl_usd=gross_pnl,
      fees_usd=fees,
      gas_cost_usd=gas_cost,
      latency_cost_usd=self._cost_model.latency_cost_usd(trade_size_usd),
      mev_haircut_usd=mev_haircut,
      net_pnl_usd=net_pnl,
    )

  def passes_mev_gate(self) -> bool:
    return random.random() < self._config.mev_capture_rate

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
