---
name: partial-tp-bug-fix-and-analysis
description: Partial TP (Option C) analysis + tp1_ratio bug fix across 3 traders
metadata:
  type: learning
---

# Partial TP (Option C) — Bug Fix + Full Analysis

## Bug Fixed: tp1_ratio hardcoded to 0.5

**ปัญหา**: `TP1_RATIO` env var ถูกโหลดเข้า `self.risk.tp1_ratio` แต่ code ใช้ hardcoded `0.5` แทน
**แก้**: เปลี่ยน `0.5` → `self.risk.tp1_ratio` ใน 3 ไฟล์:
- `metty/execution/live_trader.py` L1262-1267
- `metty/execution/scalp_trader.py` L1031-1036
- `metty/execution/m5_scalp_trader.py` L845-850

## Partial TP Mechanism (Option C)

```
1. Position 1 opens: SL/TP normal + tp1_price = entry ± tp_distance * tp1_ratio
2. Price hits TP1 → close position 1 fully → lock TP1 profit
3. Open scale-in position 2:
   - Entry ≈ TP1 price
   - SL = TP1 ± (remaining_distance / rr_scale_in)  ← TIGHT!
   - TP = original final TP
4. Position 2 monitored normally (SL/TP/max_holding)
```

## Backtest Results (Post-processing estimation, NOT true sim)

| Scenario | % Trades | Impact |
|----------|---------|--------|
| TP1 → TP final | 37.7% | +$15.76/trade |
| TP1 → NOT TP final | 34.6% | +$2,263 total |
| Never reaches TP1 | 27.7% | No change |
| SL trades (MC est. ~46% reached TP1) | ~46% | +$12,419 total |

**Total**: PnL from $4,347 → ~$19,047 (+$14,698)

## Caveats

1. **Scale-in SL แคบมาก**: remaining 50% / 2.5 = 0.2× tp_distance → scale-in ถูก stop เร็ว
2. **BacktestEngine ไม่รองรับ**: ใช้ post-processing MFE/Monte Carlo → ผลอาจเกินจริง
3. **Circuit breaker นับแยก**: position 1 win + position 2 loss = 1W+1L → 3 scale-in losses ติดกัน = breaker
4. **Execution slippage ไม่โมเดล**: scale-in entry ≈ tp1_price แต่จริงมี spread/gap
5. **ยังไม่เคยเปิดใช้จริง**: DB ไม่มี tp_level=2 เลย → ไม่มี real-world data ยืนยัน

## Parameters

| Parameter | Default | Per-account Env Var |
|-----------|---------|---------------------|
| partial_tp_enabled | False | PARTIAL_TP_ENABLED_{A,B,C} |
| tp1_ratio | 0.5 | TP1_RATIO_{A,B,C} |
| rr_scale_in | 2.5 | RR_SCALE_IN_{A,B,C} |

## DB Schema (already exists)

- `live_trades.tp1_price` — TP1 price level
- `live_trades.tp_level` — 1=original, 2=scale-in
- `live_trades.parent_trade_id` — links scale-in to original
- `live_trades.remaining_lots` — set to 0 on close

## Recommendation

เปิดบน 1 account ก่อน (เช่น Account C) เพื่อ A/B test จริง → เก็บ real data → เปรียบเทียบผลจริง vs estimation

See also: [[partial-tp-backtest-option-c]]