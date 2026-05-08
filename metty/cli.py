"""Metty CLI — command-line interface for the MT5 execution bridge."""

from __future__ import annotations

import click
import logging
from pathlib import Path

from metty.core.db import get_snapshot_count


@click.group()
@click.version_option(version="0.1.0")
def main():
    """Metty — Execution bridge for XAUUSD trading via MT5."""
    pass


@main.command()
def ping():
    """Check MT5 bridge connection."""
    click.echo("Checking MT5 bridge connection...")
    click.echo("Bridge not yet connected. Implement bridge connection first.")


@main.command()
@click.option("--signal", required=True, help="Path to signal JSON file")
def execute(signal: str):
    """Execute a trade from a signal file."""
    click.echo(f"Executing signal from: {signal}")
    click.echo("Execution not yet implemented. Build bridge connection first.")


@main.command()
def status():
    """Show system status — bridge, positions, and circuit breaker."""
    click.echo("=== Metty Status ===")
    click.echo("Bridge: Not connected")
    click.echo("Positions: 0")
    click.echo("Circuit breaker: Inactive")


@main.command("collect-historical")
@click.option("--data-dir", default="data/xau-data", help="Path to historical CSV data")
@click.option("--warmup", default=200, type=int, help="Warmup bars before collection")
@click.option("--sample-every", default=12, type=int, help="Sample every N M5 bars")
@click.option("--db-path", default=None, help="Path to SQLite database")
@click.option("--verbose", is_flag=True, help="Enable debug logging")
def collect_historical(data_dir: str, warmup: int, sample_every: int, db_path: str | None, verbose: bool):
    """Collect feature snapshots from historical CSV data for ML training."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    db = Path(db_path) if db_path else None

    click.echo(f"Collecting historical data from: {data_dir}")
    click.echo(f"Warmup: {warmup} bars, Sample every: {sample_every} M5 bars")

    from metty.execution.historical_collector import HistoricalCollector

    collector = HistoricalCollector(
        data_dir=data_dir,
        db_path=db,
        warmup=warmup,
        sample_every=sample_every,
    )

    result = collector.collect()

    click.echo(f"\n=== Collection Results ===")
    click.echo(f"Total M5 bars: {result['total_bars']}")
    click.echo(f"Sampled bars: {result['sampled_bars']}")
    click.echo(f"Snapshots written: {result['snapshots_written']}")
    click.echo(f"Errors: {result['errors']}")

    count = get_snapshot_count(db)
    click.echo(f"\nTotal snapshots in DB: {count}")


@main.command("collect-live")
@click.option("--interval", default=300, type=int, help="Poll interval in seconds (default 300 = 5min for M5)")
@click.option("--cycles", default=0, type=int, help="Max cycles (0 = unlimited)")
@click.option("--account", default="A", help="Account: A, B, or C")
@click.option("--db-path", default=None, help="Path to SQLite database")
@click.option("--data-dir", default="data/xau-data", help="Fallback CSV data directory")
@click.option("--verbose", is_flag=True, help="Enable debug logging")
def collect_live(interval: int, cycles: int, account: str, db_path: str | None, data_dir: str, verbose: bool):
    """Collect feature snapshots from live data (MT5 bridge or CSV fallback).

    Each cycle fetches candles, computes indicators, enriches with
    sentiment (Fear & Greed, news), and writes a feature snapshot.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    db = Path(db_path) if db_path else None

    click.echo(f"Starting live collection: account={account}, interval={interval}s, max_cycles={cycles}")

    from dotenv import load_dotenv
    load_dotenv()

    from metty.execution.live_collector import LiveCollector

    collector = LiveCollector(
        account=account,
        db_path=db,
        data_dir=Path(data_dir),
    )

    result = collector.run(interval=interval, max_cycles=cycles)

    click.echo(f"\n=== Live Collection Results ===")
    click.echo(f"Cycles: {result['cycles']}")
    click.echo(f"Snapshots: {result['snapshots']}")
    click.echo(f"Errors: {result['errors']}")

    count = get_snapshot_count(db)
    click.echo(f"Total snapshots in DB: {count}")


@main.command("trade-live")
@click.option("--account", default="B", help="Account: A, B, or C")
@click.option("--interval", default=300, type=int, help="Poll interval in seconds (default 300 = 5min)")
@click.option("--cycles", default=0, type=int, help="Max cycles (0 = unlimited)")
@click.option("--db-path", default=None, help="Path to SQLite database")
@click.option("--data-dir", default="data/xau-data", help="Fallback CSV data directory")
@click.option("--dry-run", is_flag=True, help="Dry run mode — log signals but don't send orders")
@click.option("--min-confidence", default=0.60, type=float, help="Minimum signal confidence to trade")
@click.option("--risk-pct", default=None, type=float, help="Risk per trade as decimal (e.g., 0.02)")
@click.option("--verbose", is_flag=True, help="Enable debug logging")
def trade_live(
    account: str, interval: int, cycles: int, db_path: str | None,
    data_dir: str, dry_run: bool, min_confidence: float,
    risk_pct: float | None, verbose: bool,
):
    """Live trading: generate signals and execute trades via MT5 bridge.

    Each cycle fetches candles, generates a signal, checks risk,
    and (if signal is strong enough) sends an order to MT5.

    Use --dry-run to see signals without executing orders.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    db = Path(db_path) if db_path else None

    from dotenv import load_dotenv
    load_dotenv()

    from metty.execution.live_trader import LiveTrader, RiskConfig

    risk_config = RiskConfig(min_confidence=min_confidence)
    if risk_pct is not None:
        risk_config.risk_per_trade = risk_pct

    mode = "DRY-RUN" if dry_run else "LIVE"
    click.echo(f"Starting {mode} trading: account={account}, interval={interval}s, cycles={cycles}")
    click.echo(f"Risk: {risk_config.risk_per_trade:.1%} per trade, min confidence={min_confidence}")

    trader = LiveTrader(
        account=account,
        db_path=db,
        data_dir=Path(data_dir),
        dry_run=dry_run,
        risk_config=risk_config,
    )

    result = trader.run(interval=interval, max_cycles=cycles)

    click.echo(f"\n=== {mode} Trading Results ===")
    click.echo(f"Cycles: {result['cycles']}")
    click.echo(f"Trades opened: {result['trades_opened']}")
    click.echo(f"Holds: {result['holds']}")
    click.echo(f"Errors: {result['errors']}")


if __name__ == "__main__":
    main()