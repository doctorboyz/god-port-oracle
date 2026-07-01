# Trailing Stop — Bar-by-Bar Replay vs MFE/MAE Approximation

> Date: 2026-07-01
> Source: scripts/trailing_replay.py on Real-A 1131 trades (May-June 2026)
> Related: [[2026-06-26_dynamic-max-positions-from-equity]]

## Hypothesis
Previous backtest (`scripts/backtest_tp_variants.py`) used MFE/MAE single-value approximation for trailing stop. Hypothesis: this overestimates trailing benefit because it assumes "exit at MFE - trail_distance" without modeling WHEN the peak happened or whether trailing triggered before TP.

## Method
Built `scripts/trailing_replay.py` — bar-by-bar replay using M5 bars fetched from MT5 bridge (20k bars, Mar-Jul 2026). For each trade, walk M5 bars from entry to exit, check SL/TP/trailing in chronological order. Conservative intra-bar: adverse-first (SL before TP before trailing update).

Compared 3 baselines:
- **Actual**: original DB pnl (includes user manual close)
- **Let-run**: pure SL/TP/max_holding replay (no trailing, no manual close) — TRUE baseline
- **Trailing variants**: D-simple, ATR-based, Breakeven, Multi-stage, Regime-aware

## Findings (Real-A, 1131 trades, May-June 2026)

| Variant | PnL | MaxDD | PnL/MaxDD | WR |
|---|---|---|---|---|
| Actual (user manual close) | +$2,317 | $1,306 | 1.77 | 42.3% |
| Let-run (no trail, no manual) | +$425 | $1,783 | 0.24 | 39.7% |
| D-simple 0.30/0.15 | +$686 | $926 | 0.74 | 48.5% |
| **D-simple 0.20/0.10** | **+$976** | **$466** | **2.10** | **57.5%** |
| Multi-stage | +$1,066 | $736 | 1.45 | 57.2% |
| Regime-aware | +$1,120 | $1,028 | 1.09 | 51.3% |

## Critical Insights

1. **MFE/MAE approximation OVERESTIMATES trailing benefit by ~$440/trade set.**
   - MFE/MAE method claimed: D 0.30/0.15 = +$1,129 PnL (vs baseline +$494)
   - Bar-by-bar truth: D 0.30/0.15 = +$686 PnL (vs let-run +$425)
   - The approximation didn't model "did trailing trigger before TP" — it assumed every trade with MFE >= activation exits at peak-trail, ignoring trades where TP would have hit first (bigger win)

2. **User's manual close is valuable**: Actual (+$2,317) >> Let-run (+$425). User intervention added ~$1,891 of value. The user is good at manual closing.

3. **Trailing CAN'T replace user skill**: Best trailing (Multi-stage +$1,066) < Actual (+$2,317). Trailing loses ~$1,250 vs actual.

4. **But trailing beats doing nothing**: All trailing variants beat Let-run (+$425) on PnL, and ALL dramatically reduce MaxDD ($1,783 → $466-1,028).

5. **D-simple 0.20/0.10 is best risk-adjusted**: PnL/MaxDD = 2.10, MaxDD only $466 (74% reduction vs let-run).

## Why Trailing Helps Less Than Expected

The original concern: "ออกออเดอร์ถูกแต่รอ TP ไกลมาก จนเกิดกลับตัวก่อน กลายเป็น stoploss" — entries correct but TP too far, price reverses to SL. Trailing should save these.

Bar-by-bar shows:
- Trailing converts TP winners (192 → 86) into smaller trailing winners (393 trailing_tp). Net: more wins but smaller wins.
- Trailing converts some max_holding winners (+$3,825 in baseline) into smaller trailing wins.
- SL count slightly INCREASED (460 → 489) because some trades that armed trailing then reversed through trail_level below entry → trailing loss (still classified as trailing_tp, not SL).
- Net: total PnL DROPS vs actual, but MaxDD drops more, so risk-adjusted improves.

## Recommendation

For Real-A: deploy D-simple 0.20/0.10 (best risk-adjusted) OR Multi-stage (best PnL among trailing). DO NOT deploy D 0.30/0.15 (the original recommendation from MFE/MAE backtest) — it's the worst trailing variant on PnL/MaxDD ratio.

Combine with manual close: trailing as safety net when user is away, manual close when user is active. But this requires the agent to NOT auto-close when user is monitoring.

## Method Limitations

- Conservative intra-bar (adverse-first) underestimates trailing — true benefit may be slightly higher
- Bars cover only Mar-Jul 2026 (Exness period). Premium data backtest (2023-2026) needed to confirm across market regimes
- Regime classification uses trade's recorded regime field, which may differ from bar-by-bar regime
- 33 trades with inferred exit_reason excluded from baseline; they have pnl=0 in DB

## Premium Data Validation (2023-06 to 2026-04, 1996 synthetic entries)

Ran same trailing variants on premium M5 data (200k bars, 2023-2026) with synthetic EMA50 trend-following entries (every 100 M5 bars). Same entries for all variants → pure trailing comparison.

| Variant | PnL | MaxDD | WR | Note |
|---|---|---|---|---|
| Let-run (no trail) | -$385 | $594 | 33.1% | baseline |
| D-simple 0.30/0.15 | -$526 | $571 | 32.7% | **WORSE than let-run** — confirms bad recommendation |
| D-simple 0.20/0.10 | -$79 | $271 | 37.8% | nearly break-even, good risk-adjusted |
| **ATR trail k=0.5 act=0.20** | **-$7.75** | **$241** | 37.8% | **BEST PnL + lowest MaxDD** |
| Multi-stage | -$272 | $398 | 37.6% | |
| Regime-aware | -$194 | $346 | 36.7% | |

By year (D-simple 0.20/0.10):
- 2023: -$57.86 (379 trades, ranging year)
- 2024: +$91.37 (712 trades, trending year — only profitable year)
- 2025: -$42.91 (708 trades)
- 2026: -$69.99 (197 trades, Jan-Apr only)

## Cross-Dataset Conclusion

**The previous MFE/MAE-based recommendation (D 0.30/0.15) was WRONG.** Bar-by-bar replay on both Exness and premium data shows:

1. **D 0.30/0.15 is the WORST trailing variant** — worse than let-run on premium data, worst PnL/MaxDD on Exness.
2. **ATR-based trailing (k=0.5, act=0.20) is the most robust** across both datasets:
   - Exness: PnL +$893, MaxDD $508, PnL/MaxDD 1.76 (2nd best risk-adjusted)
   - Premium: PnL -$7.75 (nearly break-even vs let-run -$385), MaxDD $241 (lowest)
3. **D-simple 0.20/0.10 is the best fixed-% trailing**:
   - Exness: PnL +$976, MaxDD $466, PnL/MaxDD 2.10 (best risk-adjusted)
   - Premium: PnL -$79, MaxDD $271

## Final Recommendation

**Deploy ATR-based trailing (k=0.5 ATR, arm at 0.20%) OR D-simple 0.20/0.10.** Both are robust across datasets.

Do NOT deploy D 0.30/0.15 — it was a recommendation error from the MFE/MAE approximation that overestimated benefits by ~$440/trade set.

The user's original concern ("TP too far, price reverses to SL") is best addressed by ATR-trail k=0.5 (volatility-adaptive) — it cuts losers faster in volatile regimes and lets trending trades run wider.

## Method Limitations

- Conservative intra-bar (adverse-first) underestimates trailing — true benefit may be slightly higher
- Premium entries are synthetic (EMA50 trend-following), not the actual swing strategy — engine too slow (O(n²)) on 200k bars. But same entries for all variants → trailing comparison still valid
- ATR-trail on Exness uses trade's recorded atr_at_entry; on premium uses bar's ATR(14)
- 33 Exness trades with inferred exit_reason excluded from baseline

## Files
- `scripts/trailing_replay.py` — bar-by-bar replay framework (Exness)
- `scripts/trailing_replay_premium.py` — premium data backtest (synthetic entries)
- `/tmp/exness_m5_bars.csv` — 20k M5 bars from MT5
- `/tmp/oracle_engine.db` — Real-A trades copy from VPS
- `/tmp/premium_replay.log` — premium backtest full output