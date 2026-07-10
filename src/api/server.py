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

    engine = ArbitrageEngine(config, env, ledger, on_event=print)
    engine_task = asyncio.create_task(engine.run_forever())
    app.state.engine = engine
    app.state.engine_task = engine_task

    print("\n=== DEX Arbitrage Paper Trader ===")
    print(f"Dashboard: http://localhost:8000")
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
      await ledger.close()

  app = FastAPI(title="DEX Arbitrage Paper Trader", lifespan=lifespan)
  app.include_router(create_router())

  @app.get("/")
  async def dashboard() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")

  if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

  return app


def main() -> None:
  uvicorn.run("src.api.server:create_app", factory=True, host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
  main()
