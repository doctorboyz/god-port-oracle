"""S5: Multi-timeframe confirmation sweep — compare MTF on vs off at various thresholds."""

import sys
sys.path.insert(0, ".")

import pandas as pd
from broky.data.loader import load_timeframe
from broky.backtest.engine import BacktestEngine


def main():
    data_dir = "data/xau-data"

    df_h1 = load_timeframe(data_dir, "H1")
    cutoff = pd.Timestamp("2024-01-01")
    df_h1 = df_h1[df_h1.index >= cutoff]

    df_d1 = load_timeframe(data_dir, "D1")
    df_d1 = df_d1[df_d1.index >= cutoff - pd.Timedelta(days=400)]

    date_range = df_h1.index[-1] - df_h1.index[0]
    years = date_range.days / 365.25

    print(f"H1: {len(df_h1)} candles ({df_h1.index[0].date()} → {df_h1.index[-1].date()})")
    print(f"D1: {len(df_d1)} candles")
    print(f"Period: {years:.2f} years")
    print()

    # S5: MTF comparison at best thresholds from S4 (conf 0.45-0.60, mh=24)
    thresholds = [0.45, 0.50, 0.55, 0.60]
    mh = 24  # Best MH from S4

    print("=" * 95)
    print("S5: MTF COMPARISON (MH=24, best thresholds from S4)")
    print("=" * 95)
    print(f"{'CONF':>5} {'MTF':>4} {'Trades':>6} {'Tr/Yr':>6} {'WR':>6} {'PF':>6} {'PnL%':>8} {'MaxDD':>6} {'PASS':>6}")
    print(f"{'-'*5} {'-'*4} {'-'*6} {'-'*6} {'-'*6} {'-'*6} {'-'*8} {'-'*6} {'-'*6}")

    results = []
    for conf in thresholds:
        for mtf in [False, True]:
            engine = BacktestEngine(
                initial_equity=1000,
                risk_per_trade=0.02,
                atr_multiplier=1.5,
                risk_reward_ratio=2.5,
                min_confidence=conf,
                max_holding_bars=mh,
                cooldown_bars=12,
            )
            r = engine.run(df_h1, warmup=50, d1_df=df_d1 if mtf else None)
            trades_per_year = r.total_trades / years if years > 0 else 0
            passed = r.profit_factor >= 1.5 and r.win_rate >= 0.50 and trades_per_year >= 30
            mark = "  ✓" if passed else ""
            mtf_label = "Y" if mtf else "N"
            print(f"{conf:>5.2f} {mtf_label:>4} {r.total_trades:>6} {trades_per_year:>5.1f}/y "
                  f"{r.win_rate:>5.0%} {r.profit_factor:>6.2f} {r.total_pnl_pct:>+7.1f}% "
                  f"{r.max_drawdown_pct:>5.1f}%{mark}")
            results.append((conf, mh, mtf, r, trades_per_year, passed))

    # Summary: MTF improvement
    print(f"\n{'='*95}")
    print("MTF IMPROVEMENT ANALYSIS")
    print(f"{'='*95}")
    for conf in thresholds:
        off = [r for r in results if r[0] == conf and not r[2]]
        on = [r for r in results if r[0] == conf and r[2]]
        if off and on:
            off_r = off[0][3]
            on_r = on[0][3]
            wr_diff = (on_r.win_rate - off_r.win_rate) * 100
            pf_diff = on_r.profit_factor - off_r.profit_factor
            trade_diff = on_r.total_trades - off_r.total_trades
            dd_diff = on_r.max_drawdown_pct - off_r.max_drawdown_pct
            print(f"  conf={conf:.2f}: MTF → WR {wr_diff:+.1f}pp, PF {pf_diff:+.2f}, "
                  f"trades {trade_diff:+d}, MaxDD {dd_diff:+.1f}pp")

    # Also test hard MTF filter (block counter-trend entirely) — need code change
    # For now, report soft filter results
    print(f"\n{'='*95}")
    print("NOTE: Current MTF is SOFT filter (*0.5 confidence for counter-trend)")
    print("Hard filter (block counter-trend) may improve WR further — next step")
    print(f"{'='*95}")

    # Find best
    best = [r for r in results if r[5]]
    if best:
        print("\nPASSING CONFIGS (PF≥1.5, WR≥50%, ≥30 trades/yr):")
        for conf, mh, mtf, r, tpy, _ in best:
            print(f"  conf={conf:.2f} mh={mh} MTF={'Y' if mtf else 'N'}: "
                  f"{r.total_trades} trades, WR={r.win_rate:.0%}, PF={r.profit_factor:.2f}")
    else:
        print("\nNo config passes all three KPIs yet.")
        # Show best
        by_wr = sorted(results, key=lambda x: x[3].win_rate, reverse=True)[0]
        by_pf = sorted(results, key=lambda x: x[3].profit_factor, reverse=True)[0]
        print(f"\nBest WR:  conf={by_wr[0]:.2f} MTF={'Y' if by_wr[2] else 'N'} WR={by_wr[3].win_rate:.0%} PF={by_wr[3].profit_factor:.2f}")
        print(f"Best PF:  conf={by_pf[0]:.2f} MTF={'Y' if by_pf[2] else 'N'} PF={by_pf[3].profit_factor:.2f} WR={by_pf[3].win_rate:.0%}")


if __name__ == "__main__":
    main()