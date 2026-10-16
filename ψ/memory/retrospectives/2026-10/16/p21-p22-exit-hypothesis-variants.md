---
date: 2026-10-16
type: retro
scope: farm P21/P22 — exit hypothesis variants (ยืด time stop + trailing TP)
commits: (ด้านล่าง)
---

# P21/P22: ทดสอบสมมติฐาน exit ของ Marks

## เหตุผล (ตัวเลขจาก Marks รอบ 16/10)

ไม้ชนะโดน time stop 12 bars (1 ชม.) หั่นกลางทาง:
**max_holding 81 ไม้ จบ -86.8$ (เฉลี่ย -1.07/ไม้)** ขณะที่ TP จบเฉลี่ย
**+8.17/ไม้** และ SL แค่ 6 ครั้ง → สัญญาณเข้าดีแต่ exit ตัดไม้กำไรก่อนถึง TP

สมมติฐานสองทาง ทดสอบด้วย variants ใหม่บนฐาน P20 (baseline ดีสุด:
+22.03$ / PF 1.66 / WR 56% ในยุคสะอาด):

- **P21** = P20 + `TIME_STOP_BARS: 24` — ยืดเวลา 1→2 ชม. ให้ไม้มีโอกาส
  วิ่งถึง TP ก่อนโดนหั่น
- **P22** = P20 + `TRAILING_TP_ENABLED: 1` (time stop 12 คงเดิม) —
  ปล่อยกำไรวิ่งต่อหลัง activation แทนตายที่ระดับคงที่

Knobs อื่นทั้งหมด clone P20 เป๊ะ: RR 1.20, MR_BOLL 0.85, ENTRY_HOURS/
BLOCKED_HOURS ว่าง, MIN_CONFIDENCE 0.55, ATR 2.0, SL_CAP 12.0,
MR_COST_MULT 3.0 — ต่างจาก P20 ที่ knob เดียวพอให้ผลอ่านเจอง่าย

## ทำอะไร

1. append P21/P22 ลง farm_variants.yml + ชื่อเข้ากลุ่ม farm1
2. regenerate compose → diff ยืนยันเป็นการเติมเท่านั้น (P11-P20 knobs
   230 แถว identical ก่อน/หลัง deploy ใน container จริง)
3. `paper_farm_apply.py --up` → seed P21/P22 อัตโนมัติ (balance 100,
   running, leverage 2000)
4. ตรวจก่อน deploy: ทั้งสอง knob มีผู้อ่านจริง
   (`live_trader.py:322,327` — `TIME_STOP_BARS_{acct}` /
   `TRAILING_TP_ENABLED_{acct}`) กันคลาสบั๊ก env-name-mismatch
   (ISSUE-100/108)

## หลักฐาน

- `docker ps`: oracle-engine-farm1 Up healthy
- `docker inspect` ACCOUNTS: P11..P22 ครบ 12 ชื่อ
- DB farm1.db: 12 accounts — P21 (id 14), P22 (id 15) balance 100 /
  running; ประวัติ P11-P20 ไม้เดิมอยู่ครบ; open = 0 ทุกไม้ (deploy
  ตอนไม่มีไม้ค้าง = ไม่มี state ที่ต้อง reconcile)
- engine log: Trader:Demo-P21/P22 รันวงจร DRY-RUN ปกติ ไม่มี error

## ระวัง / ข้อจำกัด

- ช่วงนี้ตลาด trending (ADX 29.3 ≥ 20) → MR-only mode HOLD ทั้งก้อน
  ไม้แรกของ P21/P22 ต้องรอ entry window ถัดไปที่ตลาดกลับมา ranging
- trailing TP ของ P22 ใช้ activation/trail default (0.20/0.10) จาก
  `_per_name_knob_attrs` — ถ้าผลอ่านยาก รอบหน้าอาจต้องเพิ่ม
  TRAILING_ACTIVATION_PCT เป็น knob ตาม variant
- league table รอบถัดไปต้องมี 12 แถว — ถ้า P21/P22 หายเป็นสัญญาณว่า
  seed/regenerate มีปัญหา

## แล้วไง

รอ farm วิ่ง ≥ 1 สัปดาห์ (ไม้พอสถิติ ~15+ ต่อ variant) แล้วเทียบ
exit reason กับ P20: ถ้า max_holding ของ P21 กลายเป็นกำไรเฉลี่ยบวก
และ/หรือ trail ของ P22 เฉลี่ยต่อไม้สูงกว่า TP ตายตายของ P20 →
นำ knob ที่ชนะไป propose ให้ BCD จริงบน VPS (ผ่าน Hermes ตาม protocol)