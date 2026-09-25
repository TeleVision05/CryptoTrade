"""Safety rails: simulate is the default and live trading needs explicit opt-in.

These tests are fully offline. They never construct a real wallet, never open
an RPC connection, and fail loudly if a code path tries to quote or broadcast.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.config import AppConfig, EnvSettings, load_config
from src.data.oneinch import OneInchClient
from src.execution import LiveExecutor, create_executor
from src.execution.simulate import OnlineDryRunExecutor
from src.simulator import PaperExecutor
from tests.test_core import _test_config

# Placeholder string, deliberately NOT a valid key. LiveExecutor.ready only
# checks that a key is configured; every gate below fires before it is parsed.
FAKE_KEY = "placeholder-not-a-real-key"


class _FakeLedger:
  def __init__(self, wins: int) -> None:
    self._wins = wins

  async def count_winning_trades(self) -> int:
    return self._wins


def _env(**overrides) -> EnvSettings:
  values = dict(oneinch_api_key="test", wallet_private_key=FAKE_KEY)
  values.update(overrides)
  return EnvSettings(_env_file=None, **values)


@pytest.fixture(autouse=True)
def _forbid_network(monkeypatch):
  async def _boom(*_args, **_kwargs):
    raise AssertionError("live path reached a network quote")

  monkeypatch.setattr("src.quoting.oneinch_roundtrip.quote_spatial_round_trip", _boom)


def test_model_defaults_are_simulate_and_locked():
  fields = AppConfig.model_fields
  assert fields["execution_mode"].default == "simulate"
  assert fields["live_enabled"].default is False
  assert fields["live_max_trade_usd"].default <= 25.0
  assert fields["live_require_simulate_wins"].default >= 1


def test_shipped_settings_yaml_is_simulate_and_locked():
  config = load_config()
  assert config.execution_mode == "simulate"
  assert config.live_enabled is False


def test_env_defaults_have_no_wallet_key():
  env = EnvSettings(_env_file=None)
  assert env.wallet_private_key == ""


def test_create_executor_picks_non_broadcasting_executors():
  env = _env(wallet_private_key="")
  oneinch = OneInchClient("")
  paper = create_executor(_test_config(execution_mode="paper"), env, _FakeLedger(0), oneinch)
  sim = create_executor(_test_config(execution_mode="simulate"), env, _FakeLedger(0), oneinch)
  assert isinstance(paper, PaperExecutor)
  assert isinstance(sim, OnlineDryRunExecutor)


def test_create_executor_refuses_live_without_opt_in():
  config = _test_config(execution_mode="live", live_enabled=False)
  with pytest.raises(RuntimeError, match="live_enabled=false"):
    create_executor(config, _env(), _FakeLedger(99), OneInchClient("test"))


def test_live_executor_not_ready_without_key():
  config = _test_config(execution_mode="live", live_enabled=True)
  executor = LiveExecutor(config, _env(wallet_private_key=""), _FakeLedger(99), OneInchClient("test"))
  assert executor.ready is False


async def test_live_execute_refuses_when_locked():
  config = _test_config(execution_mode="live", live_enabled=False)
  executor = LiveExecutor(config, _env(), _FakeLedger(99), OneInchClient("test"))
  with pytest.raises(RuntimeError, match="locked"):
    await executor.execute(SimpleNamespace(trade_size_usd=10.0))


async def test_live_execute_enforces_size_cap():
  config = _test_config(execution_mode="live", live_enabled=True, live_max_trade_usd=25.0)
  executor = LiveExecutor(config, _env(), _FakeLedger(99), OneInchClient("test"))
  with pytest.raises(RuntimeError, match="exceeds live_max_trade_usd"):
    await executor.execute(SimpleNamespace(trade_size_usd=26.0))


async def test_live_execute_requires_simulate_wins():
  config = _test_config(execution_mode="live", live_enabled=True, live_require_simulate_wins=5)
  executor = LiveExecutor(config, _env(), _FakeLedger(4), OneInchClient("test"))
  with pytest.raises(RuntimeError, match="need 5 winning"):
    await executor.execute(SimpleNamespace(trade_size_usd=10.0))
