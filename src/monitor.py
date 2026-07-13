"""CLI-only engine loop with periodic P&L reporting."""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime

from src.config import load_config, resolve_db_path
from src.engine import ArbitrageEngine
from src.ledger import Ledger


async def run_monitor(cycles: int = 0) -> None:
  config = load_config()
  engine = await ArbitrageEngine.create()
  ledger = engine._ledger
  cycle = 0
  start_balance = await ledger.get_balance()

  print(f"Monitor started | balance=${start_balance:,.2f} | cycles={'∞' if cycles == 0 else cycles}")
  try:
    while cycles == 0 or cycle < cycles:
      cycle += 1
      await engine.run_once()
      balance = await ledger.get_balance()
      pnl = balance - start_balance
      trades = await ledger.count_trades()
      ts = datetime.now().strftime("%H:%M:%S")
      print(f"[{ts}] cycle={cycle} balance=${balance:,.2f} session_pnl=${pnl:+.2f} trades={trades}")
      await asyncio.sleep(config.scan_interval_sec)
  except KeyboardInterrupt:
    print("\nMonitor stopped.")
  finally:
    await engine.close()


def main() -> None:
  n = int(sys.argv[1]) if len(sys.argv) > 1 else 0
  asyncio.run(run_monitor(n))


if __name__ == "__main__":
  main()
