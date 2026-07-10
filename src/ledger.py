from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from src.config import AppConfig
from src.models import Opportunity, OpportunityStatus, PaperTrade, PortfolioSnapshot, PortfolioStatus


class Ledger:
  def __init__(self, db_path: Path, config: AppConfig) -> None:
    self._db_path = db_path
    self._config = config
    self._conn: aiosqlite.Connection | None = None

  async def connect(self) -> None:
    self._db_path.parent.mkdir(parents=True, exist_ok=True)
    self._conn = await aiosqlite.connect(self._db_path)
    self._conn.row_factory = aiosqlite.Row
    await self._create_tables()
    await self._ensure_state()

  async def close(self) -> None:
    if self._conn is not None:
      await self._conn.close()
      self._conn = None

  async def _create_tables(self) -> None:
    assert self._conn is not None
    await self._conn.executescript(
      """
      CREATE TABLE IF NOT EXISTS portfolio_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        balance_usd REAL NOT NULL,
        starting_capital_usd REAL NOT NULL,
        updated_at TEXT NOT NULL
      );

      CREATE TABLE IF NOT EXISTS trades (
        id TEXT PRIMARY KEY,
        opportunity_id TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        chain TEXT NOT NULL,
        pair_label TEXT NOT NULL,
        buy_dex TEXT NOT NULL,
        sell_dex TEXT NOT NULL,
        trade_size_usd REAL NOT NULL,
        buy_price_usd REAL NOT NULL,
        sell_price_usd REAL NOT NULL,
        gross_pnl_usd REAL NOT NULL,
        fees_usd REAL NOT NULL,
        gas_cost_usd REAL NOT NULL,
        mev_haircut_usd REAL NOT NULL,
        net_pnl_usd REAL NOT NULL,
        balance_after_usd REAL NOT NULL
      );

      CREATE TABLE IF NOT EXISTS opportunities (
        id TEXT PRIMARY KEY,
        timestamp TEXT NOT NULL,
        chain TEXT NOT NULL,
        pair_label TEXT NOT NULL,
        buy_dex TEXT NOT NULL,
        sell_dex TEXT NOT NULL,
        buy_price_usd REAL NOT NULL,
        sell_price_usd REAL NOT NULL,
        gross_spread_bps REAL NOT NULL,
        status TEXT NOT NULL,
        rejection_reason TEXT,
        payload_json TEXT NOT NULL
      );

      CREATE TABLE IF NOT EXISTS portfolio_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        balance_usd REAL NOT NULL,
        total_trades INTEGER NOT NULL,
        win_rate REAL NOT NULL,
        total_pnl_usd REAL NOT NULL
      );
      """
    )
    await self._conn.commit()

  async def _ensure_state(self) -> None:
    assert self._conn is not None
    cursor = await self._conn.execute("SELECT balance_usd FROM portfolio_state WHERE id = 1")
    row = await cursor.fetchone()
    if row is None:
      now = datetime.utcnow().isoformat()
      await self._conn.execute(
        """
        INSERT INTO portfolio_state (id, balance_usd, starting_capital_usd, updated_at)
        VALUES (1, ?, ?, ?)
        """,
        (self._config.starting_capital_usd, self._config.starting_capital_usd, now),
      )
      await self._conn.commit()
      await self.record_snapshot()

  async def reset(self) -> None:
    assert self._conn is not None
    await self._conn.executescript(
      """
      DELETE FROM trades;
      DELETE FROM opportunities;
      DELETE FROM portfolio_snapshots;
      DELETE FROM portfolio_state;
      """
    )
    await self._conn.commit()
    await self._ensure_state()

  async def get_balance(self) -> float:
    assert self._conn is not None
    cursor = await self._conn.execute("SELECT balance_usd FROM portfolio_state WHERE id = 1")
    row = await cursor.fetchone()
    return float(row["balance_usd"])

  async def get_starting_capital(self) -> float:
    assert self._conn is not None
    cursor = await self._conn.execute(
      "SELECT starting_capital_usd FROM portfolio_state WHERE id = 1"
    )
    row = await cursor.fetchone()
    return float(row["starting_capital_usd"])

  async def update_balance(self, balance_usd: float) -> None:
    assert self._conn is not None
    now = datetime.utcnow().isoformat()
    await self._conn.execute(
      "UPDATE portfolio_state SET balance_usd = ?, updated_at = ? WHERE id = 1",
      (balance_usd, now),
    )
    await self._conn.commit()

  async def save_opportunity(self, opportunity: Opportunity) -> None:
    assert self._conn is not None
    payload = opportunity.model_dump(mode="json")
    await self._conn.execute(
      """
      INSERT OR REPLACE INTO opportunities (
        id, timestamp, chain, pair_label, buy_dex, sell_dex,
        buy_price_usd, sell_price_usd, gross_spread_bps, status,
        rejection_reason, payload_json
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      """,
      (
        opportunity.id,
        opportunity.timestamp.isoformat(),
        opportunity.chain,
        opportunity.pair_label,
        opportunity.buy_dex,
        opportunity.sell_dex,
        opportunity.buy_price_usd,
        opportunity.sell_price_usd,
        opportunity.gross_spread_bps,
        opportunity.status.value,
        opportunity.rejection_reason,
        json.dumps(payload),
      ),
    )
    await self._conn.commit()

  async def save_trade(self, trade: PaperTrade) -> None:
    assert self._conn is not None
    await self._conn.execute(
      """
      INSERT INTO trades (
        id, opportunity_id, timestamp, chain, pair_label, buy_dex, sell_dex,
        trade_size_usd, buy_price_usd, sell_price_usd, gross_pnl_usd,
        fees_usd, gas_cost_usd, mev_haircut_usd, net_pnl_usd, balance_after_usd
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      """,
      (
        trade.id,
        trade.opportunity_id,
        trade.timestamp.isoformat(),
        trade.chain,
        trade.pair_label,
        trade.buy_dex,
        trade.sell_dex,
        trade.trade_size_usd,
        trade.buy_price_usd,
        trade.sell_price_usd,
        trade.gross_pnl_usd,
        trade.fees_usd,
        trade.gas_cost_usd,
        trade.mev_haircut_usd,
        trade.net_pnl_usd,
        trade.balance_after_usd,
      ),
    )
    await self._conn.commit()

  async def count_trades(self) -> int:
    assert self._conn is not None
    cursor = await self._conn.execute("SELECT COUNT(*) AS count FROM trades")
    row = await cursor.fetchone()
    return int(row["count"])

  async def count_winning_trades(self) -> int:
    assert self._conn is not None
    cursor = await self._conn.execute(
      "SELECT COUNT(*) AS count FROM trades WHERE net_pnl_usd > 0"
    )
    row = await cursor.fetchone()
    return int(row["count"])

  async def get_recent_trades(self, limit: int = 20) -> list[PaperTrade]:
    assert self._conn is not None
    cursor = await self._conn.execute(
      """
      SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?
      """,
      (limit,),
    )
    rows = await cursor.fetchall()
    return [self._row_to_trade(row) for row in rows]

  async def get_recent_opportunities(self, limit: int = 50) -> list[dict[str, Any]]:
    assert self._conn is not None
    cursor = await self._conn.execute(
      """
      SELECT * FROM opportunities ORDER BY timestamp DESC LIMIT ?
      """,
      (limit,),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]

  async def get_equity_curve(self) -> list[PortfolioSnapshot]:
    assert self._conn is not None
    cursor = await self._conn.execute(
      "SELECT * FROM portfolio_snapshots ORDER BY timestamp ASC"
    )
    rows = await cursor.fetchall()
    return [
      PortfolioSnapshot(
        timestamp=datetime.fromisoformat(row["timestamp"]),
        balance_usd=float(row["balance_usd"]),
        total_trades=int(row["total_trades"]),
        win_rate=float(row["win_rate"]),
        total_pnl_usd=float(row["total_pnl_usd"]),
      )
      for row in rows
    ]

  async def get_portfolio_status(self, last_n: int = 5) -> PortfolioStatus:
    balance = await self.get_balance()
    starting = await self.get_starting_capital()
    total_trades = await self.count_trades()
    winning = await self.count_winning_trades()
    win_rate = (winning / total_trades) if total_trades else 0.0
    return PortfolioStatus(
      balance_usd=balance,
      starting_capital_usd=starting,
      total_pnl_usd=balance - starting,
      total_trades=total_trades,
      winning_trades=winning,
      win_rate=win_rate,
      last_trades=await self.get_recent_trades(last_n),
    )

  async def record_snapshot(self) -> None:
    status = await self.get_portfolio_status()
    assert self._conn is not None
    await self._conn.execute(
      """
      INSERT INTO portfolio_snapshots (
        timestamp, balance_usd, total_trades, win_rate, total_pnl_usd
      ) VALUES (?, ?, ?, ?, ?)
      """,
      (
        datetime.utcnow().isoformat(),
        status.balance_usd,
        status.total_trades,
        status.win_rate,
        status.total_pnl_usd,
      ),
    )
    await self._conn.commit()

  async def maybe_record_snapshot(self) -> None:
    total_trades = await self.count_trades()
    if total_trades == 0:
      return
    if total_trades % self._config.snapshot_every_n_trades == 0:
      await self.record_snapshot()

  @staticmethod
  def _row_to_trade(row: aiosqlite.Row) -> PaperTrade:
    return PaperTrade(
      id=row["id"],
      opportunity_id=row["opportunity_id"],
      timestamp=datetime.fromisoformat(row["timestamp"]),
      chain=row["chain"],
      pair_label=row["pair_label"],
      buy_dex=row["buy_dex"],
      sell_dex=row["sell_dex"],
      trade_size_usd=float(row["trade_size_usd"]),
      buy_price_usd=float(row["buy_price_usd"]),
      sell_price_usd=float(row["sell_price_usd"]),
      gross_pnl_usd=float(row["gross_pnl_usd"]),
      fees_usd=float(row["fees_usd"]),
      gas_cost_usd=float(row["gas_cost_usd"]),
      mev_haircut_usd=float(row["mev_haircut_usd"]),
      net_pnl_usd=float(row["net_pnl_usd"]),
      balance_after_usd=float(row["balance_after_usd"]),
    )
