---
name: investment-config
description:  investment config ทางการของระบบเทรด god-port (คุณหมอสั่งผ่าน Hermes 2026-10-10) — ประเภทบัญชี / เงินต้น / leverage 1:2000 / margin_safety 0.80 / EQUITY_PER_POSITION 200
metadata:
  type: config
  date: 2026-10-10
  source: Hermes relay MSG (คุณหมอวางแผนเอง 2026-10-10)
---

# Investment Config ทางการ — god-port trading

> สั่งโดยคุณหมอ (doctorboyz) ผ่าน Hermes 2026-10-10 · เป็น config ที่ถือว่าเป็นทางการ
> การเปลี่ยนแปลงใดๆ ต้องผ่านคุณหมอเท่านั้น

## 1. ประเภทบัญชีที่ใช้งาน

| กลุ่ม | สถานะ | หมายเหตุ |
|---|---|---|
| **Paper farm P11-P20** | ✅ ใช้งานหลัก | Mac mini, brokerless dry-run, feed GC=F CSV — ที่เก็บข้อมูลตัดสินกลยุทธ์ (league) |
| **Demo B/C/D** | ✅ ใช้งาน | VPS, ต่อ MT5 bridge จริง — ห้ามแตะโดยไม่ขออนุมัติ |
| **Cent P1-P3** | ❌ **พัง — ไม่ใช้** | ยอด $0, Exness ปิด trading บช.นี้ — ไม่ใช้จนกว่าจะฝากเงินและเปิดใช้ใหม่ (ถ้าฝากต้องแจ้งให้ sync อีกครั้ง) |
| Real-A | 🚫 นอกขอบเขต | ห้ามแตะเด็ดขาดตามกฎ scope |

## 2. เงินต้น

- **Paper farm: $100 / variant** (`INITIAL_BALANCE` / `INITIAL_EQUITY` = 100 ใน `farm_variants.yml`)
- ทุก variant เริ่มเท่ากันหมด เพื่อให้ league เทียบกันได้ยุติธรรม

## 3. Leverage = 1:2000

กำหนด `LEVERAGE_P11..P20 = 2000` (ตรงบช.จริง Cent Real25 ของ Exness):

- margin ต่อไม้ (lot 0.01, ทอง ~$4,220): 0.01 × 100oz × 4220 / 2000 = **~$2.11/ไม้**
  (หมายเหตุ: คุณหมอประเมินคร่าวๆ ไว้ $0.21 — ตัวเลขจริงตามสูตรคือ $2.11 ยังต่ำกว่า margin gate มากอยู่ดี)
- **ไม่มีปัญหา margin gate อีก** — ก่อนหน้านี้โค้ดอ่านชื่อ env ผิด (`MT5_LEVERAGE_*` ที่ไม่มีใครตั้ง)
  ทำให้คิด margin ที่ 1:100 = $42.20/ไม้ แล้ว `trade_blocker:margin_safety` บล็อกสัญญาณจริง
  ~306 ครั้ง (6-9 ต.ค.) — แก้แล้ว 2026-10-10 (commit ดูที่ HANDOFF / `tests/test_leverage_env_farm_causal.py`)

## 4. คงไว้ไม่แก้

- **`margin_safety_factor = 0.80`** — gate ยังต้องเดิน: บล็อกเมื่อ margin_required > 80% ของ free margin
- **`EQUITY_PER_POSITION = 200`** — ไว้สำหรับเฟสใช้จริง: ยกเป็น max_positions จริงเมื่อ equity โต
  (ปัจจุบัน farm ใช้ MAX_POSITIONS=1/CAP=1 ตาม farm_variants.yml)

## 5. การเชื่อมโยง

- Guard กัน no-op คลาสเดียวกัน: `tests/test_env_coverage_guard.py`
  (ทุก env ที่ compose เขียนต้องมีผู้อ่าน + ทุก per-account read ต้องมีครอบครัวถูก set จริง)
- แผนหลัก 3 เฟส: [[three-phase-plan]] `ψ/goals/active/three-phase-plan-2026-10.md`
- ผู้พบว่า Cent P1-P3 พัง + เจอ leverage no-op: Hermes (log จริง 2026-10-06..09)