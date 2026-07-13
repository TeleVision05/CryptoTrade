from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from src.api.routes import create_router
from src.config import EnvSettings, load_config, resolve_db_path
from src.engine import ArbitrageEngine
from src.ledger import Ledger
from src.sports import SportsArbScanner

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
WEB_DIR = ROOT_DIR / "web"


def create_app() -> FastAPI:
  config = load_config()

  @asynccontextmanager
  async def lifespan(app: FastAPI):
    env = EnvSettings()
    ledger = Ledger(resolve_db_path(config), config)
    await ledger.connect()
    app.state.ledger = ledger

    engine = await ArbitrageEngine.create(config, env, on_event=print)
    engine_task = asyncio.create_task(engine.run_forever())
    app.state.engine = engine
    app.state.engine_task = engine_task

    sports = None
    if getattr(config, "sports_arb_enabled", True):
      sports = SportsArbScanner(
        min_profit_pct=float(getattr(config, "sports_arb_min_profit_pct", 0.35)),
        stake_total=float(getattr(config, "sports_arb_stake_usd", 100.0)),
        scan_interval_sec=float(getattr(config, "sports_arb_scan_interval_sec", 45.0)),
        max_odds_age_sec=float(getattr(config, "sports_arb_max_odds_age_sec", 360.0)),
        max_profit_pct=float(getattr(config, "sports_arb_max_profit_pct", 5.0)),
        odds_api_interval_sec=float(
          getattr(config, "sports_arb_odds_api_interval_sec", 300.0)
        ),
        leagues=list(getattr(config, "sports_arb_leagues", [])),
        odds_api_key=env.odds_api_key,
      )
      sports.start_background()
      print(
        f"Sports arb ON | Action Network + ESPN auto"
        f"{' | Odds API manual-only' if env.odds_api_key else ''} | "
        f"min profit {config.sports_arb_min_profit_pct:.2f}%"
      )
    app.state.sports = sports

    mode = config.execution_mode.upper()
    port = EnvSettings().api_port
    print("\n=== DEX Arbitrage Trader ===")
    print(f"Mode:      {mode}")
    print(f"Dashboard: http://localhost:{port}")
    print(f"Capital:   ${config.starting_capital_usd:,.2f}")
    print(f"Chains:    {', '.join(config.chains)}")
    print("Press Ctrl+C to stop\n")

    try:
      yield
    finally:
      engine.stop()
      engine_task.cancel()
      try:
        await engine_task
      except asyncio.CancelledError:
        pass
      await engine.close()
      if sports is not None:
        await sports.close()

  app = FastAPI(title="DEX Arbitrage Trader", lifespan=lifespan)
  app.include_router(create_router())

  @app.get("/")
  async def dashboard() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")

  if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

  return app


def main() -> None:
  port = EnvSettings().api_port
  uvicorn.run(
    "src.api.server:create_app",
    factory=True,
    host="0.0.0.0",
    port=port,
    reload=False,
  )


if __name__ == "__main__":
  main()
