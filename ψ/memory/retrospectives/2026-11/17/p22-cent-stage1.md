# Retro: P22 Cent Stage 1 — engine-p1/p2/p3 (สั่ง order ไม่ออก)

**วันที่**: 2026-11-17 (งานสั่งผ่าน Hermes 16/11 — งานที่ 2 ต่อจาก kill switch)
**สาขา**: mr-bet-bcd · **ไฟล์หลัก**: docker-compose.vps.yml · tests/test_p22_cent_deploy_causal.py · scripts/verify_deploy.py

## ทำอะไร

เปลี่ยน engine-p1/p2/p3 จาก variant กระดาษยุคก.ย. (scalp+ML ensemble คอร์สเก่า) เป็น P22 champion
มิเรอร์ demo C (mr-bet-p22-C) บนบช. Exness Standard Cent เงินจริง ladder $50/$20/$10 —
ขั้นแรก `DRY_RUN=1` hard-code (คิด lot จริง สั่งจริงไม่ออก) เพื่อเก็บหลักฐาน USC sizing ก่อนเปิดจริง

## ข้อค้นพบหลัก — หน่วย USC (จุดอันตรายที่สุดตามโจทย์)

พิสูจน์จากโค้ดจริง 3 ชั้น (ไม่ใช่ speculation):

1. **Sizing ปลอดภัยเอง** — bridge คืน equity เป็น USC (client.py get_account_info ไม่มี conversion)
   แต่สูตร `lots = equity×risk / (SL×100)` คิดในสกุลเดียวกับ PnL ของ broker (USC, contract 100)
   → equity 5000 USC × 0.5% = 25 USC ($0.25) → lot 0.04 ที่ SL $6 — ตรงช่วงที่คุณหมออนุมัติ
   (P1 0.01–0.05 / P2 0.01–0.02 / P3 0.01) · TradeBlocker hard cap 0.05 กันเลยช่วงตอน ATR ต่ำ
2. **จุดที่พังจริง = baseline หน่วย** — DD account-level เทียบ `INITIAL_EQUITY` กับ equity จาก bridge
   ตรงๆ ถ้าตั้งตามตัวอักษรโจทย์ (USD 50/20/10) เส้น 20% จะตายเงียบ (5000 USC ไม่มีวันแตะ $40)
   → เลือกตั้ง USC 5000/2000/1000 ตามเจตนาของคำสั่ง ("ให้ CB/DD baseline ถูก") และพิสูจน์สองทาง
   ด้วย causal test: USC baseline บล็อกที่ 4000 ✓ / USD baseline ไม่บล็อกแม้ −90% จริง ✗
3. **EQUITY_PER_POSITION** — ใช้ใน swing path จริง (ไม่ใช่แค่ m5_scalp) แต่กับ cap=1 สูตร
   `min(1, max(1, floor(eq/epp)))` = 1 เสมอแม้ทุนเหลือ $0.50 → inert พิสูจน์จาก method จริง
   ทั้งค่าใหม่ (USC) และ default เดิม 200

บทเรียนเพิ่ม: P3 ($10) กระทบ min-lot floor — raw lot 0.008 ที่ SL $6 → ปัดขึ้น 0.01 →
เสี่ยงจริงต่อไม้ ~0.6% (เกิน 0.5% เล็กน้อยตามกลไก floor ของ broker ทุกเจ้า) — จดไว้ใน compose comment

## สิ่งที่ต้องจำ (กันหลง)

- `DRY_RUN=1` เป็น hard-code ไม่ใช่ interpolation — การ arm จริงขั้น 2 = แก้ compose 1 บรรทัด
  (จงใจ: ให้ .env เก่า DRY_RUN_PORTFOLIO=0 ชนะไม่ได้ — คลาสบทเรียน stale-.env)
- RANGING_HARD_BLOCK ต้อง UNSET บน P22 (MR เข้าตอน ranging) — verify_deploy บังคับเป็น
  expected-UNSET แล้ว
- seeding ใน oracle_runner เป็น idempotent-skip — DB เดิมบน VPS baseline_balance ยังเป็น 100
  ต้อง UPDATE ด้วย SQL ใน runbook ก่อน start (อยู่ใน handoff)
- verify_deploy สำหรับ P-account ต้องมี DB_PATH env — db.py default เป็น data/oracle.db
  ซึ่งเป็นไฟล์ผิดใน P engine (แก้แล้ว: verify_account อ่าน DB_PATH env)

## ตัวเลข

- ไฟล์ test ใหม่: tests/test_p22_cent_deploy_causal.py — 43 tests ผ่าน
  (compose contract 6 + object knobs 9 + sizing 12 + baseline 2 + epp 13 + equity path 1)
- full suite จริง: **1500 passed, 2 skipped, 0 failed** (692.80s) — baseline 1401 + 99 ตัวใหม่ ไม่มี failure เพิ่ม
- verify_deploy p22-cent พิสูจน์สองทาง: env ตรง contract → exit 0 "all preset
  expectations met" · env ผิด (RR 3.0 + RANGING_HARD_BLOCK=1) → exit 1 จับครบทั้งคู่

## อะไรค้าง

- การพิสูจน์ runtime จริง (balance 5000 USC จาก bridge จริง, "Resolved symbol: XAUUSDc")
  = ขั้น VPS ใน runbook หลังคุณหมออนุมัติ start — งานนี้ห้าม deploy ตาม IRON
- สมมติฐานที่ยังไม่ได้พิสูจน์สด: XAUUSDc contract_size=100 ใน USC ตามมาตรฐาน Exness cent —
  runbook มีคำสั่งตรวจสดก่อนเปิดจริง
