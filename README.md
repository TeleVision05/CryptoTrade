# DEX Arbitrage Trader

Cross-DEX arbitrage bot for **Base** and **Arbitrum**. Scans live market data, detects price gaps between DEXes, and runs **online dry-run** (default), local paper, or live on-chain swaps.

**Default mode is `simulate`** — real QuoterV2 / Aerodrome / PancakeSwap quotes via RPC `eth_call`, no broadcast. Flip to `live` only when ready.

## Modes

| Mode | Behavior | Needs |
|------|----------|--------|
| `paper` | Local AMM math + SQLite (offline fallback) | nothing |
| `simulate` | **Default** — on-chain QuoterV2 round-trip (Uni/Aero/PCS); ledger updated; **never sends txs** | RPC URLs (`ONEINCH_API_KEY` optional) |
| `live` | Broadcast swaps via 1inch; hard size cap | + `WALLET_PRIVATE_KEY`, funded wallet, `live_enabled: true` |

## Strategies

### 1. Extreme funding harvest (high risk / reward)

When HL funding is abnormally rich/cheap, take the **receiving** side with a hard stop. Real rates; can get stopped out. Live = HL perps.

### 2. HL momentum (selective)

Directional trend + funding tilt on 15m candles. Higher ROC bar to avoid fee bleed.

### 3. Stablecoin lending (safety sleeve)

Aave / Fluid / Compound USDC via live DefiLlama APY.

## Reality check

- **Funding carry** is how retail-consistent PnL shows up in 2026 research (HL ↔ CEX / single-venue carry). Simulate uses live HL rates; live needs an HL wallet + hedge plan.
- **Spatial DEX arb** alone is not a consistent profit engine against MEV bots.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### Online dry-run (recommended)

1. Put RPC URLs in `.env` (publicnode works; dedicated Alchemy/Infura is more reliable):
   ```
   BASE_RPC_URL=https://base.publicnode.com
   ARBITRUM_RPC_URL=https://arbitrum.publicnode.com
   API_PORT=8005
   ```
2. Optional: `ONEINCH_API_KEY` for live path / aggregator mode
3. Confirm `execution_mode: simulate` and `simulate_quote_mode: onchain` in [`config/settings.yaml`](config/settings.yaml)
4. Run:
   ```bash
   python -m src
   ```
5. Dashboard: **http://localhost:8005** — badge should show **SIMULATE**

### Live checklist (tiny size)

1. Prove repeated **simulate** wins (see `live_require_simulate_wins`)
2. Dedicated wallet with limited funds (two non-atomic legs — inventory risk)
3. Fund with USDC (+ a little ETH for gas) on Base
4. Set `WALLET_PRIVATE_KEY`, `ONEINCH_API_KEY`, `execution_mode: live`, `live_enabled: true`, keep `live_max_trade_usd: 25`
5. Restart and watch the first trade carefully

## Configuration

Edit [`config/settings.yaml`](config/settings.yaml):

- `execution_mode`: `paper` | `simulate` | `live`
- `simulate_quote_mode`: `onchain` | `pool_direct` | `oneinch`
- `live_max_trade_usd`, `live_enabled`, spread/risk limits, DEXes

## Usage

```bash
# Engine + dashboard
python -m src

# CLI monitor loop
PYTHONUNBUFFERED=1 python -m src.monitor

# Portfolio / reset
python -m src.cli status
python -m src.cli reset
```

## API

| Endpoint | Description |
|----------|-------------|
| `GET /api/portfolio` | Balance, P&L, win rate |
| `GET /api/status` | Mode, 1inch/RPC readiness, risk |
| `GET /api/trades` | Trade history |
| `GET /api/equity` | Equity curve |
| `GET /api/opportunities` | Detected / rejected / executed |
| `GET /api/activity` | Live process feed |

## Disclaimer

Online dry-run is closer to reality than local AMM math, but still not a guarantee of live P&L. Real arb involves MEV, latency, failed txs, and stuck inventory between legs. Start tiny.
