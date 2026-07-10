from __future__ import annotations

from fastapi import APIRouter, Query, Request

from src.ledger import Ledger


def get_ledger(request: Request) -> Ledger:
  return request.app.state.ledger


def create_router() -> APIRouter:
  router = APIRouter()

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

  return router
