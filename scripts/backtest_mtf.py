"""Sweet spot backtest: sweep parameters to find optimal XAUUSD H1 config."""

import sys
sys.path.insert(0, ".")

import pandas as pd
from broky.data.loader import load_timeframe
from broky.backtest.engine import BacktestEngine


def run_backtest(label, engine, df_h1, d1_df=None):
    result = engine.run(df_h1, warmup=200, d1_df=d1_df)
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    print(f"  Trades:          {result.total_trades}")
    print(f"  Win Rate:        {result.win_rate:.1%}")
    print(f"  Profit Factor:   {result.profit_factor:.2f}")
    print(f"  PnL:            ${result.total_pnl:.2f} ({result.total_pnl_pct:.1f}%)")
    print(f"  Max Drawdown:    {result.max_drawdown_pct:.1f}%")
    print(f"  Sharpe Ratio:    {result.sharpe_ratio:.2f}")
    print(f"  Max Cons Wins:   {result.max_consecutive_wins}")
    print(f"  Max Cons Losses:  {result.max_consecutive_losses}")
    return result


def main():
    data_dir = "data/xau-data"
    df_h1 = load_timeframe(data_dir, "H1")
    cutoff = pd.Timestamp("2023-01-01")
    df_h1 = df_h1[df_h1.index >= cutoff]
    print(f"Loaded H1: {len(df_h1)} candles, {df_h1.index[0]} → {df_h1.index[-1]}")

    df_d1 = load_timeframe(data_dir, "D1")
    df_d1 = df_d1[df_d1.index >= cutoff - pd.Timedelta(days=400)]
    print(f"Loaded D1: {len(df_d1)} candles, {df_d1.index[0]} → {df_d1.index[-1]}")

    # Sweet spot sweep: focused on CONF=0.65 + D1 filter
    configs = []
    for conf in [0.60, 0.65, 0.70]:
        for rr in [2.0, 2.5, 3.0, 3.5]:
            for atr in [1.5, 2.0, 2.5]:
                for risk in [0.005, 0.01]:
                    for mh in [48, 72, 96]:
                        label = f"C{conf:.0f} RR{rr} ATR{atr} R{risk*100:.0f}% MH{mh}"
                        configs.append((label, BacktestEngine(
                            initial_equity=1000,
                            risk_per_trade=risk,
                            atr_multiplier=atr,
                            risk_reward_ratio=rr,
                            min_confidence=conf,
                            max_holding_bars=mh,
                            cooldown_bars=12,
                        ), df_d1))

    # Add a few cooldown variants on the best-known area
    configs += [
        ("C65 RR2.5 ATR2.0 R0.5% MH96 CD18", BacktestEngine(
            initial_equity=1000, risk_per_trade=0.005, atr_multiplier=2.0,
            risk_reward_ratio=2.5, min_confidence=0.65, max_holding_bars=96,
            cooldown_bars=18,
        ), df_d1),
        ("C65 RR2.5 ATR2.0 R0.5% MH96 CD24", BacktestEngine(
            initial_equity=1000, risk_per_trade=0.005, atr_multiplier=2.0,
            risk_reward_ratio=2.5, min_confidence=0.65, max_holding_bars=96,
            cooldown_bars=24,
        ), df_d1),
    ]

    results = {}
    for label, engine, d1 in configs:
        results[label] = run_backtest(label, engine, df_h1, d1_df=d1)

    # Summary table sorted by MaxDD then PF
    print(f"\n{'='*80}")
    print("  SWEEP SUMMARY (sorted by MaxDD asc)")
    print(f"{'='*80}")
    print(f"  {'Config':<40} {'Trades':>6} {'WR':>6} {'PF':>6} {'PnL%':>7} {'MaxDD':>6}")
    print(f"  {'-'*40} {'-'*6} {'-'*6} {'-'*6} {'-'*7} {'-'*6}")

    sorted_results = sorted(results.items(), key=lambda x: x[1].max_drawdown_pct)
    for label, r in sorted_results:
        print(f"  {label:<40} {r.total_trades:>6} {r.win_rate:>5.0%} {r.profit_factor:>6.2f} {r.total_pnl_pct:>+6.1f}% {r.max_drawdown_pct:>5.1f}%")

    # Also show top 5 by PnL
    print(f"\n{'='*80}")
    print("  TOP 5 BY PnL%")
    print(f"{'='*80}")
    print(f"  {'Config':<40} {'Trades':>6} {'WR':>6} {'PF':>6} {'PnL%':>7} {'MaxDD':>6}")
    print(f"  {'-'*40} {'-'*6} {'-'*6} {'-'*6} {'-'*7} {'-'*6}")
    top_pnl = sorted(results.items(), key=lambda x: x[1].total_pnl_pct, reverse=True)[:5]
    for label, r in top_pnl:
        print(f"  {label:<40} {r.total_trades:>6} {r.win_rate:>5.0%} {r.profit_factor:>6.2f} {r.total_pnl_pct:>+6.1f}% {r.max_drawdown_pct:>5.1f}%")

    # Best compromise: MaxDD < 25% AND PnL > 100% AND PF > 1.3
    print(f"\n{'='*80}")
    print("  SWEET SPOT CANDIDATES (MaxDD<25%, PnL>100%, PF>1.3)")
    print(f"{'='*80}")
    print(f"  {'Config':<40} {'Trades':>6} {'WR':>6} {'PF':>6} {'PnL%':>7} {'MaxDD':>6}")
    print(f"  {'-'*40} {'-'*6} {'-'*6} {'-'*6} {'-'*7} {'-'*6}")
    sweet_spots = [(l, r) for l, r in results.items()
                   if r.max_drawdown_pct < 25 and r.total_pnl_pct > 100 and r.profit_factor > 1.3]
    sweet_spots = sorted(sweet_spots, key=lambda x: x[1].max_drawdown_pct)
    for label, r in sweet_spots:
        print(f"  {label:<40} {r.total_trades:>6} {r.win_rate:>5.0%} {r.profit_factor:>6.2f} {r.total_pnl_pct:>+6.1f}% {r.max_drawdown_pct:>5.1f}%")


if __name__ == "__main__":
    main()
