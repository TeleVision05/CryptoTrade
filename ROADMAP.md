# CryptoTrade roadmap

## Current state (2026-09-25)

**Status: experimental. Every strategy is paper/simulate only; no real-money path is enabled.**

What runs today (`python -m src`, see `src/api/server.py`):

- A FastAPI app serving the dashboard (`web/index.html`) and `/api/*`, plus one
  background `ArbitrageEngine` loop (`src/engine.py`) and one background
  `SportsArbScanner` (`src/sports/scanner.py`).
- Per `config/settings.yaml`, the engine loop runs, in order, each `scan_interval_sec` (3s):

  | Strategy | Module | Enabled in `settings.yaml` | Books to ledger |
  |---|---|---|---|
  | Stablecoin lending yield (DefiLlama APY, 65% of capital) | `src/yield_carry.py` | yes | accrual only |
  | Extreme Hyperliquid funding harvest | `src/funding_harvest.py` | yes | paper positions |
  | HL <-> OKX/Bitget funding carry ("honest carry") | `src/funding/carry.py` | yes | paper positions |
  | HL momentum (15m trend + funding tilt) | `src/momentum.py` | **no** (`momentum_enabled: false`; `AppConfig` default is `True`) | paper positions |
  | Spatial DEX arb on Base/Arbitrum | `src/scanner.py`, `src/evaluator.py`, `src/execution/*` | **no** (`spatial_enabled: false`) | paper / simulate / live |
  | Sports betting arb (Action Network + ESPN auto, Odds API manual) | `src/sports/*` | yes | not booked; dashboard only |

- Execution mode is `simulate` (default in both `AppConfig` and the yaml). `live`
  is locked: `live_enabled: false`, and `LiveExecutor.execute` in
  `src/execution/__init__.py` also refuses above `live_max_trade_usd` (25),
  below `live_require_simulate_wins` (5), and when a fresh 1inch round-trip is
  under +10 bps.
- The only code that can move real funds is the 1inch spatial-DEX path in
  `LiveExecutor`. The Hyperliquid, OKX, Bitget and DefiLlama clients in
  `src/data/` are read-only (funding rates, candles, APYs); there is no order
  placement for perps, CEX or lending anywhere in the tree.
- Ledger is SQLite at `data/cryptotrade.db` (`src/ledger.py`: `portfolio_state`,
  `trades`, `opportunities`, `portfolio_snapshots`). `python -m src.cli reset` wipes it.

How that was verified:

- `.venv/bin/python -m pytest -q` on Python 3.14.5: **23 passed, 1 warning in 0.76s**
  (run 2026-09-25 09:52 PDT). Tests are offline and need no keys, RPC or wallet.
- Test coverage by module (from the imports in `tests/`):
  `src/amm/math`, `src/mev`, `src/risk`, `src/quoting/pool_quotes`, `src/config`,
  `src/execution` (factory + `LiveExecutor` gates), `src/sports/odds_math`,
  `src/sports/regions`, `src/sports/scanner`.
- The running app has **not** been exercised as part of this verification; the
  last commits touched docs and tests only.

## Known gaps and bugs

1. **Identity is stale.** `src/api/server.py` (`title="DEX Arbitrage Trader"`, startup
   banner) and `web/index.html` (`<title>`, `<h1>`) still call this a DEX arbitrage
   trader, but spatial DEX arb is disabled by default. `README.md` and
   `pyproject.toml` were corrected on 2026-09-25; the server and dashboard were not.
2. **Untested modules.** No tests for `src/engine.py` (`run_once`,
   `_process_opportunity`), `src/execution/simulate.py` (`OnlineDryRunExecutor`),
   `src/api/routes.py`, `src/ledger.py`, or any of the four strategy engines
   (`yield_carry`, `funding_harvest`, `momentum`, `funding/carry`). `src/evaluator.py`,
   `src/scanner.py`, `src/triangular.py`, `src/cost_model.py`,
   `src/quoting/onchain_quoter.py` and every `src/data/*` client are also untested.
3. **Config defaults diverge from the shipped yaml.** `AppConfig` in `src/config.py`
   and `config/settings.yaml` disagree on, for example, `momentum_enabled`
   (True vs false), `route_cooldown_sec` (60 vs 5), `min_gross_spread_bps` (30 vs 0),
   `min_liquidity_usd` (500k vs 80k), `harvest_notional_usd` (250 vs 150). Tests use
   `_test_config()` in `tests/test_core.py`, so the yaml values are only checked for
   `execution_mode` and `live_enabled`.
4. **`websockets.legacy` DeprecationWarning** on every test run. Source is
   `web3==7.16.0`'s persistent websocket provider importing `websockets.legacy`
   (`websockets==15.0.1`). This project only uses `Web3.HTTPProvider`
   (`src/execution/simulate.py`, `src/execution/__init__.py`), so it is harmless
   today but will become an ImportError when `websockets` drops `legacy`.
5. **Files over 500 lines:** `src/quoting/onchain_quoter.py` (760),
   `src/sports/scanner.py` (655), `src/scanner.py` (645), `src/funding/carry.py` (508).
6. **Private-attribute reach-through in the API.** `src/api/routes.py` `get_status`
   reads `engine._yield`, `engine._harvest`, `engine._momentum`, `engine._funding`,
   and `get_sports` reads `sports._odds_api`. Any rename breaks the dashboard silently.
7. **Duplicate `import time`** inside `ArbitrageEngine.run_once` in `src/engine.py`
   shadows the module-level import. Harmless, but it hides a refactor.
8. **`funding_time_scale`** is documented in `src/config.py` as "ignored — code
   forces 1.0" yet is still a config field and is reported by `GET /api/status`.
9. **No lint, formatter, pre-commit hook or CI.** The code uses 2-space indentation
   throughout, so adopting `ruff format`/`black` as-is would rewrite every file.
10. **Public repo with a local `.env`.** `.env` is git-ignored (verified with
    `git check-ignore`) and a history scan for key patterns and tracked `.env*`/`.pem`
    files found nothing on 2026-09-25. Keep it that way.

## Milestones

### M1. Tests for the paths that actually run

- [ ] `tests/test_ledger.py`: connect to a temp SQLite path, book a `PaperTrade`,
      assert `get_portfolio_status`, `count_winning_trades`, `reset`.
- [ ] `tests/test_strategies.py`: one `tick()` test per engine (`StableYieldEngine`,
      `FundingHarvestEngine`, `MomentumEngine`, `HonestCarryEngine`) using fake
      clients that return canned markets/APYs; assert open, accrue, stop, take-profit.
- [ ] `tests/test_engine.py`: `ArbitrageEngine.run_once` with the strategies stubbed,
      `spatial_enabled` both ways; assert the spatial branch is skipped when off.
- [ ] `tests/test_simulate_executor.py`: `OnlineDryRunExecutor` for `onchain`,
      `pool_direct`, `oneinch` with a fake quoter; assert it never constructs a wallet.
- [ ] `tests/test_api.py`: `fastapi.testclient.TestClient` against `create_router()`
      with stub `app.state`; cover every route in `src/api/routes.py` including the
      three `/api/sports*` endpoints.
- **Done when:** `.venv/bin/python -m pytest -q` passes with all five files present
  and the count is at least 45.

### M2. Lint, hooks, CI, and the deprecation warning

- [ ] Decide indentation (open question 5), then add `ruff` config to `pyproject.toml`.
- [ ] Add `.pre-commit-config.yaml` running `ruff check` and `ruff format --check`;
      document `pre-commit install` in the README.
- [ ] Add `.github/workflows/test.yml` running `pytest -q` on push and PR for the
      Python versions in `requires-python` (currently `>=3.11`; only 3.14.5 tested).
- [ ] Resolve gap 4: either add `filterwarnings = error::DeprecationWarning` plus an
      explicit `ignore` for `websockets.legacy` in `pytest.ini`, or pin
      `websockets<16` in `requirements.txt` with a comment naming web3 as the reason.
- **Done when:** a green GitHub Actions run URL on `main`, and
  `pre-commit run --all-files` exits 0 locally.

### M3. One source of truth for configuration

- [ ] Reconcile `AppConfig` defaults with `config/settings.yaml`, or strip the
      per-strategy defaults so the yaml is required.
- [ ] Add `tests/test_settings_yaml.py` loading the shipped yaml and asserting the
      enabled set is exactly `{yield, harvest, funding, sports_arb}` and that
      `momentum_enabled`, `spatial_enabled` and `live_enabled` are all `False`.
- [ ] Remove `funding_time_scale` from `AppConfig`, the yaml and `GET /api/status`.
- [ ] Fix the stale title in `src/api/server.py` and `web/index.html` (gap 1).
- **Done when:** the new test passes and `grep -rn "DEX Arbitrage Trader" src web`
  returns nothing.

### M4. Evidence from a real simulate run

- [ ] Run `python -m src` in `simulate` for at least 24 hours against public RPCs.
- [ ] Capture `GET /api/portfolio`, `GET /api/equity` and `GET /api/status` at the end,
      plus a dashboard screenshot, into `docs/simulate-runs/<date>.md`.
- [ ] Break P&L down per strategy (needs a `strategy` column on `trades` if the
      ledger does not already carry it; check `src/ledger.py` first).
- [ ] Decide per strategy: keep, tune, or disable.
- **Done when:** `docs/simulate-runs/<date>.md` exists with the JSON and the
  screenshot path, and `settings.yaml` reflects the keep/disable decisions.

### M5. Bring oversized modules under 500 lines

- [ ] Split `src/quoting/onchain_quoter.py` (760) into ABI/encoding, pool discovery,
      and round-trip quoting.
- [ ] Split `src/sports/scanner.py` (655) into snapshot/state, matching, and the
      scan loop.
- [ ] Split `src/scanner.py` (645) into DexScreener ingestion and hot-route re-quoting.
- [ ] Trim `src/funding/carry.py` (508) by moving position math to its own module.
- [ ] Remove the duplicate `import time` in `src/engine.py` (gap 7).
- **Done when:** `wc -l src/*.py src/*/*.py` shows no file above 500 lines and the
  full test suite still passes.

### M6. (Gated) First real-money path

Only starts after M4 shows a strategy with positive simulate P&L and after the
owner answers open question 3.

- [ ] If the chosen strategy is spatial DEX arb: flip `spatial_enabled`, run
      simulate until `live_require_simulate_wins` is met, then follow the live
      checklist in the README with `live_max_trade_usd: 25`.
- [ ] If the chosen strategy is a Hyperliquid perp strategy: add a signed
      `/exchange` client (none exists), a paper-vs-live executor split like
      `src/execution/`, and the same lock/size-cap/wins gates with tests before
      any key is configured.
- **Done when:** exactly one live trade at or below $25 is booked with a real tx hash
  or HL order id in the ledger, and the safety tests in `tests/test_live_safety.py`
  still pass unchanged.

## Won't do / out of scope

- Weakening or auto-unlocking any live gate (`live_enabled`, `live_max_trade_usd`,
  `live_require_simulate_wins`, the +10 bps fresh-quote check).
- Auto-polling The Odds API. It is manual-only by design (`settings.yaml` comment,
  `POST /api/sports/odds-api`) because the free tier is ~500 requests/month.
- An MEV-competitive spatial DEX arb engine (private mempool, bundles, custom
  contracts). The README's own reality check says spatial arb alone is not a
  consistent retail profit engine.
- Hosted or multi-user deployment. The dashboard binds `0.0.0.0` with no auth and
  is meant for a local machine.
- Wholesale reformatting to 4-space PEP 8 before the owner answers open question 5.

## Open questions for the owner

1. Should the project keep the name CryptoTrade, and what should the dashboard
   and server title say now that the default strategies are yield, funding
   harvest, funding carry and sports arb?
2. `momentum_enabled` is `false` in the yaml but `True` in `AppConfig`. Which is
   intended?
3. Which strategy, if any, is the candidate to go live first? Hyperliquid perps
   would need a signed exchange client that does not exist yet; the only wired
   live path is 1inch spatial DEX swaps, and that strategy is off.
4. Sports arb is unrelated to crypto, is the second-largest module, and has its
   own four data clients. Keep it in this process, or split it into its own repo?
5. Indentation: keep 2-space (and configure ruff accordingly) or reformat to
   4-space once and adopt `ruff format` defaults?
6. `requires-python = ">=3.11"` but only 3.14.5 has been exercised. Pin, or add
   3.11/3.12/3.13 to CI in M2?
7. The repo is public and `.env` holds `ONEINCH_API_KEY` and `ODDS_API_KEY`
   locally. Are both keys free-tier and fine to keep as-is, or should they be
   rotated now that the repo is being actively pushed?
