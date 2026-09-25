# CryptoTrade

Multi-strategy **paper-trading** engine with a local dashboard. Scans live market
data and books simulated positions to a SQLite ledger; nothing moves real funds
unless you explicitly unlock the (disabled) live DEX path.

**Status: experimental.** Runs in `simulate` mode; all strategies are paper-only.
See [ROADMAP.md](ROADMAP.md) for what works, what is untested, and what is next.

## Strategies

Enabled/disabled per [`config/settings.yaml`](config/settings.yaml):

| Strategy | Data source | Default |
|---|---|---|
| Stablecoin lending yield (Aave / Fluid / Compound USDC) | DefiLlama live APY | on (65% of capital) |
| Extreme Hyperliquid funding harvest (take the receiving side, hard stop) | Hyperliquid | on |
| Hyperliquid <-> OKX/Bitget funding carry (fee-gated) | Hyperliquid, OKX, Bitget | on |
| Hyperliquid momentum (15m trend + funding tilt) | Hyperliquid | off |
| Spatial DEX arbitrage on Base / Arbitrum (Uniswap, Aerodrome, PancakeSwap) | DexScreener, QuoterV2 via RPC, 1inch | off |
| Sports betting arbitrage (multi-book, same-region, 3-way soccer) | Action Network, ESPN, The Odds API (manual) | on |

The Hyperliquid, CEX and lending clients are read-only. The only code that can
broadcast a transaction is the 1inch spatial-DEX `live` executor, and it is locked
(see Modes).

## Modes

| Mode | Behavior | Needs |
|------|----------|--------|
| `paper` | Local AMM math + SQLite | nothing |
| `simulate` | **Default.** On-chain QuoterV2 round-trip via `eth_call`; ledger updated; never sends txs | RPC URLs |
| `live` | Broadcast DEX swaps via 1inch, hard size cap | `WALLET_PRIVATE_KEY`, `ONEINCH_API_KEY`, funded wallet, `live_enabled: true`, and `live_require_simulate_wins` simulate wins |

`live_enabled` is `false` in code and in the yaml. The live executor also refuses
trades above `live_max_trade_usd` (25) and any route whose fresh 1inch round-trip
is under +10 bps. `tests/test_live_safety.py` locks these rails in.

## Install

Requires Python 3.11+ (developed on 3.14).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## Configure

Environment variables go in `.env` (git-ignored); every key is documented in
[`.env.example`](.env.example):

| Variable | Needed for |
|---|---|
| `BASE_RPC_URL`, `ARBITRUM_RPC_URL` | simulate / live DEX quotes (public defaults work; dedicated endpoints 429 less) |
| `API_PORT` | dashboard port (default 8001) |
| `ONEINCH_API_KEY` | `simulate_quote_mode: oneinch` and live |
| `DEXSCREENER_API_KEY` | optional |
| `SIMULATE_FROM_ADDRESS` | optional `from` for `eth_call`; never broadcasts |
| `WALLET_PRIVATE_KEY` | live only; dedicated wallet, small funds |
| `ODDS_API_KEY` | optional; sports arb "Fetch Odds API" button only, never auto-polled |

Strategy thresholds, capital splits, risk limits, chains and pairs are in
[`config/settings.yaml`](config/settings.yaml). Key switches: `execution_mode`,
`simulate_quote_mode`, `*_enabled`, `live_enabled`, `live_max_trade_usd`.

## Run

```bash
# Engine + dashboard at http://localhost:$API_PORT (badge should read SIMULATE)
python -m src

# CLI-only engine loop with periodic P&L lines (optional cycle count)
PYTHONUNBUFFERED=1 python -m src.monitor

# Portfolio / reset ledger
python -m src.cli status
python -m src.cli reset
```

### Going live (DEX path only, tiny size)

1. Enable `spatial_enabled` and prove repeated `simulate` wins (`live_require_simulate_wins`).
2. Dedicated wallet with limited USDC plus a little ETH for gas on Base.
3. Set `WALLET_PRIVATE_KEY`, `ONEINCH_API_KEY`, `execution_mode: live`,
   `live_enabled: true`; keep `live_max_trade_usd: 25`.
4. Restart and watch the first trade. The two legs are not atomic; inventory can get stuck.

## Test

Offline; no RPC, keys or wallet needed.

```bash
.venv/bin/python -m pytest -q
```

Last run 2026-09-25: `23 passed, 1 warning` (the warning is `websockets.legacy`
from web3, tracked in the roadmap).

## API

| Endpoint | Description |
|----------|-------------|
| `GET /api/portfolio` | Balance, P&L, win rate |
| `GET /api/status` | Mode, 1inch/RPC readiness, enabled strategies, open positions, risk |
| `GET /api/trades` | Trade history |
| `GET /api/equity` | Equity curve |
| `GET /api/opportunities` | Detected / rejected / executed DEX routes |
| `GET /api/activity` | Live process feed |
| `GET /api/sports` | Latest sports-arb snapshot (arbs, near misses, books, sources) |
| `POST /api/sports/scan` | Force a sports scan (Action Network + ESPN) |
| `POST /api/sports/odds-api` | One manual Odds API fetch; spends credits |

## Disclaimer

Simulated P&L is not live P&L. Real execution involves MEV, latency, failed
transactions, stuck inventory between legs, and exchange risk. Start tiny.
