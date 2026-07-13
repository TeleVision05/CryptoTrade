from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from src.amm.math import get_amount_out, spot_round_trip_usd
from src.config import AppConfig, ChainConfig, PairConfig
from src.mev import MevModel
from src.models import DexPair, Opportunity
from src.quoting.pool_quotes import apply_spread_decay, pool_round_trip_usd
from src.risk import RiskManager


def _test_config(**overrides) -> AppConfig:
  defaults = dict(
    starting_capital_usd=1000.0,
    scan_interval_sec=10,
    max_position_pct=0.40,
    min_gross_spread_bps=10.0,
    min_net_profit_usd=0.10,
    mev_capture_rate=0.30,
    mev_spread_sensitivity=0.02,
    mev_execution_haircut_pct=0.15,
    min_liquidity_usd=400_000,
    min_volume_24h_usd=20_000,
    latency_penalty_bps=5.0,
    lp_fee_bps_per_leg=5.0,
    spread_decay_bps=2.0,
    slippage_bps=50.0,
    snapshot_every_n_trades=5,
    execution_mode="paper",
    live_max_trade_usd=25.0,
    per_chain_capital=True,
    max_daily_loss_usd=50.0,
    max_drawdown_pct=0.15,
    max_consecutive_losses=5,
    db_path="data/test.db",
    chains={
      "base": ChainConfig(
        chain_id=8453,
        dexscreener_chain="base",
        gas_cost_usd=0.03,
        dexes=["uniswap", "aerodrome"],
      ),
    },
    pairs=[PairConfig(base="WETH", quotes=["USDC"])],
  )
  defaults.update(overrides)
  return AppConfig(**defaults)


def _sample_opportunity(
  buy_price: float = 3000.0,
  sell_price: float = 3015.0,
  liquidity: float = 1_000_000.0,
) -> Opportunity:
  buy_pair = DexPair(
    chain="base",
    dex_id="aerodrome",
    pair_address="0xbuy",
    base_symbol="WETH",
    quote_symbol="USDC",
    price_usd=buy_price,
    liquidity_usd=liquidity,
    volume_24h_usd=100_000.0,
    base_token_address="0xweth",
    quote_token_address="0xusdc",
  )
  sell_pair = buy_pair.model_copy(
    update={
      "dex_id": "uniswap",
      "pair_address": "0xsell",
      "price_usd": sell_price,
    }
  )
  spread_bps = ((sell_price - buy_price) / buy_price) * 10_000
  return Opportunity(
    id=str(uuid.uuid4()),
    timestamp=datetime.now(timezone.utc),
    chain="base",
    pair_label="WETH/USDC",
    buy_dex="aerodrome",
    sell_dex="uniswap",
    buy_price_usd=buy_price,
    sell_price_usd=sell_price,
    gross_spread_bps=spread_bps,
    buy_pair=buy_pair,
    sell_pair=sell_pair,
  )


def test_get_amount_out_positive():
  out = get_amount_out(100.0, 10_000.0, 10_000.0, fee_bps=30.0)
  assert 0 < out < 100.0


def test_pool_round_trip_profitable_on_wide_spread():
  opp = _sample_opportunity(buy_price=3000.0, sell_price=3030.0)
  quote = pool_round_trip_usd(opp, trade_size_usd=100.0)
  assert quote is not None
  assert quote.sell_output_usd > quote.trade_size_usd
  assert quote.effective_spread_bps > 0


def test_pool_round_trip_rejects_when_impact_exceeds_spread():
  opp = _sample_opportunity(buy_price=3000.0, sell_price=3003.0, liquidity=50_000.0)
  quote = pool_round_trip_usd(opp, trade_size_usd=500.0)
  if quote is not None:
    assert quote.buy_impact_bps + quote.sell_impact_bps >= opp.gross_spread_bps


def test_apply_spread_decay():
  assert apply_spread_decay(100.0, 1000.0, 0.0) == 100.0
  assert apply_spread_decay(100.0, 1000.0, 10.0) == 99.0


def test_mev_capture_probability_decreases_with_spread():
  config = _test_config()
  mev = MevModel(config)
  low = mev.capture_probability(15.0, 100.0)
  high = mev.capture_probability(40.0, 100.0)
  assert high < low


def test_risk_blocks_daily_loss():
  config = _test_config(max_daily_loss_usd=10.0)
  risk = RiskManager(config)
  risk.sync_balance(1000.0)
  risk.record_trade_result(-12.0)
  reason = risk.check_can_trade(988.0, 50.0)
  assert reason is not None
  assert "Daily loss" in reason


def test_risk_per_chain_capital():
  config = _test_config(per_chain_capital=True)
  risk = RiskManager(config)
  available = risk.available_capital("base", 1000.0)
  assert available == 1000.0  # one chain in test config


def test_net_pnl_never_exceeds_gross():
  opp = _sample_opportunity()
  quote = pool_round_trip_usd(opp, 100.0)
  assert quote is not None
  gross = quote.sell_output_usd - quote.trade_size_usd
  decayed = apply_spread_decay(quote.sell_output_usd, quote.trade_size_usd, 2.0)
  net_before_costs = decayed - quote.trade_size_usd
  assert net_before_costs <= gross


def test_best_pool_pair_orients_regardless_of_list_order():
  """Cheaper pool listed after dearer must still be selected as buy."""
  from src.scanner import OpportunityScanner

  config = _test_config(min_liquidity_usd=1_000, min_volume_24h_usd=1_000)
  dear = DexPair(
    chain="base",
    dex_id="uniswap",
    pair_address="0xdear",
    base_symbol="VIRTUAL",
    quote_symbol="WETH",
    price_usd=0.63,
    liquidity_usd=400_000.0,
    volume_24h_usd=100_000.0,
    base_token_address="0xbase",
    quote_token_address="0xquote",
    fee_bps=20.0,
  )
  cheap = DexPair(
    chain="base",
    dex_id="aerodrome",
    pair_address="0xcheap",
    base_symbol="VIRTUAL",
    quote_symbol="WETH",
    price_usd=0.625,
    liquidity_usd=4_000_000.0,
    volume_24h_usd=100_000.0,
    base_token_address="0xbase",
    quote_token_address="0xquote",
    fee_bps=15.0,
  )
  # Dear listed first — old bug skipped this combo.
  scanner = OpportunityScanner(config, dexscreener=None)  # type: ignore[arg-type]
  buy, sell, spread = scanner._best_pool_pair([dear, cheap], reference_size_usd=100.0)
  assert buy is not None and sell is not None
  assert buy.pair_address == "0xcheap"
  assert sell.pair_address == "0xdear"
  assert spread > 50.0
