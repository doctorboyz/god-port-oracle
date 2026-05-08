"""S4: Threshold sweep — find optimal min_confidence for PF>=1.5, WR>=50%, trades>=30/year."""

import sys
sys.path.insert(0, ".")

import pandas as pd
from broky.data.loader import load_timeframe
from broky.backtest.engine import BacktestEngine


def main():
    data_dir = "data/xau-data"

    # Load H1 data (2024 onwards per threshold_scan.py)
    df_h1 = load_timeframe(data_dir, "H1")
    cutoff = pd.Timestamp("2024-01-01")
    df_h1 = df_h1[df_h1.index >= cutoff]

    # Load D1 data (with warmup)
    df_d1 = load_timeframe(data_dir, "D1")
    df_d1 = df_d1[df_d1.index >= cutoff - pd.Timedelta(days=400)]

    # Calculate date range and years for trades/year
    date_range = df_h1.index[-1] - df_h1.index[0]
    years = date_range.days / 365.25

    print(f"H1: {len(df_h1)} candles ({df_h1.index[0].date()} → {df_h1.index[-1].date()})")
    print(f"D1: {len(df_d1)} candles")
    print(f"Period: {years:.2f} years")
    print()

    # S4: Sweep min_confidence at 0.40, 0.45, 0.50, 0.55, 0.60
    thresholds = [0.40, 0.45, 0.50, 0.55, 0.60]

    print("=" * 90)
    print("S4: THRESHOLD SWEEP (no MTF, no session filter)")
    print("=" * 90)
    print(f"{'CONF':>5} {'MH':>4} {'Trades':>6} {'Tr/Yr':>6} {'WR':>6} {'PF':>6} {'PnL%':>8} {'MaxDD':>6} {'PASS':>6}")
    print(f"{'-'*5} {'-'*4} {'-'*6} {'-'*6} {'-'*6} {'-'*6} {'-'*8} {'-'*6} {'-'*6}")

    results = []
    for conf in thresholds:
        for mh in [0, 24, 48]:
            engine = BacktestEngine(
                initial_equity=1000,
                risk_per_trade=0.02,
                atr_multiplier=1.5,
                risk_reward_ratio=2.5,
                min_confidence=conf,
                max_holding_bars=mh,
                cooldown_bars=12,
            )
            r = engine.run(df_h1, warmup=50)
            trades_per_year = r.total_trades / years if years > 0 else 0
            # KPI check: PF>=1.5, WR>=50%, trades/year>=30
            passed = r.profit_factor >= 1.5 and r.win_rate >= 0.50 and trades_per_year >= 30
            mark = "  ✓" if passed else ""
            print(f"{conf:>5.2f} {mh:>4} {r.total_trades:>6} {trades_per_year:>5.1f}/y "
                  f"{r.win_rate:>5.0%} {r.profit_factor:>6.2f} {r.total_pnl_pct:>+7.1f}% "
                  f"{r.max_drawdown_pct:>5.1f}%{mark}")
            results.append((conf, mh, r, trades_per_year, passed))

    # Find best result
    best = [r for r in results if r[4]]
    if best:
        print(f"\n{'='*90}")
        print("PASSING CONFIGS (PF≥1.5, WR≥50%, ≥30 trades/yr):")
        print(f"{'='*90}")
        for conf, mh, r, tpy, passed in best:
            print(f"  conf={conf:.2f} mh={mh}: {r.total_trades} trades, WR={r.win_rate:.0%}, "
                  f"PF={r.profit_factor:.2f}, PnL={r.total_pnl_pct:+.1f}%, MaxDD={r.max_drawdown_pct:.1f}%")
    else:
        print(f"\n{'='*90}")
        print("NO CONFIG PASSES ALL THREE KPIs simultaneously")
        print(f"{'='*90}")
        # Show closest
        by_pf = sorted(results, key=lambda x: x[2].profit_factor, reverse=True)[:3]
        print("\nClosest by Profit Factor:")
        for conf, mh, r, tpy, _ in by_pf:
            print(f"  conf={conf:.2f} mh={mh}: PF={r.profit_factor:.2f} WR={r.win_rate:.0%} trades/yr={tpy:.1f}")
        by_wr = sorted(results, key=lambda x: x[2].win_rate, reverse=True)[:3]
        print("\nClosest by Win Rate:")
        for conf, mh, r, tpy, _ in by_wr:
            print(f"  conf={conf:.2f} mh={mh}: WR={r.win_rate:.0%} PF={r.profit_factor:.2f} trades/yr={tpy:.1f}")
        by_trades = sorted(results, key=lambda x: x[3], reverse=True)[:3]
        print("\nClosest by Trade Frequency:")
        for conf, mh, r, tpy, _ in by_trades:
            print(f"  conf={conf:.2f} mh={mh}: trades/yr={tpy:.1f} WR={r.win_rate:.0%} PF={r.profit_factor:.2f}")


if __name__ == "__main__":
    main()