# DEX Arbitrage Paper Trader

Paper-trading simulator for **cross-DEX arbitrage** on Base and Arbitrum. Scans live market data, detects price gaps between decentralized exchanges, and simulates round-trip trades starting from **$1,000 USDC** — including fees, slippage, gas, and MEV haircut.

**Paper trading only.** No wallet, no on-chain transactions.

## Strategy

- **Cross-DEX (spatial) arbitrage** on the same L2 chain: buy WETH cheap on one DEX, sell high on another
- Pairs: WETH/USDC, WETH/USDT
- DEXes: Uniswap V3, Aerodrome (Base), SushiSwap (Arbitrum)

## Data Sources

| Tier | Source | Purpose |
|------|--------|---------|
| Discovery | [DexScreener API](https://docs.dexscreener.com/api/reference) | Screen pairs by price, liquidity, volume |
| Execution quotes | [1inch Swap API v6](https://business.1inch.com/) (optional) | Executable swap amounts |
| Fallback | Local Uniswap V2 AMM math | Quotes when 1inch key is absent |

## Simulation Assumptions

- Starting capital: **$1,000 USDC** (shared pool across chains)
- Max position: 40% of capital per trade
- Min gross spread: 20 bps (screening threshold)
- Min net profit: $0.35 per trade
- MEV capture rate: 30% (70% of opportunities missed by bots)
- Gas: ~$0.03 (Base), ~$0.05 (Arbitrum) per round-trip
- LP fees: 5 bps per leg + latency penalty

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Optional: add ONEINCH_API_KEY for higher-fidelity quotes
```

## Usage

### One command (engine + dashboard)

```bash
python -m src
```

Opens the dashboard at **http://localhost:8000** and runs the scanner in the background. Scan results also print in the terminal. Press `Ctrl+C` to stop.

### CLI only

```bash
# Terminal-only paper trading (no web UI)
python -m src.cli run

# Show portfolio status
python -m src.cli status

# Reset to $1,000
python -m src.cli reset
```

### Web dashboard only (alternative)

```bash
python -m src.api.server
```

## Configuration

Edit [`config/settings.yaml`](config/settings.yaml) to adjust:

- Scan interval, spread thresholds, MEV capture rate
- Chain gas costs and allowed DEXes
- Liquidity/volume filters

## Project Structure

```
CryptoTrade/
├── config/settings.yaml    # Simulation parameters
├── src/
│   ├── scanner.py          # DexScreener opportunity detection
│   ├── evaluator.py        # Trade sizing and net P&L
│   ├── simulator.py        # Paper trade execution
│   ├── engine.py           # Main async loop
│   ├── cli.py              # CLI commands
│   └── api/server.py       # FastAPI + dashboard
├── web/index.html          # Dashboard UI
└── data/cryptotrade.db     # SQLite ledger (created at runtime)
```

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/portfolio` | Balance, P&L, win rate |
| `GET /api/trades` | Trade history |
| `GET /api/equity` | Equity curve snapshots |
| `GET /api/opportunities` | Detected opportunities (executed, missed, rejected) |

## Disclaimer

This is a **simulation tool for educational purposes**. Real arbitrage involves MEV competition, latency, failed transactions, and smart contract risk. Simulated P&L will not match live trading results.
