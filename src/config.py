from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
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
    min_liquidity_usd: float = 500_000.0
    min_volume_24h_usd: float = 50_000.0
    latency_penalty_bps: float = 5.0
    lp_fee_bps_per_leg: float = 5.0
    snapshot_every_n_trades: int = 5
    db_path: str = "data/cryptotrade.db"
    chains: dict[str, ChainConfig]
    pairs: list[PairConfig]


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    oneinch_api_key: str = ""
    dexscreener_api_key: str = ""


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
