# Retro — Kill Switch ระดับพอร์ต multi-trigger (supervisor แยกจาก engine)

**งาน**: คำสั่งคุณหมอ 16/11/2026 (ผ่าน Hermes) — supervisor ใหม่ `scripts/kill_switch.py` + service `kill-switch` ใน `docker-compose.vps.yml` ตามมาตรฐาน FIA (Best Practices for Automated Trading Risk Controls) · ปิดงาน 17/11/2026
**Branch**: mr-bet-bcd

## อะไรเปลี่ยน
1. `scripts/kill_switch.py` (ใหม่ ~830 บรรทัด) — supervisor loop 30s: trigger 5 ตัวอิสระ (daily loss 3% เทียบต้นวัน UTC · DD จาก peak 10% · order-rate >20 ไม้/ชม./บช. · price deviation >1% จาก median ข้ามบช. · connectivity ไม่มีข้อมูลใช้ได้ >10 นาที) ทุกตัวปรับผ่าน env ได้
2. `docker-compose.vps.yml` — service `kill-switch` (additive ล้วน: 13 services เดิม + 1) + named volume `kill-switch-state` — dry-run default, engine DB mount `:ro`, docker.sock สำหรับ stop
3. Tests ใหม่ 3 ไฟล์ 56 ตัว — `test_kill_switch.py` (behavior, mock bridge ทั้งหมด) · `test_kill_switch_infra.py` (compose contract + env parser == compose) · `test_core_integrity.py` (manifest sha256 ล็อก IRON scope 124 ไฟล์)

## สถาปัตย์ที่เลือก (ตัดสินใจสำคัญของงาน)
**"Handle-safe read client" บน bridge เดิมของ engine — ไม่ใช่ DB-snapshot ล้วน (ก) และไม่ใช่ bridge ใหม่ (ข):**
- อ่านต้นกลไกจริงจาก `docker/mt5/mt5_bridge_server.py`: `exposed_shutdown()` เรียก `mt5.shutdown()` **ระดับ process กลาง** — นี่คือกลไกที่ cycling client ฆ่า handle ของ engine (บทเรียน "bridge หนึ่งใบ = terminal handle เดียว")
- `HandleSafeBridge` จึง **ไม่มีทางเรียก initialize/shutdown เลย** — ขี่ handle ที่ engine initialize ไว้, ปิดแค่ `conn.close()` — ต่างจาก PM ที่เรียก `fetch_account_info_sync`→`disconnect`→`root.shutdown` ทุก 30 นาที
- เหตุผลที่ไม่ใช่ (ก) ล้วน: DB ของ engine **ไม่มี live equity** snapshot ต่อ cycle (มีแต่ realized PnL + peak ที่ PM เขียน) — daily-loss 3% ที่มี floating PnL อ่านจาก DB ไม่ได้จริง · order-rate ยังใช้ DB (`:ro` + `mode=ro`) เพราะเป็นข้อมูลที่ engine เขียนอยู่แล้วจริง
- เหตุผลที่ไม่ใช่ (ข): bridge "เฉพาะ" = MT5 terminal ใหม่ต่อบช. (แพง และต้องแตะ docker/mt5) — ส่วน RPyC connection ที่สองบน bridge server เดิมไม่ฆ่า handle ตราบใดที่ไม่ initialize/shutdown (ThreadedServer รับได้)
- Connectivity probe = TCP socket ล้วน (ไม่พูด RPyC) — ปลอดภัยที่สุดในทุก call ที่ supervisor ยิงทุก 30 วิ
- Trade-off ที่ยอมรับ: handle-safe pattern **ยังไม่เคยพิสูจน์บน Wine bridge จริง** — KILL_SWITCH_DRY_RUN=1 (default) รัน path อ่านทั้งหมดบน VPS จริงโดยไม่ flatten/stop — dry-run period คือช่วงพิสูจน์ ก่อน arm

## บั๊กจริงใน implementation หลัก: 0 ตัว · บั๊กในตัวตรวจเอง: 1 ตัว
บั๊กที่ test จับได้ระหว่างเขียน (แก้ก่อน merge):
1. **Act ซ้ำต่อ trigger** — บช. B ยิง daily_loss + dd พร้อมกัน → stop container 2 ครั้ง → ปรับให้ _act รัน **ครั้งเดียวต่อบช.ต่อ cycle** (จัดกลุ่ม trigger ตามบช.ก่อน) — ความผิดพลาดแบบนี้กับ docker stop คือ restart loop เลย
2. **FakeRoot ไม่ลบ position ที่ปิดแล้ว** — ทำให้ enforcement pass ปิดซ้ำ ticket เดิมใน mock (จริงๆ MT5 ลบให้) — แก้ mock ให้เหมือน terminal จริง ไม่ใช่ปิดหู mock ให้ test ผ่าน

บั๊กที่ full suite จับได้ แต่ run เดี่ยวไม่เห็น (RED→GREEN จริง):
3. **Test ผมไม่ hermetic ต่อ env ที่รั่ว** — run เดี่ยวผ่านหมด แต่ใน full suite สอง test กลุ่ม REAL fail · หลักฐาน: ปิด hermetic cleanup ชั่วคราวแล้วรัน polluters + kill switch tests → **2 failed `KeyError: ('mt5a', 8001)`** ตรงกับที่ suite เต็ม fail เป๊ะ · สาเหตุ: โมดูลเก่า 3 ตัว (`test_check_position_initialize.py`, `test_reconcile_time_filter.py`, `test_live_trader_external_close_reconcile.py`) ใช้ `os.environ.setdefault("MT5_BRIDGE_A_HOST", "localhost")` **ไม่ cleanup** → kill switch อ่าน host ของ A เป็น localhost · แก้ที่ตัวเอง: make_config ล้าง `KILL_*`/`MT5_BRIDGE_*`/`MT5_SYMBOL_*` ทุกตัวก่อน build (ไม่แตะ test คนอื่น) → รันชุดเดิมได้ **44 passed** · สัญญาณเตือน: **run เดี่ยวผ่าน ≠ ผ่าน — suite เต็มคือกรณี test จริง** (pattern เดียวกับ retro 12/11: ตัวตรวจก็เป็นโค้ดที่ผิดได้)

## หลักฐานจากการรันจริง
- `test_kill_switch.py` (36) + `test_kill_switch_infra.py` (18) + `test_core_integrity.py` (2) = 56 ตัวใหม่ ผ่านหมด
- **Full suite หลังแก้ hermetic: 1457 passed, 2 skipped, 0 failed (692 วินาที)** — baseline ก่อนงาน 1401 passed 2 skipped → +56 ไม่มีของเดิมแตก
- พิสูจน์ RED→GREEN ของบั๊ก hermetic: ปิด fix + polluters → `2 failed KeyError ('mt5a', 8001)` → เปิด fix → 44 passed
- `git status` ยืนยัน: ไฟล์ใน metty/ broky/ shared/ docker/mt5/ Dockerfile ไม่แตะแม้บรรทัดเดียว (manifest lock 124 ไฟล์ทำให้เป็นการันตีถาวร ไม่ใช่แค่คำสัญญา)

## บทเรียน
1. **อ่านกลไกของ "บทเรียน" ก่อนเชื่อข้อสรุป** — "cycling client ฆ่า handle" ไม่ใช่ magic: มันคือ `root.shutdown`→`mt5.shutdown()` ระดับ process รู้กลไก = ออกแบบทางเลือกที่สาม (handle-safe) ได้ แทนที่จะเลือกแค่ 2 ทางที่โจทย์เสนอ
2. **Policy act ต้อง idempotent ต่อบช.** — หลาย trigger ยิงพร้อมกันเป็นเรื่องปกติของ kill switch (daily loss และ DD มักไปด้วยกัน) — act-once-per-account ต่อ cycle เป็น invariant ที่ควรเป็น test
3. **Supervisor กับ alert แยกกัน** — trigger แจ้งครั้งเดียว (dedup ใน state) แต่การบังคับ (re-flatten ไม้ใหม่ของบช.ที่ถูก kill) ทำเงียบทุก cycle — ไม่งั้น TG คือ spam และไม้ใหม่คือช่องโหว่

## ค้าง / next
- **Deploy VPS เป็น approval แยก** (คำสั่งคุณหมอ) — runbook อยู่ใน handoff
- **Dry-run period บน VPS** อย่างน้อย 1-2 สัปดาห์ — ดูว่า trigger ไหร่ยิงบ่อยเกินจริง (สะท้อนค่า default) และ handle-safe pattern ไม่ทำให้ engine "stream has been closed" เพิ่ม
- **ช่องว่างที่รู้และยอมรับ**: REAL policy ห้าม stop container → engine ที่ยังรันเปิดไม้ใหม่ได้ — supervisor re-flatten ทุก cycle (enforcement) แต่คนต้องเข้ามาตัดสินใจเอง — ปิดช่องนี้ต้องแตะ core ซึ่งโจทย์ห้าม
- State ของ connectivity ตอน VPS restart ทั้งเครื่อง: last_good_data เก่า > 10 นาที → ยิง connectivity รอบแรกหลัง boot ทุกบช. (เก็บไว้เป็นข้อสังเกตระหว่าง dry-run — ถ้ารำคาญให้เพิ่ม grace period หลัง boot)
