# Healthcheck healthy ≠ terminal logged in — retry ต้องมี spacing และอย่าเชื่อข้อความ "manual VNC"

**วันที่**: 2026-10-02 · **Issue**: ISSUE-093 · **File**: scripts/oracle_runner.py

## เกิดอะไร

ตอน mt5 container ถูก recreate, terminal GUI เสร็จ auto-login **หลายนาทีหลัง healthcheck ขึ้น healthy** (incident 2026-09-24: engine พยายาม login 02:48 แล้วยอมแพ้, terminal login เองสำเร็จ 03:03) — startup `ensure_mt5_logged_in` ลองแค่ 3×5s แล้ว log "manual VNC login required" (หลอก — ไม่มี VNC check ไหนเลย สถานการณ์ self-heal เอง), ส่วน `_mt5_health_check` ใน cycle ลอง login **ครั้งเดียวไม่มี spacing** และเผลอ ignore return ของ `initialize()`

## บทเรียน

1. **Failure window ตอน terminal ยัง boot = "ช่วงเวลา" ไม่ใช่ "สถานะถาวร"** — วิธีแก้ที่ถูกคือ retry มี spacing ให้ทันเวลาที่ terminal เสร็จเอง ไม่ใช่ประกาศให้คนเข้ามาแก้มือ
2. **Bound ของ retry loop ต้องน้อยกว่า cycle interval**: `MT5_RELOGIN_ATTEMPTS=3 × MT5_RELOGIN_SPACING_SECONDS=60 = 180s < 300s cycle` ไม่งั้น worker ตีบกันเอง
3. **`login()` หลัง `initialize()` fail เป็น RPC ที่ตายแล้ว** — เช็ค return ของ initialize ก่อน แล้วค่อย login (rpyc "result expired" ตอน bridge ยุ่งจะกลายเป็น attempt ที่เสียเปล่า)
4. **Guard สำหรับ brokerless ต้องอยู่ก่อน rpyc**: `MT5_AUTO_LOGIN=0` → return เลย ไม่งั้น farm (10 accounts × 288 cycles) สร้าง warning noise เปล่าๆ ทุกวัน

## ใช้ยังไงต่อ

ออกแบบ retry ใน cycle loop ให้ตอบ 3 คำถาม: spacing เท่าไหร่, bound รวม < interval ไหม, มี path "off switch" สำหรับ brokerless ไหม — แล้วเขียน causal test พิสูจน์ทั้งสาม (tests/test_mt5_relogin_spaced_retry_causal.py)

**Related**: [[2026-09-24_mt5-healthy-ne-terminal-logged-in]] · [[2026-09-24_live-mr-bet-data-in-container-db]]