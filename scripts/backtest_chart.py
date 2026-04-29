"""Generate equity curve chart for backtest report."""

from broky.data.loader import load_csv
from broky.data.resampler import resample_timeframe
from broky.backtest.engine import BacktestEngine
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.ticker as ticker
import numpy as np
import pandas as pd

filepath = Path('/Users/doctorboyz/Documents/xau-data/XAUUSD_M5-2026-04-15-06_40-Premium Data.csv')
df = load_csv(filepath)
h1 = resample_timeframe(df, 'H1')

engine = BacktestEngine(
    initial_equity=1000.0,
    risk_per_trade=0.02,
    atr_multiplier=1.0,
    risk_reward_ratio=2.5,
    spread_buffer=2.5,
    min_confidence=0.45,
    contract_size=100.0,
)

result = engine.run(h1, warmup=50)

# Build equity curve with dates
warmup = 50
# equity_curve has len(h1) - warmup + 1 entries (starts from initial equity)
eq_len = len(result.equity_curve)
dates = h1.index[warmup-1:warmup-1+eq_len]
if len(dates) > eq_len:
    dates = dates[:eq_len]
elif len(dates) < eq_len:
    dates = h1.index[warmup-1:warmup-1+eq_len]
equity = pd.Series(result.equity_curve[:len(dates)], index=dates)

# Calculate metrics
starting = 1000.0
final = equity.iloc[-1]
pnl = final - starting
pnl_pct = (final - starting) / starting * 100

# Duration
start_date = dates[0]
end_date = dates[-1]
duration_days = (end_date - start_date).days
duration_years = duration_days / 365.25
annual_return = ((final / starting) ** (365.25 / duration_days) - 1) * 100 if duration_days > 0 else 0

# Drawdown series
running_max = equity.cummax()
drawdown = (equity - running_max) / running_max * 100

# Trade markers
trade_buys = []
trade_sells = []
for t in result.trades:
    entry_date = h1.index[t.entry_idx]
    if t.pnl > 0:
        trade_buys.append((entry_date, equity.loc[:entry_date].iloc[-1] if entry_date in equity.index else None))
    else:
        trade_sells.append((entry_date, equity.loc[:entry_date].iloc[-1] if entry_date in equity.index else None))

# === CHART ===
fig, axes = plt.subplots(3, 1, figsize=(16, 14), gridspec_kw={'height_ratios': [3, 1, 1]})
fig.suptitle('XAUUSD H1 Backtest Report — $1,000 Starting Capital', fontsize=16, fontweight='bold', y=0.98)

# 1. Equity Curve
ax1 = axes[0]
ax1.fill_between(equity.index, starting, equity, alpha=0.15, color='#2196F3')
ax1.plot(equity.index, equity, color='#2196F3', linewidth=1.5, label='Equity')
ax1.axhline(y=starting, color='gray', linestyle='--', alpha=0.5, linewidth=0.8)
ax1.set_ylabel('Balance (USD)', fontsize=12)
ax1.set_title(f'Equity Curve: ${starting:,.0f} → ${final:,.2f} ({pnl_pct:+.1f}%)', fontsize=13)
ax1.yaxis.set_major_formatter(ticker.FuncFormatter(lambda x, p: f'${x:,.0f}'))
ax1.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
ax1.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
ax1.grid(True, alpha=0.3)
ax1.legend(loc='upper left', fontsize=10)

# Add trade markers
for t in result.trades:
    if t.exit_idx is not None:
        exit_date = h1.index[t.exit_idx]
        if exit_date in equity.index:
            color = '#4CAF50' if t.pnl > 0 else '#F44336'
            marker = '^' if t.pnl > 0 else 'v'
            ax1.scatter(exit_date, equity.loc[exit_date], color=color, marker=marker, s=60, zorder=5, alpha=0.8)

# Stats box
stats_text = (
    f'Period: {start_date.strftime("%Y-%m-%d")} → {end_date.strftime("%Y-%m-%d")} ({duration_days} days)\n'
    f'Trades: {result.total_trades} | Wins: {result.winning_trades} | Losses: {result.losing_trades}\n'
    f'Win Rate: {result.win_rate:.1%} | Profit Factor: {result.profit_factor:.2f}\n'
    f'Max DD: {result.max_drawdown_pct:.1f}% | Sharpe: {result.sharpe_ratio:.2f}\n'
    f'Annual Return: {annual_return:.1f}%'
)
ax1.text(0.02, 0.97, stats_text, transform=ax1.transAxes, fontsize=9,
         verticalalignment='top', fontfamily='monospace',
         bbox=dict(boxstyle='round', facecolor='wheat', alpha=0.8))

# 2. Drawdown
ax2 = axes[1]
ax2.fill_between(drawdown.index, drawdown, 0, alpha=0.4, color='#F44336')
ax2.plot(drawdown.index, drawdown, color='#F44336', linewidth=0.8)
ax2.set_ylabel('Drawdown %', fontsize=10)
ax2.set_title(f'Max Drawdown: {result.max_drawdown_pct:.1f}%', fontsize=11)
ax2.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
ax2.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
ax2.grid(True, alpha=0.3)

# 3. Price with trade markers
ax3 = axes[2]
price_slice = h1['Close'].iloc[warmup:warmup+len(result.equity_curve)]
ax3.plot(price_slice.index, price_slice.values, color='#607D8B', linewidth=0.8, alpha=0.7)

for t in result.trades:
    entry_date = h1.index[t.entry_idx]
    if t.pnl > 0:
        ax3.scatter(entry_date, t.entry_price, color='#4CAF50', marker='^', s=40, zorder=5)
    else:
        ax3.scatter(entry_date, t.entry_price, color='#F44336', marker='v', s=40, zorder=5)

ax3.set_ylabel('XAUUSD Price', fontsize=10)
ax3.set_xlabel('Date', fontsize=10)
ax3.set_title('XAUUSD Price with Trade Entries (Green=Win, Red=Loss)', fontsize=11)
ax3.yaxis.set_major_formatter(ticker.FuncFormatter(lambda x, p: f'${x:,.0f}'))
ax3.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
ax3.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
ax3.grid(True, alpha=0.3)

plt.tight_layout()
plt.savefig('/Users/doctorboyz/MT5/data/results/backtest_equity_curve.png', dpi=150, bbox_inches='tight')
print(f'Chart saved to /Users/doctorboyz/MT5/data/results/backtest_equity_curve.png')

# Print trade table
print()
print('=' * 80)
print('TRADE LOG')
print('=' * 80)
print(f'{"#":>3} {"Dir":>4} {"Entry":>8} {"Exit":>8} {"Reason":>12} {"PnL":>9} {"Equity":>9} {"Entry Date":>16} {"Exit Date":>16}')
print('-' * 80)

running_equity = starting
for i, t in enumerate(result.trades, 1):
    running_equity += t.pnl
    d = t.direction.value
    entry_date = h1.index[t.entry_idx].strftime('%Y-%m-%d %H:%M')
    exit_date = h1.index[t.exit_idx].strftime('%Y-%m-%d %H:%M') if t.exit_idx else 'OPEN'
    pnl_str = f'+${t.pnl:.2f}' if t.pnl > 0 else f'-${abs(t.pnl):.2f}'
    print(f'{i:>3} {d:>4} {t.entry_price:>8.2f} {t.exit_price:>8.2f} {t.exit_reason:>12} {pnl_str:>9} ${running_equity:>8.2f} {entry_date:>16} {exit_date:>16}')

print('-' * 80)
print(f'Final Equity: ${final:.2f} | Total PnL: ${pnl:.2f} ({pnl_pct:.1f}%) | Duration: {duration_days} days ({duration_years:.1f} years)')
print(f'Annual Return: {annual_return:.1f}% | CAGR: {(((final/starting)**(365.25/duration_days))-1)*100:.1f}%')