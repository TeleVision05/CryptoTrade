from __future__ import annotations

import asyncio

import typer

from src.config import EnvSettings, load_config, resolve_db_path
from src.engine import ArbitrageEngine
from src.ledger import Ledger

app = typer.Typer(help="DEX Arbitrage Paper Trader")


@app.command()
def run() -> None:
  """Start the paper trading engine."""
  asyncio.run(_run_engine())


@app.command()
def status() -> None:
  """Show portfolio status and recent trades."""
  asyncio.run(_show_status())


@app.command()
def reset() -> None:
  """Reset ledger to starting capital."""
  asyncio.run(_reset_ledger())


async def _run_engine() -> None:
  engine = await ArbitrageEngine.create()
  try:
    await engine.run_forever()
  except KeyboardInterrupt:
    engine.stop()
    typer.echo("\nEngine stopped.")
  finally:
    await engine.close()


async def _show_status() -> None:
  config = load_config()
  ledger = Ledger(resolve_db_path(config), config)
  await ledger.connect()
  try:
    status = await ledger.get_portfolio_status(last_n=5)
    typer.echo("=== Portfolio Status ===")
    typer.echo(f"Balance:      ${status.balance_usd:,.2f}")
    typer.echo(f"Starting:     ${status.starting_capital_usd:,.2f}")
    typer.echo(f"Total P&L:    ${status.total_pnl_usd:+,.2f}")
    typer.echo(f"Trades:       {status.total_trades}")
    typer.echo(f"Win rate:     {status.win_rate * 100:.1f}%")
    if status.last_trades:
      typer.echo("\n=== Recent Trades ===")
      for trade in status.last_trades:
        typer.echo(
          f"{trade.timestamp.strftime('%Y-%m-%d %H:%M:%S')} "
          f"{trade.chain.upper():<9} {trade.pair_label:<10} "
          f"net={trade.net_pnl_usd:+.2f} balance=${trade.balance_after_usd:,.2f}"
        )
  finally:
    await ledger.close()


async def _reset_ledger() -> None:
  config = load_config()
  ledger = Ledger(resolve_db_path(config), config)
  await ledger.connect()
  try:
    await ledger.reset()
    typer.echo(f"Ledger reset to ${config.starting_capital_usd:,.2f}")
  finally:
    await ledger.close()


def main() -> None:
  app()


if __name__ == "__main__":
  main()
