# External-Close Reconcile Ghost Trade Bug (ISSUE-077)

> วันที่: 6 กรกฎาคม 2026
> เกี่ยวข้อง: [[2026-07-03_real-a-no-ml-withdrawal-strategy]], [[2026-06-19_mt5-source-of-truth-ghost-positions]], [[2026-06-22_ghost-trade-prevention]]
> ประเภท: learning (bug fix — CPT)
> scope: Real-A production bug + causal test + fix + deploy

## สรุป

Real-A หลัง deploy no-ML (2026-07-03) เปิด trade 1 อัน BUY @ $4170.26, SL $4158.10. โบรกเกอร์ปิด SL ที่ $4158.06 (loss -$12.20) ในวันเดียวกัน. แต่ DB ยังโชว์ `is_open=1` เป็นเวลา 3 วัน เพราะ `_monitor_positions` คอนflate "MT5 position not found" (broker ปิดแล้ว) กับ "MT5 close failed" (จริง ๆ) → ทั้งสอง return `(False, None)` → trader ตีความเป็น "close failed, retry next cycle" → ghost trade ค้าง → M5 scalp งด trade ใหม่เพราะเห็น "1 open in DB".

## Bug symptom

```
WARNING Position 2715202701 not found
WARNING [Real-A] MT5 close failed for ticket 2715202701 — leaving DB open, will retry next cycle
```

- DB: trade #5537 `is_open=1`, exit_price=NULL, pnl=NULL (ghost)
- MT5: 0 open positions, balance=$387.80 ($400 - $12.20)
- M5 scalp: hold "position limit (1/1)" — งด trade ใหม่
- Reconcile loop รันทุก 5 นาที ไม่ converge

## Root cause

`_close_mt5_position_with_fill` ใน `metty/execution/live_trader.py` return `(False, None)` สองกรณี:
1. **Position no longer exists** (broker closed via SL/TP/manual) → DB ควร reconcile จาก deal history
2. **MT5 connection / order_send failed** → ควร retry next cycle (current behavior)

Trader ตีความทั้งสองเหมือนกัน → ghost trade ค้าง

## Hypothesis (CPT format)

"Bug occurs because `_monitor_positions` calls `_close_mt5_position_with_fill` which returns `(False, None)` when `positions_get(ticket)` returns empty (broker already closed). The trader treats this as 'MT5 close failed' and `continue`s without updating DB, leaving is_open=1 forever — a ghost that blocks all new entries via max_positions check."

## Causal test (RED → GREEN)

ไฟล์: `tests/test_live_trader_external_close_reconcile.py` (3 tests)

| Test | จุดประสงค์ | RED | GREEN |
|------|-----------|-----|-------|
| `test_ghost_reconciled_when_position_already_closed_by_broker` | กรณี 1: broker ปิดแล้ว + deal history มี closing deal → DB ต้อง is_open=0 | ✅ fail (is_open=1 ghost) | ✅ pass (is_open=0, exit=$4158.06) |
| `test_ghost_remains_when_no_closing_deal_found` | sanity: bridge OK แต่ไม่มี closing deal → ต้อง retry (is_open=1) | ✅ pass | ✅ pass |
| `test_bridge_failure_does_not_false_close` | sanity: bridge down → ห้าม false-close (is_open=1) | ✅ pass | ✅ pass |

Test 1 ล้มเหลวก่อน fix → ยืนยันว่า hypothesis ถูก. Test 2-3 pass อยู่แล้วเพราะ current behavior ถูกต้องในกรณีนั้น.

## Fix

เพิ่ม `_reconcile_external_close(ticket, direction, entry_price, sl, tp)` helper ใน `LiveTrader`:
- Query deal history (already have `_get_deal_history`)
- Match closing deal 3 strategies (mirror `scripts/sync_ghost_trades.py`):
  1. `deal.order == ticket AND deal.type == closing_type` (MT5 link)
  2. `deal.position_id == ticket` (if bridge exposes — add `position_id` to DEAL_COLUMNS)
  3. Price proximity: `deal.type == closing_type AND |deal.price - entry_price| < 0.5%`
- Derive exit_reason จาก `deal.reason` (4=SL, 5=TP, 6=SO) หรือ comment pattern (`[sl ...]`, `[tp ...]`)
- Return `{"exit_price": float, "exit_reason": str}` หรือ None

Wire เข้า `_monitor_positions` + `_execute_tp1_close`:
```python
if not mt5_ok:
    external = self._reconcile_external_close(ticket, direction, entry_price, sl, tp)
    if external is not None:
        actual_fill = external["exit_price"]
        if external.get("exit_reason"):
            exit_reason = external["exit_reason"]
        # fall through to DB close
    else:
        logger.warning("MT5 close failed — leaving DB open, retry next cycle")
        continue  # กรณี 2: จริง ๆ ล้มเหลว
```

## Verification

| Step | Result |
|------|--------|
| Causal test RED (before fix) | ✅ test 1 fail (is_open=1 ghost) |
| Causal test GREEN (after fix) | ✅ all 3 pass |
| Regression: 76 live_trader tests | ✅ all pass |
| Pre-deploy check | ✅ pass (ML warning harmless — ML off) |
| Deploy Real-A | ✅ container healthy in <20s |
| Production log | `INFO [Real-A] Reconciled external close for ticket 2715202701 — exit=$4158.06 reason=stop_loss` ✅ |
| DB after reconcile | trade #5537: is_open=0, exit=$4158.06, pnl=-$12.20, reason=stop_loss ✅ |
| Open positions | 0 ✅ (matches MT5) |

## บทเรียน

1. **Conflated failure modes** — return value `(False, None)` ของ `_close_mt5_position_with_fill` ไม่แยก "position gone" กับ "close failed" → caller ตัดสินใจผิด. Rule: ถ้า operation มีหลาย failure modes, return ต้องแยกให้ caller เลือก action ได้.

2. **Causal test จับ bug ที่ unit test ไม่ catch** — Test 1 ตั้งใจสร้าง scenario "broker closed + deal history มี record" ซึ่งเป็น mechanism ไม่ใช่ symptom. Test 2-3 ตั้งใจ verify ว่า fix ไม่ทำลาย sanity (no false-close).

3. **Ghost trade block all new entries** — `max_positions` check ดู DB count ถ้า DB ผิด → trader งด trade ใหม่ → ระบบนิ่ง 3 วันโดยไม่ trade. DB ต้อง mirror MT5 ตลอด — reconcile ทุก cycle.

4. **Deal history คือ source of truth** — เมื่อ MT5 position หายไป, deal history ยังเก็บ closing deal พร้อมราคาจริง + reason. ใช้ต่อ PnL/exit_reason ได้เลย ไม่ต้อง fallback entry_price.

5. **Strategy 0 (deal.order == ticket) ไม่ work สำหรับ broker-closed deals** — Exness SL-closed deal มี `order=0` (ไม่ใช่ position ticket). Strategy 1 (price proximity) จับได้. ถ้ามี `position_id` field ใน MT5 API จะแมทช์ exact กว่า — เพิ่ม `position_id` เข้า DEAL_COLUMNS ของ client ไว้แล้ว.

## ไฟล์ที่แก้

| ไฟล์ | การแก้ |
|------|-------|
| `metty/execution/live_trader.py` | เพิ่ม `_reconcile_external_close` helper + wire เข้า `_monitor_positions` + `_execute_tp1_close` |
| `tests/test_live_trader_external_close_reconcile.py` | (ใหม่) 3 causal tests (RED→GREEN) |

## สถานะปัจจุบัน

- Fix deployed to Real-A, container healthy
- Ghost #5537 reconciled (is_open=0, exit=$4158.06, pnl=-$12.20, reason=stop_loss)
- M5 scalp รอ next cycle (5 min) เพื่อ verify ว่าเลิก hold "1/1 position limit"