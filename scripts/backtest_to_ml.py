#!/usr/bin/env python3
"""Backtest-to-ML pipeline — generate synthetic trade_outcomes from historical data.

Usage:
    # Target bearish D1 periods (Phase 1: M5-backed, Jun 2023+)
    python scripts/backtest_to_ml.py --target-regimes bearish --verbose

    # Full range (all D1 regimes)
    python scripts/backtest_to_ml.py --full-range --verbose

    # Phase 2: H1 fallback for pre-2023 bearish data
    python scripts/backtest_to_ml.py --target-regimes bearish --use-h1-fallback --verbose

    # Dry run (no DB writes)
    python scripts/backtest_to_ml.py --dry-run --verbose
"""

import argparse
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from broky.backtest.synth_pipeline import BacktestToMLPipeline


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate synthetic ML training data from backtests",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Target bearish D1 periods (default)
  python scripts/backtest_to_ml.py --target-regimes bearish --verbose

  # All regimes, full date range
  python scripts/backtest_to_ml.py --full-range --verbose

  # H1 fallback for pre-2023 bearish data
  python scripts/backtest_to_ml.py --target-regimes bearish --use-h1-fallback --verbose

  # Dry run (no DB writes)
  python scripts/backtest_to_ml.py --dry-run --verbose
        """,
    )

    parser.add_argument(
        "--data-dir",
        type=str,
        default="data/xau-data",
        help="Directory containing XAUUSD CSV files (default: data/xau-data)",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="Path to SQLite database (default: data/oracle.db)",
    )
    parser.add_argument(
        "--strategy",
        type=str,
        default="swing",
        choices=["swing", "m5_scalp"],
        help="Trading strategy to backtest (default: swing)",
    )
    parser.add_argument(
        "--primary-tf",
        type=str,
        default="H1",
        choices=["M5", "H1", "H4"],
        help="Primary timeframe for backtesting (default: H1)",
    )
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=0.30,
        help="Minimum signal confidence threshold (default: 0.30, lower than live 0.60)",
    )
    parser.add_argument(
        "--risk-per-trade",
        type=float,
        default=0.02,
        help="Risk per trade as fraction of equity (default: 0.02)",
    )
    parser.add_argument(
        "--atr-multiplier",
        type=float,
        default=1.5,
        help="ATR multiplier for stop loss (default: 1.5)",
    )
    parser.add_argument(
        "--risk-reward-ratio",
        type=float,
        default=2.0,
        help="Risk-reward ratio for take profit (default: 2.0)",
    )
    parser.add_argument(
        "--initial-equity",
        type=float,
        default=10000,
        help="Starting equity for backtest (default: 10000)",
    )
    parser.add_argument(
        "--max-holding-bars",
        type=int,
        default=48,
        help="Max holding bars before forced exit (default: 48)",
    )
    parser.add_argument(
        "--cooldown-bars",
        type=int,
        default=12,
        help="Min bars between trades (default: 12)",
    )
    parser.add_argument(
        "--slippage-bps",
        type=float,
        default=3.0,
        help="Slippage in basis points (default: 3.0)",
    )
    parser.add_argument(
        "--target-regimes",
        nargs="+",
        default=["bearish"],
        choices=["bearish", "bullish", "trending", "ranging", "volatile"],
        help="Target D1 regimes for synthetic data (default: bearish)",
    )
    parser.add_argument(
        "--full-range",
        action="store_true",
        help="Run on all data, not just target regimes",
    )
    parser.add_argument(
        "--use-h1-fallback",
        action="store_true",
        help="Use H1 instead of M5 for pre-2023 data where M5 is unavailable",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run pipeline but don't write to database",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print detailed progress information",
    )

    args = parser.parse_args()

    # Determine target regimes
    target_regimes = None if args.full_range else args.target_regimes

    # Default db path
    db_path = args.db_path or str(project_root / "data" / "oracle.db")

    print(f"{'='*60}")
    print(f"Backtest-to-ML Pipeline")
    print(f"{'='*60}")
    print(f"  Data dir:       {args.data_dir}")
    print(f"  DB path:        {db_path}")
    print(f"  Strategy:       {args.strategy}")
    print(f"  Primary TF:     {args.primary_tf}")
    print(f"  Min confidence:  {args.min_confidence}")
    print(f"  Learning mode:  True (relaxed trend filters)")
    print(f"  Target regimes: {target_regimes or 'ALL'}")
    print(f"  H1 fallback:    {args.use_h1_fallback}")
    print(f"  Dry run:        {args.dry_run}")
    print(f"{'='*60}")

    if args.dry_run:
        print("  ⚠  DRY RUN — no database writes will be made")

    pipeline = BacktestToMLPipeline(
        data_dir=args.data_dir,
        db_path=db_path,
        strategy=args.strategy,
        primary_tf=args.primary_tf,
        min_confidence=args.min_confidence,
        learning_mode=True,  # Always on for synthetic data
        risk_per_trade=args.risk_per_trade,
        atr_multiplier=args.atr_multiplier,
        risk_reward_ratio=args.risk_reward_ratio,
        initial_equity=args.initial_equity,
        max_holding_bars=args.max_holding_bars,
        cooldown_bars=args.cooldown_bars,
        slippage_bps=args.slippage_bps,
        target_regimes=target_regimes,
        use_h1_fallback=args.use_h1_fallback,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )

    try:
        stats = pipeline.run()

        print(f"\n{'='*60}")
        print(f"Results Summary")
        print(f"{'='*60}")
        print(f"  Total trades:     {stats['total_trades']}")
        print(f"  BUY trades:      {stats['buy_trades']}")
        print(f"  SELL trades:     {stats['sell_trades']}")
        print(f"  WIN:             {stats['wins']}")
        print(f"  LOSS:            {stats['losses']}")
        if stats['total_trades'] > 0:
            print(f"  Win rate:        {stats['wins']/stats['total_trades']:.1%}")
        print(f"  Bearish SELL:    {stats['bearish_sell']}")
        print(f"  Bullish BUY:     {stats['bullish_buy']}")
        print(f"  DB rows written: {stats['db_writes']}")

        if not args.dry_run and stats['db_writes'] > 0:
            print(f"\n  ✅ Synthetic data written to database")
            print(f"  Retrain ML model with:")
            print(f"    python -m broky.ml.trade_outcome_trainer --db-path {db_path} --version v4")
        elif args.dry_run and stats['total_trades'] > 0:
            print(f"\n  ⊘ Dry run — no data written")
            print(f"  Re-run without --dry-run to write to database")
        else:
            print(f"\n  ⚠ No trades generated — check data and parameters")

    except FileNotFoundError as e:
        print(f"\n  ✗ Data file not found: {e}")
        print(f"  Make sure CSV files exist in {args.data_dir}/")
        sys.exit(1)
    except Exception as e:
        print(f"\n  ✗ Pipeline error: {e}")
        raise


if __name__ == "__main__":
    main()