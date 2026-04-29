"""Broky CLI — command-line interface for the trading analysis engine."""

from __future__ import annotations

import click
import json
import sys
from pathlib import Path

import pandas as pd


@click.group()
@click.version_option(version="0.1.0")
def main():
    """Broky — Market Intelligence Cell for XAUUSD trading."""
    pass


@main.command()
@click.option("--config", default=None, help="Path to backtest config YAML")
@click.option("--timeframe", default="M5", help="Timeframe to backtest (M5, H1, D1)")
@click.option("--initial-equity", default=1000.0, type=float, help="Starting equity")
@click.option("--risk-per-trade", default=0.02, type=float, help="Risk per trade (0.02 = 2%)")
@click.option("--output", default=None, help="Output file for results (JSON)")
def backtest(config: str | None, timeframe: str, initial_equity: float, risk_per_trade: float, output: str | None):
    """Run backtest on historical data."""
    from broky.backtest.engine import BacktestEngine
    from broky.data.loader import load_timeframe

    data_dir = Path("data/xau-data")
    if not data_dir.exists():
        click.echo(f"Error: Data directory {data_dir} not found.", err=True)
        sys.exit(1)

    click.echo(f"Loading {timeframe} data...")
    try:
        df = load_timeframe(data_dir, timeframe)
    except FileNotFoundError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    click.echo(f"Loaded {len(df)} candles. Running backtest...")

    engine = BacktestEngine(
        initial_equity=initial_equity,
        risk_per_trade=risk_per_trade,
    )
    result = engine.run(df)

    click.echo(f"\n=== Backtest Results ({timeframe}) ===")
    click.echo(f"Total trades: {result.total_trades}")
    click.echo(f"Win rate: {result.win_rate:.1%}")
    click.echo(f"Total PnL: ${result.total_pnl:.2f} ({result.total_pnl_pct:.1f}%)")
    click.echo(f"Max drawdown: {result.max_drawdown_pct:.1f}%")
    click.echo(f"Profit factor: {result.profit_factor:.2f}")
    click.echo(f"Sharpe ratio: {result.sharpe_ratio:.2f}")
    click.echo(f"Max consecutive wins: {result.max_consecutive_wins}")
    click.echo(f"Max consecutive losses: {result.max_consecutive_losses}")

    if output:
        result_data = {
            "timeframe": timeframe,
            "initial_equity": initial_equity,
            "total_trades": result.total_trades,
            "win_rate": result.win_rate,
            "total_pnl": result.total_pnl,
            "total_pnl_pct": result.total_pnl_pct,
            "max_drawdown_pct": result.max_drawdown_pct,
            "profit_factor": result.profit_factor,
            "sharpe_ratio": result.sharpe_ratio,
        }
        Path(output).write_text(json.dumps(result_data, indent=2))
        click.echo(f"\nResults saved to {output}")


@main.command()
@click.option("--config", default=None, help="Path to config YAML")
@click.option("--paper", is_flag=True, help="Paper trading mode (no real orders)")
def forward(config: str | None, paper: bool):
    """Run forward test (paper or live)."""
    mode = "paper" if paper else "live"
    click.echo(f"Forward test starting in {mode} mode...")
    click.echo("Not yet implemented. Use backtest to validate strategy first.")


@main.command()
def indicators():
    """Show current indicator values for XAUUSD."""
    from broky.core import get_indicators

    config = get_indicators()
    click.echo("=== Indicator Configuration ===")
    for name, params in config.get("indicators", {}).items():
        click.echo(f"  {name}: {params}")
    click.echo(f"\nThresholds: {config.get('thresholds', {})}")


if __name__ == "__main__":
    main()