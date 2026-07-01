# Block Verification + TradeBlocker (Gap Filler)

> Date: 2026-07-01
> Source: scripts/verify_blocks.py, broky/risk/trade_blocker.py
> Related: [[2026-06-22_drawdown-db-sync]], [[2026-06-26_dynamic-max-positions-from-equity]], [[2026-06-22_ghost-trade-prevention]]
> Status: verified locally — NOT deployed (waiting user approval)

## คำถาม

"สร้าง blocking signal ก่อน เพื่อป้องกัน การผิดพลาด หรือ % Risk , max order ที่กำหนด ให้ block ได้จริง"

= verify block ที่มีอยู่ว่า block ได้จริง + เพิ่ม block ที่ขาด

## Block ที่มีอยู่ (verify ผ่าน)

| Block | ที่อยู่ | สถานะ |
|-------|-------|-------|
| DrawdownProtector | broky/risk/drawdown_protection.py | ✅ block ได้จริง (account 30%, daily 20%) |
| CircuitBreaker | broky/risk/circuit_breaker.py | ✅ block ได้จริง (5 consec, daily 5%, flash 10%, cooldown) |
| _calculate_max_positions | metty/execution/live_trader.py | ✅ block ได้จริง (max 1-5 จาก equity) |
| risk_per_trade_size | broky/risk/sizing.py | ✅ clamp ที่ max_lots=10, min_lots=0.01 |
| BUY confidence filter | live_trader.py line 1253 | (verify ใน integration test — ไม่ได้ทำใน script นี้) |

## Block ใหม่ที่เพิ่ม (gap filler)

ไฟล์ใหม่: `broky/risk/trade_blocker.py` — `TradeBlocker` class + `BlockInput` dataclass

Pure module — no I/O, no MT5, no DB. caller pass state, TradeBlocker decide → easy unit-test.

7 block ใหม่ที่เติมในช่องว่าง:

| Block | ทำอะไร | ทำไมต้องมี |
|-------|--------|-----------|
| `position_limit` | ปฏิเสธถ้า open_positions >= max_positions | redundant กับ live_trader 4b แต่รวมเป็น single source |
| `risk_pct_sanity` | ปฏิเสธถ้า risk_per_trade > 5% | กัน config bug ที่ทำให้ risk 50% |
| `sl_too_tight` | ปฏิเสธถ้า SL < 0.05% | กัน lots ระเบิดจาก SL ที่แคบเกินไป |
| `sl_too_wide` | ปฏิเสธถ้า SL > 5% | กัน config bug ที่ SL หลวมเกินไป |
| `hard_max_lots` | ปฏิเสธถ้า lots > hard cap (default 0.50) | กัน position size ใหญ่เกินไป |
| `margin_safety` | ปฏิเสธถ้า margin_required > 80% free_margin | กัน margin call |
| `daily_trade_count` | ปฏิเสธถ้า ≥ 20 trades/day | ISSUE-028: กัน rapid cycling ใน daily loss % |
| `weekly_trade_count` | ปฏิเสธถ้า ≥ 80 trades/week | กัน over-trading |

`learning_mode=True` บายพาส daily/weekly count (data collection ต้อง cycle)

## ผล verify (43/43 PASS)

```
=== 1. risk_per_trade_size ===
  sizing_normal: $200 × 2% ÷ ($12 × 100) = 0.0033 → floored 0.01 ✅
  sizing_bigger_equity: $2000 × 2% ÷ $1200 = 0.033 → floored 0.03 ✅
  sizing_zero_equity → 0.01 (min) ✅
  sizing_zero_sl_distance → 0.01 (avoid div by zero) ✅
  sizing_insane_risk_capped → capped at max_lots=10 ✅
  sizing_tiny_sl_capped → capped (but TradeBlocker.sl_too_tight จะจับก่อน) ✅

=== 2. _calculate_max_positions ===
  equity $0/$50/$199/$200 → 1, $400 → 2, $1000/$10000 → 5 (cap) ✅
  block: open=2/max=2 → block ✅; open=1/max=2 → pass ✅

=== 3. DrawdownProtector ===
  equity $69 <= $70 (30% from $100) → block ✅; $71 → pass ✅
  daily -21% > 20% → block ✅; -19% → pass ✅

=== 4. CircuitBreaker ===
  4 consec (2% daily) → pass ✅; 5 consec → block ✅
  daily -6% > 5% → block ✅
  flash crash 11% → block ✅
  cooldown expire 20min > 15min → unblock ✅

=== 5-13. TradeBlocker ===
  position 3/3 → block ✅; 2/3 → pass ✅
  risk 6% → block ✅; 2% → pass ✅
  SL 0.03% → block ✅; SL 6% → block ✅
  lots 0.60 > 0.50 → block ✅; 0.40 → pass ✅
  margin $90 > 80% of $100 → block ✅; $70 → pass ✅
  daily 20/20 → block ✅; 10 → pass ✅; learning_mode bypass ✅
  weekly 80/80 → block ✅
  order check: position_limit fires before risk_pct_sanity ✅
  all-pass case → pass ✅
```

## ข้อค้นพบระหว่าง verify

1. **risk_per_trade_size ไม่ใช่ block เป็น sizing function** — มันแค่คำนวณ lots และ clamp ที่ max_lots=10. ถ้า risk_pct=50% มันจะปล่อย lots ที่ใหญ่เกินไป (ถ้า SL ไม่แคบเกินไป). TradeBlocker.risk_pct_sanity เป็น block จริง

2. **CircuitBreaker daily loss limit fires before consecutive limit** — 4 loss × -$2 = -8% > 5% daily limit → block ที่ daily loss ก่อน 5 consec. ต้องคำนวณ pnl ให้เล็กพอตอน test consecutive limit

3. **DrawdownProtector เก็บ daily_trades/weekly_trades แต่ไม่ได้ enforce** — มี field ใน DrawdownState แต่ไม่มี check. TradeBlocker เติมช่องว่างนี้ (ISSUE-028)

4. **Order of checks สำคัญ** — position_limit ต้องเช็คก่อน risk_pct_sanity เพราะอยาก block เร็วที่สุด (early exit). TradeBlocker จัดลำดับจาก account survival → per-trade sanity → anti-churn

## ข้อจำกัด / ยังไม่ได้ทำ

- **ไม่ได้ deploy ไป live_trader** — TradeBlocker เป็น module ใหม่ที่ยังไม่ได้ wire เข้า decision flow. ต้อง user อนุมัติก่อนเพราะกระทบ Real-A
- **ไม่ได้ทำ integration test กับ live_trader จริง** — verify เป็น unit test เท่านั้น
- **ไม่ได้ทดสอบ BUY confidence filter + ML filter block** — ต้องการ mock signal + ML predictor
- **margin_safety ใช้ค่าจาก caller** — ต้องเชื่อมจาก MT5 bridge จริง

## ขั้นต่อไป (รอ user ตัดสินใจ)

1. wire TradeBlocker เข้า live_trader หลัง position_limit check (line 1299) — เพิ่ม block ก่อน circuit_breaker
2. ทดสอบ integration กับ B/C/D demo accounts ก่อน
3. ถ้าผ่าน → deploy Real-A ด้วย explicit approval

## Files

- `broky/risk/trade_blocker.py` — pure block module (NEW)
- `scripts/verify_blocks.py` — verification script 43/43 PASS
- ISSUE-028 marked fixed with verification artifact reference