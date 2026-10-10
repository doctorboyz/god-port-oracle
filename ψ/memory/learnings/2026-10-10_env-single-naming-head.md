---
name: env-single-naming-head
description: env config หนึ่งความหมายต้องมีชื่อเดียวและผู้อ่านเดียว — ชื่อสองหัว (MT5_LEVERAGE_ vs LEVERAGE_) คือ no-op เงียบที่จับไม่ได้ด้วย error แต่จับได้ด้วย guard test สองทิศ
metadata:
  type: lesson
  date: 2026-10-10
  issues: [ISSUE-108]
---

# env หนึ่งความหมาย = ชื่อเดียว ผู้อ่านเดียว — no-op เงียบจับได้ที่ guard test

**Symptom (จาก log จริง ไม่ใช่ speculation)**

compose ตั้ง `LEVERAGE_P11..P20=2000` (ชื่อที่ registry อ่าน) แต่
`live_trader.py` กับ `m5_scalp_trader.py` อ่าน `MT5_LEVERAGE_{account}` —
ชื่อที่ไม่มีใครตั้งเลย → ทุก margin check คิดที่ 1:100 default นิ่งๆ
→ `trade_blocker:margin_safety` บล็อกสัญญาณจริง conf=0.65 **310 ครั้ง**
(5-9 ต.ค. 2026) ที่ผ่าน gate อื่นทั้งหมด ไม้เปิดต่อวันตกจาก 12 → 1-2

**กติกา**

1. **ความหมายเดียว = helper เดียว = ชื่อเดียว**: leverage ทั้งระบบ (registry,
   margin gate entry, margin gate scale-in, scalp gate) ต้องมาจาก
   `get_account_leverage()` ใน account_registry ทั้งหมด — ห้ามมี os.environ.get
   inline ครั้งที่สองของความหมายเดียวกัน เพราะหัวที่สองคือที่ drift
2. **Guard สองทิศ** (`tests/test_env_coverage_guard.py`):
   - compose→code: ทุก env ที่เขียนลง container ต้องมีผู้อ่าน
   - code→config: ทุก per-account read ต้องมีสมาชิกตระกูลถูก set จริง หรืออยู่
     ใน allowlist พร้อมเหตุผล — ทิศนี้แหละที่จับ leverage bug ได้ (ทิศแรกจับไม่ได้
     เพราะ `LEVERAGE_P*` มีผู้อ่านจริง: registry)
3. **Default ของ env ต้องตรงความจริง ไม่ใช่แค่มีค่าพอใช้**: default 1:100 ที่ไม่
   ตรง leverage บช.จริง ทำให้ระบบ "ทำงานปกติ" แต่ตัดสินใจผิดทุกรอบ
4. **แก้ตาม stack trace ไม่พอ — ต้องสแกนทั้ง corpus**: จุดที่สาม
   (m5_scalp_trader) ไม่ได้อยู่ในรายงาน จะเจอได้จากสแกนเชิงระบบเท่านั้น

**การนำไปใช้**: ทุกครั้งเพิ่ม env knob ใหม่ → รัน
`tests/test_env_coverage_guard.py` (มันจะ FAIL จนกว่าจะ set จริงหรือ allowlist
พร้อมเหตุผล) · คลาสเดียวกันที่ยังค้าง: [[ISSUE-108]] portfolio_manager
TELEGRAM_* vs compose TG_* (รอคำตัดสินคุณหมอ)

**เชื่อมโยง**: [[paper-farm-must-mirror-live-frictions]] (margin ก็เป็น friction
ที่ต้อง mirror ให้ตรงจริง) · [[investment-config]] (leverage 1:2000 ทางการ)
· แผนหลัก [[three-phase-plan]]