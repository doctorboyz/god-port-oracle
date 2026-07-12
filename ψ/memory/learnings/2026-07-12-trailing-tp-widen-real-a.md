---
name: 2026-07-12-trailing-tp-widen-real-a
description: Fix #1 — trailing TP บีบ winner ก่อถึง 2.5R TP, ขยาย arm/trail ผ่าน env เฉพาะ Real-A
metadata:
  type: project
---

# Fix #1: Widen Trailing TP for Real-A (2026-07-12)

## ปัญหา (Symptom)
หลัง deploy commit f393b0c (3 แก้) เมื่อ 2026-07-09 22:10:
- equity $374.25 → $351.92 (-6% ใน 49h)
- win:loss = 0.82:1 (winner $6.79, loser $8.24) — **invert** design RR_RATIO=2.5:1
- 2 SELL ติดซ้อนกันโดน SL (-$12.16)
- 49h ไม่มี trade ใหม่ (ranging block + 1 rejection 10018=MARKET_CLOSED)

## สาเหตุ (Root Cause)
Trailing TP defaults บีบ winner ก่อนถึง 2.5R TP:
- `trailing_activation_pct = 0.20%` → arm หลัง MFE ~$8 (entry $4100)
- `trailing_trail_pct = 0.10%` → exit หาก pullback $4 จาก peak (noise ปกติก็เกิน)

ผล: winner ออกที่ trailing_tp ~$10.89 แทน take_profit $30 (2.5R) → win:loss invert
ในขณะที่ loser โดน SL เต็ม $12 → asymmetry กลับด้าน

**Why:** ค่า default ออกแบบสำหรับ noise น้อย ไม่เหมาะกับ XAUUSD M5 ที่ pullback ปกติ $4-8
**How to apply:** เวลาปรับ trailing TP ต้องดูที่ typical MFE กับ typical pullback ของ symbol — arm ต้อง > typical pullback, trail ต้อง > noise floor

## Causal Proof (CPT)
`tests/test_trailing_tp_widen_causal.py` — 8 tests GREEN
- BUY_PATH = [4105, 4115, 4110, 4120, 4115, 4130], entry=4100, SL=4088, TP=4130 (RR=2.5)
- Tight (0.20/0.10) → exits trailing_tp ~$10.89 (choke)
- Wide (0.40/0.20) → exits take_profit $30 (2.5R achieved)
- A/B: wide gain > 2x tight gain บน path เดียวกัน → พิสูจน์ cause
- Env override: TRAILING_ACTIVATION_PCT_A / _TRAIL_PCT_A override default per account

## แก้ (Fix)
Env-configurable trailing TP per account (commit `d7c6f45`):
- `metty/execution/live_trader.py`: per_account_trail_act / _trail dicts + fallback chain (TRAILING_*_A → TRAILING_* → default 0.20/0.10)
- `metty/execution/m5_scalp_trader.py`: parity hook
- `docker-compose.vps.yml` (oracle-engine = Real-A only):
  - `TRAILING_ACTIVATION_PCT_A=0.40` (arm หลัง MFE ~$16)
  - `TRAILING_TRAIL_PCT_A=0.20` (exit หาก pullback $8 จาก peak — survives normal noise)
- B/C/D (oracle-engine-train) เก็บ default 0.20/0.10 — observation เฉพาะ Real-A

## Deploy
- IRON LAW confirmed: "I AM DEPLOYING TO REAL A"
- `bash scripts/deploy-vps.sh start oracle-engine`
- ML smoke test ผ่าน (V4 model, 10 models, all predict OK)
- env verified ใน container: arm=0.40, trail=0.20 (ทั้ง LiveTrader + M5ScalpTrader)
- equity $351.92 คงที่ก่อน/หลัง deploy (no position drift)

## แล้วไง (Next)
- เก็บข้อมูลต่อ 24-48h: เทียบ win:loss ก่อน/หลัง (target กลับเข้าใกล้ 2.5:1)
- ดูว่า trailing ใหม่ arm ที่ $16 MFE — winner ได้ถึง 2.5R TP ไหม
- ระวัง: ถ้า market ดิ่งรุนแรง อาจเสีย profit บางส่วนกลับ (trail หลวม = risk กลับ)
- ห้ามแตะ B/C/D จนกว่า Real-A จะยืนยัน win:loss ดีขึ้น
- หาก 48h ไม่ดีขึ้น → ลอง arm 0.60 / trail 0.30 (ค่อยๆ ขยาย ไม่กระโดด)

## ข้อ 2-7 ที่ยังไม่แก้ (รอสังเกตผลข้อ 1 ก่อน)
2. Reversal gate ปิด SELL กลาง trend (เสีย opportunity ไม่ใช่ cause ขาดทุน)
3. ML threshold 0.55 อาจสูงเกิน (ปล่อย low-confidence trade ผ่านในบาง regime)
4. Ranging block 49h อาจคุมแน่นเกิน (รอ trend ชัดเกินไป)
5. ATR multiplier 1.5 อาจเล็กไป (SL แนบ entry → โดน noise)
6. PARTIAL_TP_ENABLED=0 (ไม่ lock profit ระหว่างทาง)
7. ML model V4 อาจ stale (ตลาดเปลี่ยน regime)

Related: [[2026-07-12-real-a-post-deploy-3fixes-check]]