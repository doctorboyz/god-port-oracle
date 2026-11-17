# Retro: Arm P2+P3 — flip DRY_RUN ladder ครบ (compose + contract)

**วันที่**: 2026-11-17
**สาขา**: mr-bet-bcd
**คำสั่ง**: คุณหมอผ่าน Hermes ("P2 and P3 ก็ต้องเทรดจริงด้วยนะ" — dispatch ทันที)

## ทำอะไร

ปลดล็อกบช.ที่เหลือของ ladder ตามสถาปัตยกรรม per-account ที่งาน P1
(commit `9d39b2e`) วางไว้ — flip `DRY_RUN=1`→`0` ของ
`oracle-engine-p2` และ `oracle-engine-p3` + contract ให้ตรง
ผลคือ ladder 3 บช. armed ครบพร้อมกัน: same strategy (P22 champion)
3 sizing regimes (5000/2000/1000 USC) เก็บผลจริงพร้อมกันตามเจตนาคุณหมอ

## ไฟล์ที่แตะ (เท่าที่จำเป็น)

1. `docker-compose.vps.yml` — 2 บรรทัดค่า (DRY_RUN=0 ของ P2/P3) +
   comment สถานะของทั้ง 3 block (P1 แก้ comment เดียว: ลบประโยค
   "P2/P3 stay DRY_RUN=1 (one account at a time)" ที่กลายเป็นโกหก
   ค่าของ P1 ไม่ถูกแตะ)
2. `tests/test_p22_cent_deploy_causal.py` — P22_EXPECT: P2/P3
   dry_run "1"→"0" + docstring/comment 4 จุดให้ตรงสถานะใหม่
   (โครงสร้าง per-account มีอยู่แล้วจากงาน P1 — แก้ค่าใน spec เดียว
   ทั้ง knob_matrix และ per-account test ขยับตามเอง)
3. `scripts/verify_deploy.py` — preset p22-cent: expected dry_run
   P2/P3 "1"→"0" + comment 2 จุด

## สำรวจ expectation DRY_RUN อื่นทั้ง repo (ข้อ 4 ของงาน)

- ที่ผูกกับ "P2/P3=1" มีแค่ 2 ไฟล์ที่แก้ไปแล้ว
- `test_verify_deploy_risk_causal.py` — grep dry/DRY = 0 matches,
  ไม่มี expectation ซ่อน
- farm paper (`test_farm_group_mode_causal.py` / `paper_farm_apply.py`)
  — DRY_RUN=1 โดยเจตนา (paper farm ไม่ส่งไม้จริง) ไม่เกี่ยว P services
- `test_risk_env_override_causal.py:16,28` — docstring บันทึก
  observation ตอน stage 1 (ประวัติศาสตร์ ไม่ใช่ expectation) เว้นไว้
- `oracle_runner.py` / `deploy-vps.sh` — generic default=1
  (ห้ามแตะตามขอบเขตงาน) · KILL_SWITCH_DRY_RUN เป็นคนละ knob

## กติกาที่ยังล็อกครบ

- **ห้าม `${...}` บน DRY_RUN** ครบทุกบช. — host .env stale ชนะไม่ได้
  ทั้ง arm ทั้ง disarm (ไม่เปลี่ยนจากงาน P1)
- engine/train เดิม `DRY_RUN=${DRY_RUN:-1}` ไม่ถูกแตะ (ยืนยันด้วย
  grep หลัง restore: บรรทัด 129/241 เป็น interpolation เหมือนเดิม)
- knob อื่นทั้งหมดคงเดิมเป๊ะ · ไม่แตะ metty/ broky/ shared/
  docker/mt5/ farm_variants.yml oracle_runner.py kill_switch.py
- ไม่มีอะไรถูก deploy/start (repo เท่านั้น — Hermes เป็นคน deploy)

## หลักฐาน

- **RED proof contract ล็อกจริง**: python flip P2/P3 กลับ DRY_RUN=1
  ชั่วคราว (เฉพาะ 2 service block ไม่โดน P1) → แดงพอดี 2 ตัวที่ต้องแดง
  (`test_p22_knob_matrix` + `test_dry_run_state_per_account`) →
  คืนค่า → เขียว 43/43
- full suite (รอบแรก ไปโดน time bomb ข้างบน): 1690 passed 2 skipped
  **1 failed** (test_bet_conditional_cells 17:11 UTC จริง = หลัง 06:25
  พอดี) · หลังแก้ time bomb: **1691 passed 2 skipped 0 failed**
  694.31s EXIT=0 (ตรง baseline 1691 ของ 9d39b2e เป๊ะ)

## ขยายขอบเขตแจ้งชัด: time bomb ใน test_bet_conditional_cells (test-only)

รอบแรกของ full suite ได้ **1 failed**: `TestTailAnchorBucketAware::
test_flat_gate_opens_bet_at_any_clock[10-0]` — พิสูจน์ด้วย `git stash`
ว่า **แดงบน HEAD สะอาดด้วย** = มีอยู่ก่อนงานนี้ ไม่ใช่สิ่งที่งานนี้ทำ

- **Root cause จากโค้ดจริง**: test ปัก `_now` (ฝั่งสร้างเฟรม) เป็นเวลา
  ตายตัว 04:10 UTC 17/11 แต่ stale-feed guard ใน `bet_engine` (บรรทัด
  ~353) อ่าน **นาฬิกาจริง** ผ่าน `_now_utc()` ที่ไม่โดน patch → เฟรม
  พารามิเตอร์ (10,0) จบ 05:50 → หลัง 06:25 UTC จริง guard ปิดไม้ →
  **แดงถาวร** (วันที่ fake ตายตัว — ไม่ใช่หน้าต่างเวลาแต่เป็นระเบิดเวลา:
  suite ที่ผ่านมาทั้งวันเชียวๆ เพราะรันก่อน 06:25) · พารามิเตอร์ (200,0)
  รอดเพราะ anchor ยืดเฟรมพ้น 08:00 → age ติดลบ
- **ครอบครัวเดียวกับ f0a640d / f6f9a3f — leak ที่ 3**: สองรอบก่อนปัก
  นาฬิกาฝั่งเฟรม แต่นาฬิกาฝั่ง engine ยังเดินจริง
- **แก้ test-only 2 บรรทัด** (+comment): patch `bet_engine._now_utc`
  ให้คืนค่า fake now เดียวกัน — guard เห็นเฟรมสดทุก parametrize point
  ไม่แตะ production แม้บรรทัดเดียว
- **Causal proof**: ถอด patch ชั่วคราว → แดงพอดี (10,0) → คืนค่า →
  เขียว 52/52 ทั้งไฟล์
- ขยายขอบเขตเพราะเกณฑ์ "suite ผ่านทั้งชุด" ถูกสิ่งนี้ขวาง (แบบเดียว
  กับที่ f0a640d เคยทำและแจ้งไว้) — bet engine/production ไม่ถูกแตะ

## บทเรียน

1. **สถาปัตยกรรม spec จุดเดียว (P22_EXPECT) จ่ายดอกเบี้ยครั้งที่สอง** —
   งาน P1 จ่ายแรกตอนสร้าง per-account structure, งานนี้แก้ค่าใน
   spec ตำแหน่งเดียว + comment ทั้ง contract ขยับเอง = arm ladder
   ต่ออีก 2 บช. ด้วย diff เล็กมากและไม่มีโอกาส drift ไฟล์↔runtime
2. **comment สถานะคือสัญญาเหมือนค่า** — flip ค่าอย่างเดียวแล้วทิ้ง
   comment "orders NOT sent" / "stay DRY_RUN=1" ไว้คือปล่อยให้คนอ่าน
   คนถัดไป (รวมถึง agent) เชื่อผิด — แก้พร้อมกันทุกครั้ง
3. ladder 3 บช. พร้อมกัน = ตรวจไม้แรก 3 ชุด — ตาราง approval windows
   ต้องแยกต่อบช. (P1 0.01–0.05 / P2 0.01–0.02 / P3 0.01) อยู่ใน
   handoff แล้ว อย่า merge เป็นช่วงเดียว
4. **ปักนาฬิกา test ต้องปักทุกนาฬิกาที่ path นั้นอ่าน** — ครอบครัว
   wall-clock ของ test_bet_conditional_cells รั่ว 3 ครั้งใน 2 งาน
   เพราะแต่ละครั้งปักแค่นาฬิกาฝั่งเฟรม ลืมว่า engine มี _now_utc
   ของตัวเอง — ครั้งต่อไปที่ monkeypatch เวลาใน path ที่มี engine
   call: grep ทุก `now(` ใน path นั้นก่อนถือว่าเสร็จ

## ค้าง / ส่งต่อ

- Handoff: `ψ/outbox/p23_arm_compose_handoff_2026-11-17.md`
- หลัง Hermes deploy: `docker inspect` P1/P2/P3 ต้องเห็น
  DRY_RUN=0 ครบทั้งสาม · ตรวจ lot ไม้แรกตามช่วงอนุมัติต่อบช.
- เกิน hard cap 0.05 บช.ใด = เปิดงานทันที
