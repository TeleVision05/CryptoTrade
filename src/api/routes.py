from __future__ import annotations

from fastapi import APIRouter, Query, Request

from src.config import load_config
from src.ledger import Ledger


def get_ledger(request: Request) -> Ledger:
  return request.app.state.ledger


def create_router() -> APIRouter:
  router = APIRouter()
  config = load_config()

  @router.get("/api/portfolio")
  async def get_portfolio(request: Request) -> dict:
    ledger = get_ledger(request)
    status = await ledger.get_portfolio_status(last_n=10)
    return {
      "balance_usd": status.balance_usd,
      "starting_capital_usd": status.starting_capital_usd,
      "total_pnl_usd": status.total_pnl_usd,
      "total_trades": status.total_trades,
      "winning_trades": status.winning_trades,
      "win_rate": status.win_rate,
    }

  @router.get("/api/status")
  async def get_status(request: Request) -> dict:
    engine = getattr(request.app.state, "engine", None)
    risk = engine.risk_manager.state if engine is not None else None
    oneinch_ready = bool(getattr(engine, "oneinch_ready", False)) if engine else False
    rpc_ready = bool(getattr(engine, "rpc_ready", False)) if engine else False
    return {
      "execution_mode": config.execution_mode,
      "simulate_quote_mode": config.simulate_quote_mode,
      "oneinch_ready": oneinch_ready,
      "rpc_ready": rpc_ready,
      "live_enabled": config.live_enabled,
      "live_ready": bool(
        config.live_enabled and oneinch_ready and rpc_ready
      ),
      "live_max_trade_usd": config.live_max_trade_usd,
      "live_require_simulate_wins": config.live_require_simulate_wins,
      "min_gross_spread_bps": config.min_gross_spread_bps,
      "min_net_profit_usd": config.min_net_profit_usd,
      "chains": list(config.chains.keys()),
      "funding_enabled": config.funding_enabled,
      "funding_time_scale": config.funding_time_scale,
      "yield_enabled": getattr(config, "yield_enabled", False),
      "momentum_enabled": getattr(config, "momentum_enabled", False),
      "harvest_enabled": getattr(config, "harvest_enabled", False),
      "funding_positions": (
        (
          list(engine._yield.open_positions)
          + list(engine._harvest.open_positions)
          + list(engine._momentum.open_positions)
          + list(engine._funding.open_positions)
        )
        if engine
        else []
      ),
      "risk": {
        "peak_balance_usd": risk.peak_balance_usd if risk else None,
        "daily_pnl_usd": risk.daily_pnl_usd if risk else None,
        "consecutive_losses": risk.consecutive_losses if risk else None,
      },
    }

  @router.get("/api/activity")
  async def get_activity(
    request: Request,
    limit: int = Query(default=40, ge=1, le=100),
  ) -> dict:
    engine = getattr(request.app.state, "engine", None)
    if engine is None or not hasattr(engine, "activity"):
      return {
        "cycle": 0,
        "stage": "idle",
        "headline": "Engine not running",
        "events": [],
      }
    return engine.activity.snapshot(limit)

  @router.get("/api/trades")
  async def get_trades(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
  ) -> list[dict]:
    ledger = get_ledger(request)
    trades = await ledger.get_recent_trades(limit)
    return [trade.model_dump(mode="json") for trade in trades]

  @router.get("/api/equity")
  async def get_equity(request: Request) -> list[dict]:
    ledger = get_ledger(request)
    curve = await ledger.get_equity_curve()
    return [point.model_dump(mode="json") for point in curve]

  @router.get("/api/opportunities")
  async def get_opportunities(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
  ) -> list[dict]:
    ledger = get_ledger(request)
    return await ledger.get_recent_opportunities(limit)

  @router.get("/api/sports")
  async def get_sports(request: Request) -> dict:
    sports = getattr(request.app.state, "sports", None)
    if sports is None:
      return {
        "enabled": False,
        "message": "Sports arb disabled",
        "arbs": [],
        "near_misses": [],
        "events": 0,
        "books": 0,
        "sources": [],
      }
    snap = sports.latest
    return {
      "enabled": True,
      "updated_at": snap.updated_at,
      "scan_ms": snap.scan_ms,
      "events": snap.events,
      "books": snap.books,
      "sources": snap.sources,
      "message": snap.message,
      "odds_api_remaining": snap.odds_api_remaining,
      "odds_api_status": getattr(sports._odds_api, "last_status", None),
      "filtered_out": getattr(snap, "filtered_out", 0),
      "min_profit_pct": sports.min_profit_pct,
      "max_profit_pct": sports.max_profit_pct,
      "max_odds_age_sec": sports.max_odds_age_sec,
      "stake_total": sports.stake_total,
      "arbs": snap.arbs,
      "near_misses": snap.near_misses,
    }

  @router.post("/api/sports/scan")
  async def force_sports_scan(request: Request) -> dict:
    sports = getattr(request.app.state, "sports", None)
    if sports is None:
      return {"ok": False, "message": "Sports arb disabled"}
    snap = await sports.scan_once()
    return {
      "ok": True,
      "message": snap.message,
      "arbs": len(snap.arbs),
      "events": snap.events,
      "books": snap.books,
      "scan_ms": snap.scan_ms,
    }

  @router.post("/api/sports/odds-api")
  async def manual_odds_api_scan(request: Request) -> dict:
    """Spend Odds API credits once — never called by the background loop."""
    sports = getattr(request.app.state, "sports", None)
    if sports is None:
      return {"ok": False, "message": "Sports arb disabled"}
    snap = await sports.fetch_odds_api_manual()
    return {
      "ok": True,
      "message": snap.message,
      "arbs": len(snap.arbs),
      "events": snap.events,
      "books": snap.books,
      "scan_ms": snap.scan_ms,
      "odds_api_remaining": snap.odds_api_remaining,
      "odds_api_status": getattr(sports._odds_api, "last_status", None),
    }

  return router
