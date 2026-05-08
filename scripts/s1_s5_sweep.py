"""S1+S5 combined sweep: Bollinger/ADX adjustment + MTF hard filter."""

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

    # S1+S5: Sweep with Bollinger/ADX fix + MTF
    thresholds = [0.45, 0.50, 0.55, 0.60]

    print("=" * 100)
    print("S1+S5: Bollinger/ADX adjustment + MTF hard filter")
    print("=" * 100)
    print(f"{'CONF':>5} {'MH':>4} {'MTF':>4} {'Trades':>6} {'Tr/Yr':>6} {'WR':>6} {'PF':>6} {'PnL%':>8} {'MaxDD':>6} {'PASS':>6}")
    print(f"{'-'*5} {'-'*4} {'-'*4} {'-'*6} {'-'*6} {'-'*6} {'-'*6} {'-'*8} {'-'*6} {'-'*6}")

    results = []
    for conf in thresholds:
        for mh in [0, 24, 48]:
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
                print(f"{conf:>5.2f} {mh:>4} {mtf_label:>4} {r.total_trades:>6} {trades_per_year:>5.1f}/y "
                      f"{r.win_rate:>5.0%} {r.profit_factor:>6.2f} {r.total_pnl_pct:>+7.1f}% "
                      f"{r.max_drawdown_pct:>5.1f}%{mark}")
                results.append((conf, mh, mtf, r, trades_per_year, passed))

    # Summary
    passing = [r for r in results if r[5]]
    if passing:
        print(f"\n{'='*100}")
        print(f"PASSING CONFIGS (PF≥1.5, WR≥50%, ≥30 trades/yr) — {len(passing)} found:")
        print(f"{'='*100}")
        for conf, mh, mtf, r, tpy, _ in passing:
            print(f"  conf={conf:.2f} mh={mh} MTF={'Y' if mtf else 'N'}: "
                  f"{r.total_trades} trades, WR={r.win_rate:.0%}, PF={r.profit_factor:.2f}, "
                  f"PnL={r.total_pnl_pct:+.1f}%, MaxDD={r.max_drawdown_pct:.1f}%")
    else:
        print(f"\n{'='*100}")
        print("NO CONFIG PASSES ALL THREE KPIs")
        print(f"{'='*100}")
        by_wr = sorted(results, key=lambda x: x[3].win_rate, reverse=True)[:5]
        print("\nTop 5 by Win Rate:")
        for conf, mh, mtf, r, tpy, _ in by_wr:
            print(f"  conf={conf:.2f} mh={mh} MTF={'Y' if mtf else 'N'}: "
                  f"WR={r.win_rate:.0%} PF={r.profit_factor:.2f} trades/yr={tpy:.1f}")


if __name__ == "__main__":
    main()