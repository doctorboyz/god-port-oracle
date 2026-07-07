# Raw rpyc Missing initialize() — False "bridge down" (ISSUE-078)

> วันที่: 7 กรกฎาคม 2026
> เกี่ยวข้อง: [[2026-07-06_external-close-reconcile-ghost-trade]], [[2026-05-18_asyncio-run-thread-context-bridge]]
> ประเภท: learning (bug fix — CPT)
> scope: Real-A production bug + causal test + fix + deploy

## สรุป

หลัง deploy ghost fix (ISSUE-077) ระบบยังไม่ trade — M5 scalp ยัง hold ทุก cycle. ตรวจพบว่า warning เปลี่ยนจาก `MT5 bridge returned None (disconnected)` เป็น `MT5 position check failed: stream has been closed — falling back to DB`. Root cause จริง: ทั้ง swing trader (`live_trader.py:_check_existing_position` line 803) และ m5 scalp trader (`m5_scalp_trader.py:_check_existing_m5_scalp_position` line 523) ใช้ raw `rpyc.connect()` + `conn.root.positions_get()` **โดยไม่เรียก `conn.root.initialize()` ก่อน**. MT5 terminal ใน Wine ต้องการ `initialize()` ทุกครั้งก่อน query — ถ้า terminal อยู่ใน state shutdown (หลัง MT5Bridge wrapper เรียก shutdown()) query ทุกอย่างคืน None.

## Bug symptom

```
WARNING [M5Scalp:Real-A] MT5 bridge returned None (disconnected) — skipping reconcile, holding off new trades (0 open in DB)
WARNING [M5Scalp:Real-A] MT5 position check failed: stream has been closed — falling back to DB
```

ทุก 5 นาที M5 scalp cycle hold off new trades เพราะคิดว่า bridge down.

## Root cause

ทั้งสอง trader ใช้ raw rpyc pattern เดิม:
```python
conn = rpyc.connect(cfg.bridge_host, cfg.bridge_internal_port, config={"sync_request_timeout": 10})
positions_raw = conn.root.positions_get(symbol=cfg.symbol)
conn.close()
```

ไม่มี `conn.root.initialize()` ก่อน `positions_get()`. ในขณะที่ `MT5Bridge` wrapper ที่ live_collector ใช้เรียก `initialize()` + `shutdown()` ทุก cycle (`fetch_candles_sync`):
```python
async def _do():
    if not await self.connect():  # initialize()
        return pd.DataFrame()
    df = await self.get_candles(...)
    await self.disconnect()  # shutdown()
```

พอ live_collector shutdown terminal แล้ว m5_scalp/swing มา query → MT5 คืน None (terminal shutdown state).

Race condition: timing ของ cycle ตัดสินว่า trade ได้ไหม — ถ้า timing ตรงที่ terminal initialized ค้าง → trade ได้. ถ้า timing ตรงที่ terminal shutdown → ไม่ trade.

## Hypothesis (CPT format)

"Bug occurs because `_check_existing_position` (swing) and `_check_existing_m5_scalp_position` (m5 scalp) call `conn.root.positions_get()` via raw rpyc without first calling `conn.root.initialize()`. MT5 terminal in Wine requires `initialize()` before any query — without it, `positions_get()` returns None when terminal is in shutdown state (after MT5Bridge wrapper's shutdown()). The trader treats None as 'bridge disconnected' and holds off new trades, but actually the bridge is fine — just not initialized. Race condition where timing decides if trades happen."

## Causal test (RED → GREEN)

ไฟล์: `tests/test_check_position_initialize.py` (2 tests)

| Test | จุดประสงค์ | RED | GREEN |
|------|-----------|-----|-------|
| `TestSwingCheckExistingPositionInitializes::test_initialize_called_before_positions_get` | swing trader ต้องเรียก initialize ก่อน positions_get | ✅ fail (initialize ไม่ถูกเรียก) | ✅ pass |
| `TestM5ScalpCheckExistingPositionInitializes::test_initialize_called_before_positions_get` | m5 scalp trader ต้องเรียก initialize ก่อน positions_get | ✅ fail | ✅ pass |

Mock `_FakeMt5Root` คืน None สำหรับ `positions_get` ถ้าไม่มี `initialize()` ก่อน (mimic MT5 terminal shutdown จริง) และคืน `[]` ถ้ามี `initialize()` ก่อน. ทดสอบนี้พิสูจน์ mechanism ไม่ใช่ symptom — เป็น CPT จริง.

## Fix

เพิ่ม `conn.root.initialize()` ก่อน `conn.root.positions_get()` ในทั้งสอง raw rpyc path:
```python
conn = rpyc.connect(cfg.bridge_host, cfg.bridge_internal_port, config={"sync_request_timeout": 10})
try:
    if not conn.root.initialize():
        logger.warning("MT5 initialize failed (last_error=%s) — treating as bridge down",
                       self.display_name, conn.root.last_error())
        return len(get_open_trades(self.account_id, self.db_path)) > 0
    positions_raw = conn.root.positions_get(symbol=cfg.symbol)
finally:
    try:
        conn.root.shutdown()
    except Exception:
        pass
    conn.close()
```

`try/finally` ป้องกัน leak — `shutdown()` ปิด terminal ให้ state สะอาดสำหรับ caller ถัดไป.

## Verification

| Step | Result |
|------|--------|
| Causal test RED (before fix) | ✅ ทั้งสอง fail (initialize ไม่ถูกเรียก) |
| Causal test GREEN (after fix) | ✅ ทั้งสอง pass |
| Regression: 709 tests | ✅ ผ่าน (1 skipped) |
| Pre-deploy check | ✅ ผ่าน (ML warning harmless) |
| Deploy Real-A (commit 5d6d95c) | ✅ container healthy |
| Direct rpyc test 5/5 ครั้งจาก oracle-engine → mt5a | ✅ initialize=True ทุกครั้ง, positions=list (ไม่ใช่ None) |
| Deployed code check | ✅ ISSUE-078 comment อยู่ใน `/app/metty/execution/m5_scalp_trader.py` |

## บทเรียน

1. **MT5 terminal ใน Wine ต้องการ initialize() ทุกครั้ง** — ไม่เหมือน TCP connection ที่ stateful. แต่ละ session ต้องเริ่มด้วย `initialize()` และจบด้วย `shutdown()`. ถ้าข้าม `initialize()` query ทุกอย่างคืน None — ดูเหมือน bridge down แต่จริง ๆ terminal ยังไม่ตื่น.

2. **Race condition จากสอง pattern ผสมกัน** — live_collector ใช้ MT5Bridge wrapper (init+shutdown ทุก cycle), ส่วน swing/m5_scalp ใช้ raw rpyc (ไม่ init). สอง pattern นี้แยกจากกันทำงานได้ แต่พอรันพร้อมกัน timing ตัดสินผล. ต้องใช้ pattern เดียวกันทุกที่ — ถ้า raw rpyc ต้องมี initialize/shutdown ครบ. ถ้า wrapper ต้องใช้ wrapper ทุกที่.

3. **Causal test จับ bug ผ่าน call order** — ไม่ใช่ดูแค่ result. ทดสอบนี้ track `call_order` list เพื่อพิสูจน์ว่า `initialize` ถูกเรียกก่อน `positions_get`. ทดสอบ result ปกติไม่พอ — ต้องพิสูจน์ mechanism.

4. **"stream has been closed" ≠ "bridge returned None"** — อาการคล้ายกันแต่ root cause ต่าง. None = terminal shutdown (query ก่อน initialize). stream closed = RPyC connection-level race (อีกอาการที่ยังเหลือหลัง fix ISSUE-078). อย่าสรุปเร็วว่าเป็น bug เดียวกัน.

5. **Direct test จาก container สำคัญ** — ทดสอบตรง ๆ จาก oracle-engine ไป mt5a (5/5 ครั้ง initialize สำเร็จ) พิสูจน์ว่า bridge ปกติ — ปัญหาอยู่ใน code path ของ trader. ถ้าทดสอบตรง ๆ ไม่ได้ → ปัญหาที่ bridge/server. ทดสอบตรง ๆ ได้ → ปัญหาที่ client code.

## ไฟล์ที่แก้

| ไฟล์ | การแก้ |
|------|-------|
| `metty/execution/live_trader.py` | เพิ่ม `initialize()` + try/finally `shutdown()` ใน `_check_existing_position` (line 803) |
| `metty/execution/m5_scalp_trader.py` | เพิ่ม `initialize()` + try/finally `shutdown()` ใน `_check_existing_m5_scalp_position` (line 523) |
| `tests/test_check_position_initialize.py` | (ใหม่) 2 causal tests (RED→GREEN) |

## สถานะปัจจุบัน

- Fix deployed to Real-A (commit 5d6d95c)
- ISSUE-078 marked fixed
- Bridge ทำงานได้เมื่อ initialize ถูกเรียก (verified 5/5 ตรง)
- ปัญหา "stream has been closed" ที่เหลือใน cycle logs เป็น RPyC connection-level race แยกต่างหาก — อาจเป็น ISSUE-079 ใหม่ถ้า persist หลัง London session เริ่ม
- M5 scalp ยัง hold ใน Asian session (by design) — จะเห็นผลจริงตอน London session (~14:00 ไทย / 07:00 UTC)