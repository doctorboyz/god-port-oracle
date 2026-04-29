"""Metty CLI — command-line interface for the MT5 execution bridge."""

from __future__ import annotations

import click


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


if __name__ == "__main__":
    main()