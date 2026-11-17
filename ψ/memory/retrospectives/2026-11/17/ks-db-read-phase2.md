# Retro — ISSUE-010 เฟส 2: kill-switch อ่าน equity/สถานะจาก DB ล้วน (17/11/2026)

> คำสั่งคุณหมอ (ผ่าน Hermes, "แก้แต่ละเฟสตามแผน"): ชั้นพิทักษ์เงินต้องไม่พึ่ง bridge
> ที่มันปกป้อง — equity/DD/connectivity ต้องมาจาก engine DB · bridge เหลือแค่
> tick cross-check + flatten

## สิ่งที่ทำ (ทำอะไร → เพื่ออะไร → แล้วไง)

### 1. Engine equity logging (metty additive ล้วน)
- `metty/core/db.py`: migration เดิมเพิ่ม 2 columns `accounts.current_equity` (REAL) +
  `accounts.equity_ts` (TEXT) + ฟังก์ชันใหม่ `update_equity_snapshot()` (UPDATE เดียว:
  equity + UTC timestamp) — เพื่อให้ engine มี heartbeat ที่ kill-switch อ่านได้โดยไม่แตะ bridge
- `live_trader.py`: `_write_equity_snapshot()` เรียกที่ **top-of-cycle** หลัง init_db —
  จุดสำคัญที่สุดของงานนี้: cycle ที่ skip/hold กลางทาง (ไม่มี data, มีไม้เปิด, gate บล็อก)
  ต้องยังเขียน heartbeat เสมอ ถ้าเขียนตรงจุด equity ถูกใช้ (กลาง cycle) ไม้ที่เปิดค้างจะทำให้
  ts เหมือน engine ตาย → connectivity trigger ไฟลวง
- `m5_scalp_trader.py` (Real-A ใช้ engine นี้): stamp จาก **bridge equity เท่านั้น**
  ห้ามผ่าน `_get_balance()` ที่ fallback เป็น DB balance เมื่อ bridge ล่ม — ts สดบน equity
  เน่า = fabricate heartbeat ปลอม (kill-switch จะเชื่อว่า engine สบายดีทั้งที่ตาบอด)
  → ไม่มี bridge equity = ไม่ stamp = connectivity ยิงจริงตามเหตุการณ์จริง
- ไม่ env-gate = ไม่แตะ compose ทุกไฟล์ · ไม่แตะสูตร sizing/SL/TP/live_trader core แม้บรรทัด

### 2. kill_switch.py `_observe()` สลับแหล่งอ่านเป็น DB ล้วน
- equity = `accounts.current_equity` · peak = `max(state-peak, accounts.peak_equity, equity)`
  (seed จาก DB peak ที่ portfolio_manager ดูแล — high-water ไม่มีวันลด) · connectivity =
  ความสดของ `equity_ts` (จับ "engine ตาย" ได้ ไม่ใช่แค่ bridge ตาย) · order_rate อ่าน DB เดิม
- **ถอด `HandleSafeBridge.account_info()` ออกทั้ง method** — read ที่เคยรบกวน handle
  เดียวของ bridge ตามหลักฐาน production 18/18 fail · TCP probe เหลือเป็น diagnostic
  (ไม่พูด RPyC) · DB read ทำงานแม้ bridge ล่ม (DB เป็น volume mount ไม่ใช่ socket)
- `data_ok` = equity มา **และ** ts สดใน window — stale equity ไม่ว่างเข้าสูตร loss/DD
  (design lock: engine ตายแล้ว ตัวเลขเก่าอาจเก่ากว่าเหตุการณ์ และ action ของ connectivity
  ก็ flatten+stop เหมือนกันอยู่แล้ว — test `test_stale_equity_skips_loss_triggers` ล็อกไว้)

### 3. Phase 1 ที่ยังมีประโยชน์ (ตาม scope)
- **retry-once + jitter** (0.2–0.8s) บน read ที่เหลือ: `tick_mid` + `positions()` (flatten path) —
  blip ชั่วครู่ไม่ฆ่า flatten จริง · **ห้าม retry `order_send` เด็ดขาด** (ambiguous order
  ต้องไม่ส่งซ้ำ)
- **ISSUE-013 log ชนิดจริง**: `_describe_remote()` ใช้ repr() materialize อย่างถูกวิธีบน netref
  → log บอก `type=str repr='<tick unavailable>'` แทน type ของ wrapper · ใช้ทั้ง tick + positions
- **hourly success-rate** บรรทัดเดียว/ชม./บช.: `[KS] hourly P1: db 118/118 (100.0%) tick 58/60 (96.7%)`
  — tick ที่ไม่ได้ attempt (TCP ล่ม) ไม่ถูกนับเป็น fail

### 4. Tests — เลขจริง
- ไฟล์ใหม่ `tests/test_kill_switch_db_read_causal.py`: **35 passed** (engine snapshot 4 + LiveTrader 3 +
  M5Scalp 2 + _observe DB-source 8 + triggers-on-DB 6 + retry/jitter 4 + remote-type-log 4 +
  hourly 2 + evaluate semantics 2)
- `tests/test_kill_switch.py`: **41 passed** (47 − 6: ถอด `TestAccountInfoShapeSafety` ตาม method
  ที่ถูกลบ — สัญญา shape-log ย้ายไปครอบ tick/positions ในไฟล์ใหม่) · FakeRoot เพิ่ม
  `account_info_calls` counter: test ยืนยัน supervisor ไม่โทน account_info เลย
- RED proof แบบ CPT (พลิกโค้ดชั่วคราว → แดงตัวที่ต้องแดง → คืนค่า → เขียว):
  1. พลิก `_observe` ให้ไม่สน freshness → `test_stale_ts_is_not_data_ok` +
     `test_stale_equity_skips_loss_triggers` แดงพอดี 2 ตัว
  2. พลิกถอด seeding จาก `db_peak` → `test_dd_fires_seeded_from_db_peak` แดงพอดี
     (`state_peak_wins` ยังเขียว = แดงตรงจุด ไม่ใช่แดงฟรี)
- ทิศทาง "equity จาก DB ไม่ใช่ bridge" พิสูจน์ในตัว test เอง: bridge โกหก equity=1.0,
  DB บอก 990 → assert 990 + `account_info_calls == 0`
- manifest core_integrity: diff preview ก่อน update = CHANGED เป๊ะ 3 ไฟล์
  (db.py / live_trader.py / m5_scalp_trader.py) ตรวจด้วยตาแล้วค่อย --update

## บทเรียน (ยังใช้ได้ทีหลังด้วย)

1. **วาง write-point ที่ top-of-cycle ไม่ใช่จุดที่ค่าถูกใช้** — cycle มี early-return เยอะมาก;
   ค่า monitoring ต้องเขียนก่อนทุก gate ไม่งั้น "มีไม้เปิด" จะกลายเป็น "engine ดูเหมือนตาย"
2. **ห้าม stamp heartbeat บนค่า fallback** — บนเส้นที่มี fallback อยู่แล้ว (_get_balance)
   ต้องแยกทาง เอาเฉพาะค่าจากแหล่งจริง ไม่งั้น monitoring โกหกตัวเอง
3. เศษจากงานก่อนที่กลายเป็นหลักฐาน: production log "stream has been closed" บน engine +
   "account_info failed" บน supervisor คือ **คนละฝั่งของเหตุการณ์เดียวกัน** — ตัดสินใจดีไซน์จาก
   คู่นี้ได้เลย ไม่ต้องรู้ root cause ของ rpyc ก่อน

## Cross-context review (บังคับงานเสี่ยง — ผลจริง)

Reviewer ไร้ context คนแก้ ตรวจจาก diff อย่างเดียว + รัน test เอง + **reproduce HIGH ด้วยมือ**:
สร้าง engine DB รุ่นเก่า (ไม่มี snapshot columns) + armed supervisor + state เก่าจาก era
bridge → connectivity ยิงทันทีรอบแรก (silent 2880 นาที นับจาก `last_good_data` เก่า) → flatten +
docker stop + block ถาวร — **ผิดลำดับ deploy กลายเป็นเหตุการณ์เงินจริง ที่ runbook กันไม่ได้**

- **HIGH → แก้แล้ว**: fail-closed boot guard `snapshot_schema_problem()` — armed + DB รุ่นเก่า
  (หรือไม่มี `KILL_DB_PATH_*` เลย) = ปฏิเสธบูต error ชัด ตาม convention `_env_dry_run` เดิม ·
  dry-run บูตผ่านตามเจตนา (มันแจ้งเตือน และ alert ตอน gap คือสิ่งที่บอก operator) ·
  DB file ที่ยังไม่มี (engine ยังไม่ start) ไม่ถูกบล็อก — connectivity semantics ของมันจริง ·
  RED ก่อนแก้: 3 ตัว refuse แดงพอดี → แก้ → เขียว 6/6
- **MEDIUM (churn) → แก้แล้ว**: m5_scalp stamp ผ่าน `_get_balance` + เขียนเฉพาะ cache อุ่น
  (<60s) — cache ถูก set เฉพาะจาก MT5 equity จริง ดังนั้น (1) cold session ต่อ cycle ไม่เพิ่ม
  (เรียกใน cycle ต่อมาโดน cache) (2) ค่า fallback เน่าไม่มีทางกลายเป็น stamp สด — แก้สองประเด็น
  (review churn + heartbeat ปลอม) ด้วยกลไกเดียว
- **MEDIUM (ยอมรับ, ไม่ regression)**: equity ≤ 0 ไม่ถูก stamp → บัญชีระเบิดไม่เข้าสูตร loss/DD
  เหลือแต่ connectivity ตามหลัง — พฤติกรรมเดียวกับ era bridge เดิม · ถ้าจะให้ยิง DD ทันที
  ตอบลบศูนย์ เปิดงานแยก (เปลี่ยน kill semantics = งานที่ต้อง review ของตัวเอง)
- **LOW (จดไว้ ไม่แก้ในงานนี้)**: `_boot_time` inject เป็น attribute (production path ปลอดภัย) ·
  hourly bucket หายเมื่อ restart กลางชั่วโมง · `db_ok` นับ "row มา" ไม่ได้แยก "stamp สด" ·
  m5_scalp ไม่เรียก init_db เอง (runner เรียกให้)
- Reviewer ยืนยันเองด้วย: scope IRON ครบ (แตะเฉพาะ 6 ไฟล์ตามรายงาน, oracle_runner/compose/
  docker/broky/shared = zero diff) · heartbeat เขียนก่อน early-return ทุกทางในทั้งสอง engine ·
  order_send ไม่ถูก retry เด็ดขาด · flatten fail-loud ยังครบ · WAL ro-read ข้าม process ทดสอบจริง ผ่าน

## สิ่งที่ค้าง (เปิดงานถัดไป — ไม่อยู่ใน scope นี้)
- **engine state-sync** (ไม้จริง 3 ใบโดน SL ปิดที่โบรกเกอร์ระหว่าง position check พัง → rows ค้าง
  is_open=1 → ladder บล็อก) — Hermes ซ่อม rows ด้วย balance-delta แล้ว แต่ต้นเหตุยังอยู่
- หลัง deploy: เฝ้าดู hourly line + ยืนยัน "stream has been closed" ไม่กลับมา — ถ้ากลับมาแปลว่า
  tick_mid polling ที่เหลืออยู่ก็รบกวน handle ด้วย → เปิดงานทำ tick เป็น sparse/optional
