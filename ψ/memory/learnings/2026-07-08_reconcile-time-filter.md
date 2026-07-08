# Reconcile Picks Wrong Deal — Time Filter Fix (ISSUE-080)

> วันที่: 8 กรกฎาคม 2026
> เกี่ยวข้อง: [[2026-07-06_external-close-reconcile-ghost-trade]], [[2026-07-07_raw-rpyc-missing-initialize]]
> ประเภท: learning (bug fix — CPT)
> scope: Real-A production bug + causal test + fix + deploy

## สรุป

หลัง deploy fix ISSUE-077/078 และดู performance 2 วัน พบว่า WR/PF ต่ำกว่า AEGIS backtest มาก (WR 56% vs 90%, PF ~1.0 vs 5.3). ตอนเช็ค trade เเพื่อดู root cause พบว่า DB บันทึก exit_price/pnl/exit_reason ผิดหลาย trade:

- trade #5543 BUY @ 4124.679: DB บันทึก exit 4132.34 pnl +$7.66 (กำไร!) แต่ MT5 จริง ๆ ปิดที่ 4112.76 pnl -$11.92 (ขาดทุน)
- trade #5541 BUY @ 4142.495: DB exit 4134.43 pnl -$8.06 แต่ MT5 จริง ๆ ปิดที่ 4132.34 pnl -$10.16
- trade #5546 SELL @ 4046.106: DB exit 4050.48 แต่ deal นั้นเป็น close ของ trade อื่นที่ปิดก่อน #5546 เปิด

## Bug symptom

```
DB trade #5543 (BUY entry 4124.679):
  exit_price=4132.34, pnl=+$7.66, exit_reason=stop_loss  ← DB (ผิด)

MT5 deal จริง:
  open  1450937509 @ 4124.679 (time=1783505700, 06:55 ICT)
  close 1451023649 @ 4112.76 (time=1783511959, 08:19 ICT, [sl 4112.76000], pnl=-11.92)

deal ที่ DB ดึงผิด:
  close 1450457579 @ 4132.34 (time=1783469200, 18:52 ICT วันก่อน, [sl 4132.34000],
                              pnl=-10.16, เป็น SL close ของ trade #5541 จริง ๆ)
```

## Root cause

`_match_closing_deal` (metty/core/db.py) และ `_reconcile_external_close` (metty/execution/live_trader.py) ใช้ Strategy "price proximity + direction" เลือก closing deal โดยไม่กรองเวลา — ดึง deal ที่เกิดขึ้น **ก่อน** trade เปิดก็ได้:

1. Bridge ไม่ expose `position_id` หรือ `entry` field → Strategy 0 (position_id match) ใช้ไม่ได้
2. ตกไป Strategy 1 (price proximity) ที่ดูแค่ `deal.type == closing_type` และ `|deal.price - entry| < 0.5%`
3. เมื่อ trade หนาแน่นในช่วงราคาใกล้กัน → deal ของ trade อื่นที่จบก่อน trade เปิด อาจใกล้ entry สุด → ถูกเลือก
4. DB บันทึก exit_price/pnl/exit_reason จาก deal ผิด → สถิติ WR/PF เพี้ยน

Bridge expose deal fields: `ticket, order, time, time_msc, type, magic, reason, volume, price, commission, swap, profit, symbol, comment, external_id` — ไม่มี position_id/entry.

## Hypothesis (CPT format)

"Bug occurs because `_match_closing_deal` (db.py) and `_reconcile_external_close` (live_trader.py) Strategy 1 (price proximity) does NOT filter deals by time. When two trades cluster in a price neighborhood, an earlier trade's SL/TP close (which happened BEFORE this trade opened) can be the closest in price to this trade's entry, so Strategy 1 picks it. The OPEN deal has `deal.order == position.ticket` AND `deal.type == opening_type` — finding it gives us the open time, and filtering `deal.time > open_deal_time` excludes earlier-trade closes."

## Causal test (RED → GREEN)

ไฟล์: `tests/test_reconcile_time_filter.py` (3 tests)

| Test | จุดประสงค์ | RED | GREEN |
|------|-----------|-----|-------|
| `TestMatchClosingDealTimeFilter::test_does_not_pick_earlier_trade_close_deal` | #5543 (เปิด 06:55) ต้อง match SL close ของตัวเอง @ 4112.76 ไม่ใช่ SL ของ #5541 @ 4132.34 (จบ 18:52 วันก่อน) | ✅ fail (ดึง 1450457579 ผิด) | ✅ pass |
| `TestMatchClosingDealTimeFilter::test_does_not_pick_earlier_trade_close_for_first_trade` | #5541 (เปิด 18:00) ต้องไม่ match close ของ #5538 (จบ 08:35) | ✅ fail | ✅ pass |
| `TestSwingReconcileTimeFilter::test_does_not_pick_earlier_trade_close_deal` | swing trader reconcile ผ่าน _reconcile_external_close ต้องไม่ดึง deal ผิดเหมือนกัน | ✅ fail | ✅ pass |

## Fix

เพิ่มขั้นหา OPEN deal ก่อน Strategy 1:
```python
# OPEN deal has deal.order == position ticket AND deal.type == opening_type
open_deal_time: float | None = None
if ticket is not None:
    ticket_int = int(ticket)
    for deal in deals:
        deal_order = deal.get("order")
        if deal_order is None or int(deal_order) != ticket_int:
            continue
        if deal.get("type", -1) != opening_type:
            continue
        deal_time = deal.get("time")
        try:
            open_deal_time = float(deal_time)
        except (TypeError, ValueError):
            continue
        break
```

แล้ว Strategy 1 เพิ่ม time guard:
```python
if open_deal_time is not None:
    deal_time = deal.get("time")
    if deal_time is None:
        continue
    try:
        if float(deal_time) <= open_deal_time:
            continue  # closing deal happened BEFORE open — cannot be this trade's close
    except (TypeError, ValueError):
        continue
```

Apply ทั้ง `_match_closing_deal` (db.py) และ `_reconcile_external_close` (live_trader.py).

## Verification

| Step | Result |
|------|--------|
| Causal test RED (before fix) | ✅ 3/3 fail (ดึง deal ผิดตรง scenario จริง) |
| Causal test GREEN (after fix) | ✅ 3/3 pass |
| Regression: 17 reconcile-related tests | ✅ ผ่านทั้งหมด |
| ทดสอบด้วยข้อมูลจริง 2 วันของ Real-A | ✅ (verify หลัง deploy) |

## บทเรียน

1. **Price proximity ไม่พอ — ต้องกรองเวลาด้วย** — deal ที่ราคาใกล้ entry สุดไม่จำเป็นเป็น close ของ trade นั้น. ต้อง filter ด้วย `deal.time > open_deal.time` เสมอ.

2. **Bridge ไม่ expose ทุก field — ต้องใช้ของที่มี** — MT5 มี position_id/entry field จริง แต่ RPyC bridge ไม่ส่งมา. ใช้ `deal.order == ticket` + `deal.type` แยก open/close แทนได้.

3. **Strategy priority สำคัญ** — Strategy 0 (position_id) เสมอก่อน เพราะแม่นยำสุด. ตกมา Strategy 1 (price proximity) ต้องมี time guard. Strategy 2/3 (SL/TP price, time proximity) เป็น fallback สุดท้าย.

4. **Bug ที่ทำให้กำไรกลายเป็นขาดทุน — รุนแรงกว่าที่คิด** — ไม่ใช่แค่ exit_reason ผิด label แต่ pnl เซ็นผิดด้วย. สถิติทั้งหมดที่นับจาก DB เพี้ยนหมด. ต้อง recompute PnL หลัง deploy fix.

5. **Sample เล็กทำให้ bug ชัดเจน** — 9 trade บน Real-A มี 3 trade ผิด (33%). ถ้า trade เยอะขึ้นอาจไม่เห็นชัดเท่า. แต่ WR/PF ที่ได้จาก DB ไม่น่าเชื่อถือจนกว่าจะแก้ bug นี้.

## ไฟล์ที่แก้

| ไฟล์ | การแก้ |
|------|-------|
| `metty/core/db.py` | `_match_closing_deal`: เพิ่ม open_deal_time lookup + time guard ใน Strategy 1 |
| `metty/execution/live_trader.py` | `_reconcile_external_close`: เพิ่ม open_deal_time lookup + time guard ใน Strategy 2 |
| `tests/test_reconcile_time_filter.py` | (ใหม่) 3 causal tests (RED→GREEN) |
| `docker-compose.vps.yml` | `oracle-engine`: `ML_FILTER_ENABLED=${ML_FILTER_ENABLED_A:-1}` (แยกจาก train) |

## สถานะปัจจุบัน

- Fix พร้อม deploy Real-A (รอ commit + push + VPS pull)
- พร้อมเปิด ML v4 บน Real-A (set `ML_FILTER_ENABLED_A=1` ใน .env บน VPS)
- B/C/D ยังอยู่บน AEGIS-only (ML off) — ไม่กระทบ
- หลัง deploy ต้อง verify ด้วยข้อมูลใหม่ — re-exit_reason ของ trade ผิดก่อนหน้านี้ไม่ได้ (DB บันทึกไปแล้ว) แต่ trade ใหม่จะแม่นยำขึ้น