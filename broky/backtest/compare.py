"""Multi-strategy backtest comparison — run multiple configs side by side.

Usage:
    python -m broky.backtest.compare --configs conservative moderate aggressive
    python -m broky.backtest.compare --timeframe M5 --initial-equity 1000
    python -m broky.backtest.compare --slippage 3 --output results.json
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pandas as pd

from broky.backtest.engine import BacktestEngine, BacktestResult


# Predefined strategy configs for quick comparison
PRESET_CONFIGS = {
    "conservative": {
        "risk_per_trade": 0.01,
        "atr_multiplier": 2.0,
        "risk_reward_ratio": 2.5,
        "min_confidence": 0.55,
        "spread_buffer": 2.0,
        "slippage_bps": 3.0,
    },
    "moderate": {
        "risk_per_trade": 0.02,
        "atr_multiplier": 1.5,
        "risk_reward_ratio": 2.0,
        "min_confidence": 0.50,
        "spread_buffer": 1.5,
        "slippage_bps": 3.0,
    },
    "aggressive": {
        "risk_per_trade": 0.03,
        "atr_multiplier": 1.0,
        "risk_reward_ratio": 1.5,
        "min_confidence": 0.45,
        "spread_buffer": 1.0,
        "slippage_bps": 5.0,
    },
    "scalp_tight": {
        "risk_per_trade": 0.015,
        "atr_multiplier": 1.0,
        "risk_reward_ratio": 1.5,
        "min_confidence": 0.50,
        "spread_buffer": 0.5,
        "max_holding_bars": 12,
        "cooldown_bars": 6,
        "slippage_bps": 3.0,
        "strategy": "m5_scalp",
    },
    "swing_wide": {
        "risk_per_trade": 0.02,
        "atr_multiplier": 2.5,
        "risk_reward_ratio": 3.0,
        "min_confidence": 0.55,
        "spread_buffer": 3.0,
        "max_holding_bars": 96,
        "cooldown_bars": 24,
        "slippage_bps": 2.0,
    },
}


@dataclass
class ComparisonResult:
    """Results for a single strategy in a comparison."""
    name: str
    total_trades: int
    win_rate: float
    total_pnl: float
    total_pnl_pct: float
    max_drawdown_pct: float
    profit_factor: float
    sharpe_ratio: float
    avg_trade_pnl: float
    max_consecutive_wins: int
    max_consecutive_losses: int
    liquidated: bool


def run_comparison(
    df: pd.DataFrame,
    configs: dict[str, dict],
    initial_equity: float = 1000.0,
    d1_df: Optional[pd.DataFrame] = None,
    warmup: int = 50,
) -> list[ComparisonResult]:
    """Run backtest for each config and return comparison results.

    Args:
        df: DataFrame with OHLCV data and DatetimeIndex.
        configs: Dict of {name: config_kwargs} for BacktestEngine.
        initial_equity: Starting equity for all runs.
        d1_df: Optional D1 DataFrame for multi-timeframe filter.
        warmup: Number of initial candles to skip.

    Returns:
        List of ComparisonResult sorted by total PnL descending.
    """
    results = []
    for name, kwargs in configs.items():
        engine = BacktestEngine(initial_equity=initial_equity, **kwargs)
        result = engine.run(df, warmup=warmup, d1_df=d1_df)
        results.append(ComparisonResult(
            name=name,
            total_trades=result.total_trades,
            win_rate=result.win_rate,
            total_pnl=result.total_pnl,
            total_pnl_pct=result.total_pnl_pct,
            max_drawdown_pct=result.max_drawdown_pct,
            profit_factor=result.profit_factor,
            sharpe_ratio=result.sharpe_ratio,
            avg_trade_pnl=result.avg_trade_pnl,
            max_consecutive_wins=result.max_consecutive_wins,
            max_consecutive_losses=result.max_consecutive_losses,
            liquidated=result.liquidated,
        ))
    return sorted(results, key=lambda r: r.total_pnl, reverse=True)


def format_table(results: list[ComparisonResult]) -> str:
    """Format comparison results as an aligned text table.

    Args:
        results: List of ComparisonResult to display.

    Returns:
        Formatted string table.
    """
    if not results:
        return "No results to display."

    header = (
        f"{'Strategy':<16} {'Trades':>6} {'WR':>6} {'PnL':>10} "
        f"{'PnL%':>7} {'MaxDD':>7} {'PF':>6} {'Sharpe':>7} {'Avg$':>8} {'Liq':>4}"
    )
    sep = "-" * len(header)
    rows = []
    for r in results:
        liq = "YES" if r.liquidated else ""
        wr = f"{r.win_rate:.1%}" if r.total_trades > 0 else "N/A"
        pf = f"{r.profit_factor:.2f}" if r.profit_factor != float("inf") else "INF"
        rows.append(
            f"{r.name:<16} {r.total_trades:>6} {wr:>6} {r.total_pnl:>10.2f} "
            f"{r.total_pnl_pct:>+6.1f}% {r.max_drawdown_pct:>6.1f}% {pf:>6} "
            f"{r.sharpe_ratio:>7.2f} {r.avg_trade_pnl:>8.2f} {liq:>4}"
        )
    return f"{header}\n{sep}\n" + "\n".join(rows)


def to_dataframe(results: list[ComparisonResult]) -> pd.DataFrame:
    """Convert comparison results to a pandas DataFrame.

    Args:
        results: List of ComparisonResult.

    Returns:
        DataFrame with one row per strategy.
    """
    return pd.DataFrame([
        {
            "strategy": r.name,
            "trades": r.total_trades,
            "win_rate": r.win_rate,
            "pnl": r.total_pnl,
            "pnl_pct": r.total_pnl_pct,
            "max_dd_pct": r.max_drawdown_pct,
            "profit_factor": r.profit_factor,
            "sharpe": r.sharpe_ratio,
            "avg_trade": r.avg_trade_pnl,
            "max_consec_wins": r.max_consecutive_wins,
            "max_consec_losses": r.max_consecutive_losses,
            "liquidated": r.liquidated,
        }
        for r in results
    ])


def main():
    """CLI entry point for multi-strategy comparison."""
    import argparse

    parser = argparse.ArgumentParser(description="Multi-strategy backtest comparison")
    parser.add_argument(
        "--configs", nargs="+", default=["conservative", "moderate", "aggressive"],
        choices=list(PRESET_CONFIGS.keys()),
        help="Strategy configs to compare",
    )
    parser.add_argument("--timeframe", default="M5", help="Timeframe (M5, H1, D1)")
    parser.add_argument("--initial-equity", type=float, default=1000.0, help="Starting equity")
    parser.add_argument("--warmup", type=int, default=50, help="Warmup candles")
    parser.add_argument("--output", default=None, help="Output JSON path")
    parser.add_argument("--csv", default=None, help="Output CSV path")
    args = parser.parse_args()

    from broky.data.loader import load_timeframe

    data_dir = Path("data/xau-data")
    if not data_dir.exists():
        print(f"Error: Data directory {data_dir} not found.", file=sys.stderr)
        sys.exit(1)

    print(f"Loading {args.timeframe} data...")
    try:
        df = load_timeframe(data_dir, args.timeframe)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    configs = {name: PRESET_CONFIGS[name] for name in args.configs}
    print(f"Loaded {len(df)} candles. Comparing {len(configs)} strategies...\n")

    results = run_comparison(
        df, configs,
        initial_equity=args.initial_equity,
        warmup=args.warmup,
    )

    print(format_table(results))

    if args.output:
        data = [
            {
                "name": r.name, "total_trades": r.total_trades,
                "win_rate": r.win_rate, "total_pnl": r.total_pnl,
                "total_pnl_pct": r.total_pnl_pct, "max_drawdown_pct": r.max_drawdown_pct,
                "profit_factor": r.profit_factor, "sharpe_ratio": r.sharpe_ratio,
                "avg_trade_pnl": r.avg_trade_pnl, "liquidated": r.liquidated,
            }
            for r in results
        ]
        Path(args.output).write_text(json.dumps(data, indent=2))
        print(f"\nResults saved to {args.output}")

    if args.csv:
        to_dataframe(results).to_csv(args.csv, index=False)
        print(f"CSV saved to {args.csv}")


if __name__ == "__main__":
    main()