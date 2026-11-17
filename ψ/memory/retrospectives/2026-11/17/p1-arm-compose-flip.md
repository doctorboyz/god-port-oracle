# Retro: Arm ขั้น 2 — flip DRY_RUN P1 live (compose + contract)

**วันที่**: 2026-11-17
**สาขา**: mr-bet-bcd
**คำสั่ง**: คุณหมอผ่าน Hermes ("ต้องการเทรดจริง เพื่อเก็บผลลัพธ์จริง" — dispatch ทันที)

## ทำอะไร

Arm ขั้น 2 บช.แรกตามสัญญา stage 1 ที่บันทึกไว้ใน commit d2464ac:
"แก้ compose 1 บรรทัดหลัง kill switch deploy + คุณหมออนุมัติ"
วันนี้ครบทั้ง 3 เงื่อนไข (kill switch deployed + sizing proven ที่ runtime
object จริง risk=0.005 + คุณหมอ go) จึง flip `DRY_RUN=1`→`0`
เฉพาะ `oracle-engine-p1` — P2/P3 ยัง DRY_RUN=1 ตามแผน
"one account at a time, P1 first"

## ไฟล์ที่แตะ (เท่าที่จำเป็น)

1. `docker-compose.vps.yml` — บรรทัดค่าเดียวของ P1 (`DRY_RUN=0`) +
   ปรับ comment ให้ตรงสถานะใหม่ (comment เดิมบอก "orders computed
   but NOT sent" — ทิ้งไว้บนบช. live = โกหก)
2. `tests/test_p22_cent_deploy_causal.py` — contract เปลี่ยนจาก
   "DRY_RUN=1 ทุกเครื่อง" เป็น **per-account** ใน P22_EXPECT
   (P1="0" / P2="1" / P3="1"): ถอด DRY_RUN ออกจาก P22_GLOBALS,
   ฉีดใน `_expected_env` ตาม spec, test เดิม
   `test_dry_run_stage1_every_engine` → `test_dry_run_state_per_account`
   เทียบ expected ตามบช.
3. `scripts/verify_deploy.py` — preset p22-cent: ถอด `dry_run: "1"`
   ออกจาก `_P22_CENT_COMMON` ย้ายมาเป็น per-account ใน loop
   (P1="0" / P2/P3="1")

## กติกาที่ยังล็อกครบ

- **ห้าม `${...}` บน DRY_RUN** ยังอยู่ใน `_NO_INTERPOLATION_KEYS`
  ครบทุกบช. — host .env stale ชนะไม่ได้ทั้ง arm ทั้ง disarm
- knob อื่นทั้งหมดคงเดิมเป๊ะ · ไม่แตะ metty/ broky/ shared/
  docker/mt5/ farm_variants.yml oracle_runner.py kill_switch.py
- ไม่มีอะไรถูก deploy/start (repo เท่านั้น — Hermes เป็นคน deploy)

## หลักฐาน

- **RED proof contract จริง**: sed พลิก compose กลับ DRY_RUN=1
  ชั่วคราว → test แดงพอดี 2 ตัวที่ต้องแดง
  (`test_p22_knob_matrix` + `test_dry_run_state_per_account`) →
  คืนค่า → เขียว 43/43 = contract ล็อกจริง ไม่ใช่เขียวเพราะเผื่อ
- **สำรวจ DRY_RUN expectation อื่นทั้ง repo**: ที่เกี่ยว P services
  มีแค่ 2 ไฟล์ที่แก้ไปแล้ว — `test_verify_deploy_risk_causal.py`
  ใช้ preset **P2** (ไม่เปลี่ยน) และ env-only assertion อยู่ใน
  `main()` ไม่โดน path เทส นั้น / farm + kill_switch เป็นคนละ knob
  (DRY_RUN_PORTFOLIO / KILL_SWITCH_DRY_RUN) ไม่เกี่ยว
- full suite: ดูรายงานงานนี้

## บทเรียน

1. **Contract ที่ drive จาก spec เดียว (P22_EXPECT) ทำให้ arm ง่ายและ
   ปลอดภัย** — เปลี่ยนสถานะ 1 ที่ spec, ทั้ง compose check และ
   runtime-object check ขยับตามพร้อมกัน ไฟล์กับ runtime ไม่ drift
2. **comment สถานะ (stage 1/2) ต้องขยับพร้อมค่า** — ถ้า flip ค่าอย่าง
   เดียวแล้วทิ้ง comment "orders NOT sent" ไว้ คนอ่านคนถัดไป
   (รวมถึง agent) จะเชื่อผิด
3. ลำดับที่บัญญัติไว้: **kill-switch armed ก่อน flip เสมอ** —
   Hermes ทำฝั่ง VPS คู่ขนาน, repo flip รอเวลา deploy

## ค้าง / ส่งต่อ

- Handoff: `ψ/outbox/p1_arm_compose_handoff_2026-11-17.md`
- หลัง Hermes deploy: `docker inspect oracle-engine-p1` ต้องเห็น
  `DRY_RUN=0` + ไม้แรกจริงของ P1 lot ต้องอยู่ 0.01–0.02
- P2/P3 ยัง DRY_RUN=1 — รอผล P1 ก่อน arm บช.ถัดไป
