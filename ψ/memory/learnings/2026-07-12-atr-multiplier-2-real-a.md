---
name: 2026-07-12-atr-multiplier-2-real-a
description: Fix #3 — ATR_MULTIPLIER_A 1.5 → 2.0 ป้อง SL โดน noise (tightest of all accounts)
metadata:
  type: project
---

# Fix #3: ATR Multiplier 1.5 → 2.0 for Real-A (2026-07-12)

## ปัญหา (Symptom)
Real-A มี SL tightest ของทุก account:
- A=1.5, B=2.0, C=2.5, D=2.5, forward-engine BEST=2.0
- SL distance = `atr × atr_multiplier + spread_buffer`
- @ ATR(M5)=$6, multiplier 1.5 → SL = $11; multiplier 2.0 → SL = $14 (+27%)
- @ ATR=$8 → $14 vs $18 (+29%)

07-10: 2 SELL trades โดน SL ที่ระยะ $11.45 — อยู่ใน noise range ปกติของ XAUUSD M5
แม้ทิศทางจะไม่ผิด (H4 chop จาก Fix #2 เป็นสาเหตุหลัก แต่ SL แนบก็ซ้ำเติม)

## สาเหตุ (Root Cause)
ATR_MULTIPLIER_A=1.5 ตั้งตอนทดลอง ไม่ได้สอดคล้องกับ accounts อื่นที่ proven:
- B=2.0 (demo, OOS PF 2.60)
- C/D=2.5 (demo, OOS PF 2.02-2.55)
- forward-engine BEST_CONFIG=2.0
- 1.5 = tightest = เสี่ยงโดน noise stop มากที่สุด

**Why:** SL ใกล้ entry เกินไป = โดน pullback ปกติทิ่มแทงก่อนจะวิ่งไปทางที่ถูก
**How to apply:** SL distance ต้อง > typical noise range ของ symbol/timeframe; XAUUSD M5 ATR ~$6-8, multiplier 2.0 = $14-18 SL = นอก noise floor

## แก้ (Fix)
Pure env tuning — env hook มีอยู่แล้วใน live_trader.py:189 + m5_scalp_trader.py:147
- `.env`: `ATR_MULTIPLIER_A=1.5` → `2.0`
- `docker-compose.vps.yml`: default `${ATR_MULTIPLIER_A:-2.0}` (was 1.5)
- commit `e2b1fXX` (Fix #3)
- ไม่ต้องแก้ code ไม่ต้อง CPT — env hook proven

**สำคัญ:** `.env` override สำคัญกว่า docker-compose default — ต้องแก้ทั้งสองที่
ถ้าแก้แค่ docker-compose ค่าใน .env จะชนะ → deploy ไม่เปลี่ยน (เจอตอน deploy #3)

## Verification (container)
- `ATR_MULTIPLIER_A=2.0` verified ใน container
- LiveTrader.risk.atr_multiplier = 2.0
- SL distance @ ATR=6: $14.0 (was $11.0, +27%)
- SL distance @ ATR=8: $18.0 (was $14.0, +29%)
- TP distance @ ATR=6, RR=2.5: $30.0 (was $24.25) — winner ใหญ่ขึ้นตาม
- equity $351.92 คงที่ก่อน/หลัง deploy, ML smoke ผ่าน

## Trade-off
- SL ห่างขึ้น = risk per trade คงที่ (position sizing ลด lots ตาม)
  - risk_per_trade=0.02, equity $351.92, SL $14 → lots = (351.92×0.02)/14 = 0.50 lots → cap 0.50
  - risk_per_trade=0.02, equity $351.92, SL $11 → lots = (351.92×0.02)/11 = 0.64 lots → cap 0.50
  - ในกรณีนี้ cap 0.50 จำกัดอยู่แล้ว → lots เท่าเดิม แต่ SL ห่างขึ้น = โอกาสรอด noise มากขึ้น
- TP ห่างขึ้นตาม RR=2.5 → winner ใหญ่ขึ้น (รวมกับ Fix #1 trailing TP หลวม = win:loss ดีขึ้น)
- ถ้า ATR ใหญ่มาก (volatile) SL อาจกว้างเกิน → drawdownProtector + ML filter คุมอยู่

## แล้วไง (Next)
- เก็บข้อมูลต่อ 24-48h: ดูว่า trade ใหม่โดน stop น้อยลง (target stop_rate < 50% จาก ~100%)
- ดู win:loss รวม Fix #1+#2+#3: ควรกลับเข้าใกล้ 2.5:1
- ระวัง: ถ้า 48h ไม่มี trade เลย → อาจจะ tight ที่อื่น (ranging block ข้อ 7) ไม่ใช่ ATR
- B/C/D ไม่แตะ — ใช้ค่าของตัวเองอยู่แล้ว

## ข้อ 4-8 ที่ยังไม่แก้
4. Market-hours guard (Sunday 10018 rejection)
5. PARTIAL_TP_ENABLED=0 (ไม่ lock profit ระหว่างทาง)
6. ML threshold 0.55 (ต้อง backtest เทียบ)
7. Ranging block 49h คุมแน่นเกิน (รอดู 72h)
8. ML V4 stale (ต้อง manual retrain + OOS verify)

Related: [[2026-07-12-real-a-post-deploy-3fixes-check]], [[2026-07-12-trailing-tp-widen-real-a]], [[2026-07-12-h4-closed-bar-only-real-a]]