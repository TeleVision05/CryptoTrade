from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class OpportunityStatus(str, Enum):
    DETECTED = "detected"
    REJECTED = "rejected"
    MISSED_MEV = "missed_mev"
    EXECUTED = "executed"


class DexPair(BaseModel):
    chain: str
    dex_id: str
    pair_address: str
    base_symbol: str
    quote_symbol: str
    price_usd: float
    liquidity_usd: float
    volume_24h_usd: float
    base_token_address: str
    quote_token_address: str
    labels: list[str] = Field(default_factory=list)
    fee_bps: float = 30.0


class Opportunity(BaseModel):
    id: str
    timestamp: datetime
    chain: str
    pair_label: str
    buy_dex: str
    sell_dex: str
    buy_price_usd: float
    sell_price_usd: float
    gross_spread_bps: float
    buy_pair: DexPair
    sell_pair: DexPair
    strategy: str = "spatial"
    status: OpportunityStatus = OpportunityStatus.DETECTED
    rejection_reason: Optional[str] = None


class TradeEvaluation(BaseModel):
    opportunity: Opportunity
    trade_size_usd: float
    buy_input_usd: float
    sell_output_usd: float
    gross_pnl_usd: float
    fees_usd: float
    gas_cost_usd: float
    latency_cost_usd: float
    mev_haircut_usd: float
    net_pnl_usd: float
    quote_source: str = "pool_amm"
    mev_captured: bool = True


class PaperTrade(BaseModel):
    id: str
    opportunity_id: str
    timestamp: datetime
    chain: str
    pair_label: str
    buy_dex: str
    sell_dex: str
    trade_size_usd: float
    buy_price_usd: float
    sell_price_usd: float
    gross_pnl_usd: float
    fees_usd: float
    gas_cost_usd: float
    mev_haircut_usd: float
    net_pnl_usd: float
    balance_after_usd: float


class PortfolioSnapshot(BaseModel):
    timestamp: datetime
    balance_usd: float
    total_trades: int
    win_rate: float
    total_pnl_usd: float


class PortfolioStatus(BaseModel):
    balance_usd: float
    starting_capital_usd: float
    total_pnl_usd: float
    total_trades: int
    winning_trades: int
    win_rate: float
    last_trades: list[PaperTrade] = Field(default_factory=list)
