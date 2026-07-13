from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT_DIR / "config" / "settings.yaml"


class ChainConfig(BaseModel):
    chain_id: int
    dexscreener_chain: str
    gas_cost_usd: float
    dexes: list[str]


class PairConfig(BaseModel):
    base: str
    quotes: list[str]


class AppConfig(BaseModel):
    starting_capital_usd: float = 1000.0
    scan_interval_sec: int = 10
    max_position_pct: float = 0.40
    min_gross_spread_bps: float = 30.0
    min_net_profit_usd: float = 0.50
    mev_capture_rate: float = 0.30
    mev_spread_sensitivity: float = 0.02
    mev_execution_haircut_pct: float = 0.15
    min_liquidity_usd: float = 500_000.0
    min_volume_24h_usd: float = 50_000.0
    latency_penalty_bps: float = 5.0
    lp_fee_bps_per_leg: float = 5.0
    spread_decay_bps: float = 2.0
    slippage_bps: float = 50.0
    snapshot_every_n_trades: int = 5
    execution_mode: Literal["paper", "simulate", "live"] = "simulate"
    # onchain = Uniswap QuoterV2 on exact pools (closest to live, no wallet needed).
    # pool_direct = local V2-style model (can overstate).
    # oneinch = aggregator round-trip (usually negative for spatial arb).
    simulate_quote_mode: Literal["onchain", "pool_direct", "oneinch"] = "onchain"
    live_enabled: bool = False
    live_max_trade_usd: float = 25.0
    live_require_simulate_wins: int = 5
    route_cooldown_sec: int = 60
    per_chain_capital: bool = True
    max_daily_loss_usd: float = 50.0
    max_drawdown_pct: float = 0.15
    max_consecutive_losses: int = 5
    db_path: str = "data/cryptotrade.db"
    chains: dict[str, ChainConfig]
    pairs: list[PairConfig]
    # --- Hyperliquid funding carry (optional; fee-gated; off by default) ---
    funding_enabled: bool = False
    funding_notional_usd: float = 250.0
    funding_max_capital_pct: float = 0.60
    funding_max_positions: int = 4
    funding_min_bps_hourly: float = 0.20
    funding_exit_bps_hourly: float = 0.08
    funding_min_oi_usd: float = 1_000_000.0
    funding_fee_bps_per_leg: float = 2.5
    funding_basis_haircut_bps: float = 0.5
    funding_mark_residual_pct: float = 0.15
    funding_max_hold_hours: float = 72.0
    funding_max_adverse_mark_pct: float = 4.0
    funding_time_scale: float = 1.0  # ignored — code forces 1.0
    funding_accrual_book_hours: float = 0.25
    funding_min_spread_bps_hourly: float = 0.45
    funding_max_breakeven_hours: float = 18.0
    basis_min_bps: float = 12.0
    basis_exit_bps: float = 2.0
    basis_take_profit_bps: float = 4.0
    basis_max_bps: float = 40.0
    basis_min_oi_usd: float = 5_000_000.0
    basis_max_positions: int = 0
    spatial_enabled: bool = False
    # --- Stablecoin lending yield (safety sleeve) ---
    yield_enabled: bool = True
    yield_capital_pct: float = 0.40
    yield_min_deposit_usd: float = 100.0
    yield_min_tvl_usd: float = 20_000_000.0
    yield_min_apy_pct: float = 1.5
    yield_migrate_min_apy_pct: float = 1.0
    yield_accrual_book_hours: float = 0.05
    yield_prefer_chains: list[str] = ["Base", "Arbitrum", "Ethereum"]
    yield_gas_base_usd: float = 0.05
    yield_gas_arbitrum_usd: float = 0.15
    yield_gas_ethereum_usd: float = 3.0
    # --- Extreme funding harvest (high risk / high reward) ---
    harvest_enabled: bool = True
    harvest_notional_usd: float = 250.0
    harvest_max_capital_pct: float = 0.30
    harvest_max_positions: int = 2
    harvest_min_bps_hourly: float = 0.50
    harvest_exit_bps_hourly: float = 0.25
    harvest_min_oi_usd: float = 3_000_000.0
    harvest_max_basis_bps: float = 80.0
    harvest_stop_pct: float = 0.03
    harvest_take_pct: float = 0.04
    harvest_max_hold_hours: float = 36.0
    harvest_fee_bps_per_leg: float = 2.5
    harvest_slippage_bps: float = 3.0
    harvest_accrual_book_hours: float = 0.05
    harvest_cooldown_sec: float = 1200.0
    # --- HL momentum (selective — higher ROC bar) ---
    momentum_enabled: bool = True
    momentum_notional_usd: float = 250.0
    momentum_max_capital_pct: float = 0.25
    momentum_max_positions: int = 1
    momentum_min_oi_usd: float = 15_000_000.0
    momentum_sma_bars: int = 20
    momentum_roc_bars: int = 4
    momentum_min_roc_bps: float = 35.0
    momentum_max_funding_pay_bps: float = 0.40
    momentum_short_funding_bps: float = 0.35
    momentum_stop_pct: float = 0.012
    momentum_take_pct: float = 0.028
    momentum_max_hold_hours: float = 18.0
    momentum_fee_bps_per_leg: float = 2.5
    momentum_slippage_bps: float = 2.0
    momentum_cooldown_sec: float = 600.0
    momentum_universe: list[str] = [
      "BTC", "ETH", "SOL", "ZRO", "WLD", "SUI", "LINK", "NEAR", "DOGE", "AVAX",
    ]
    # --- Sports betting arbitrage ---
    sports_arb_enabled: bool = True
    sports_arb_min_profit_pct: float = 0.35
    sports_arb_stake_usd: float = 100.0
    sports_arb_scan_interval_sec: float = 45.0
    sports_arb_odds_api_interval_sec: float = 300.0
    sports_arb_max_odds_age_sec: float = 360.0
    sports_arb_max_profit_pct: float = 5.0
    sports_arb_leagues: list[str] = [
      "nfl", "nba", "mlb", "nhl", "ncaaf", "ncaab", "wnba", "mls", "soccer", "ufc",
    ]


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    oneinch_api_key: str = ""
    dexscreener_api_key: str = ""
    wallet_private_key: str = ""
    simulate_from_address: str = ""
    api_port: int = 8001
    base_rpc_url: str = "https://base.publicnode.com"
    arbitrum_rpc_url: str = "https://arbitrum.publicnode.com"
    # Optional — The Odds API (https://the-odds-api.com) for 40+ extra books.
    odds_api_key: str = ""

    def rpc_url_for(self, chain: str) -> str:
        urls = {
            "base": self.base_rpc_url,
            "arbitrum": self.arbitrum_rpc_url,
        }
        return urls.get(chain, "")

    def resolve_simulate_from_address(self) -> str:
        if self.simulate_from_address:
            return self.simulate_from_address
        if self.wallet_private_key:
            from eth_account import Account

            return Account.from_key(self.wallet_private_key).address
        # Well-known Base USDC whale used only for eth_call / 1inch swap build (never broadcast).
        return "0x3304E22DDaa22bCdC5FCa2269b418046aE7b566A"


def load_config(path: Path | None = None) -> AppConfig:
    config_path = path or CONFIG_PATH
    with config_path.open() as f:
        raw: dict[str, Any] = yaml.safe_load(f)
    return AppConfig.model_validate(raw)


def resolve_db_path(config: AppConfig) -> Path:
    db_path = Path(config.db_path)
    if not db_path.is_absolute():
        db_path = ROOT_DIR / db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return db_path
