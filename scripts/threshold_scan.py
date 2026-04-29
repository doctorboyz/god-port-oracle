"""Scan confidence thresholds to find optimal trade quality vs quantity."""

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

    print(f"H1: {len(df_h1)} candles ({df_h1.index[0].date()} → {df_h1.index[-1].date()})")
    print(f"D1: {len(df_d1)} candles")
    print()

    # Scan confidence thresholds (no MTF for speed)
    print(f"{'CONF':>5} {'MH':>4} {'Trades':>6} {'WR':>6} {'PF':>6} {'PnL%':>8} {'MaxDD':>6}")
    print(f"{'-'*5} {'-'*4} {'-'*6} {'-'*6} {'-'*6} {'-'*8} {'-'*6}")

    for conf in [0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80]:
        for mh in [0, 24, 48]:
            engine = BacktestEngine(
                initial_equity=1000,
                risk_per_trade=0.02,
                atr_multiplier=1.5,
                risk_reward_ratio=2.5,
                min_confidence=conf,
                max_holding_bars=mh,
            )
            r = engine.run(df_h1, warmup=50)
            print(f"{conf:>5.2f} {mh:>4} {r.total_trades:>6} {r.win_rate:>5.0%} "
                  f"{r.profit_factor:>6.2f} {r.total_pnl_pct:>+7.1f}% {r.max_drawdown_pct:>5.1f}%")

    # MTF comparison at best thresholds
    print(f"\n--- MTF Comparison ---")
    print(f"{'CONF':>5} {'MH':>4} {'MTF':>4} {'Trades':>6} {'WR':>6} {'PF':>6} {'PnL%':>8} {'MaxDD':>6}")
    for conf in [0.60, 0.65, 0.70]:
        for mh in [0, 48]:
            for mtf in [False, True]:
                engine = BacktestEngine(
                    initial_equity=1000,
                    risk_per_trade=0.02,
                    atr_multiplier=1.5,
                    risk_reward_ratio=2.5,
                    min_confidence=conf,
                    max_holding_bars=mh,
                )
                r = engine.run(df_h1, warmup=50, d1_df=df_d1 if mtf else None)
                print(f"{conf:>5.2f} {mh:>4} {'Y' if mtf else 'N':>4} {r.total_trades:>6} {r.win_rate:>5.0%} "
                      f"{r.profit_factor:>6.2f} {r.total_pnl_pct:>+7.1f}% {r.max_drawdown_pct:>5.1f}%")


if __name__ == "__main__":
    main()