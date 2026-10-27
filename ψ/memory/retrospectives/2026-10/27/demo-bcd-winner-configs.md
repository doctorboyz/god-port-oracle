---
date: 2026-10-27
type: retro
scope: demo B/C/D เปลี่ยนเป็น winner 3 ตัวจากฟาร์ม (B=P21 TS24 / C=P22 trailing / D=P16 TS24) + verify จาก object จริง
commits: (ดู git log — feat: demo B/C/D winner clones)
---

# Demo B/C/D = winner clones จาก selection รอบแรก

## เหตุผล (โจทย์ ada-task-20261027-demo-bcd-winners)

mr-bet TS12 เดิมแพ้จริงบน spread จริง (B -37.65 / C -26.19 / D -59.59,
รวม -123$) ขณะที่ฟาร์มกระดาษพิสูจน์ว่า TS12 หั่นไม้ชนะ — คุณหมออนุมัติ
ให้เปลี่ยนทั้งสามใบเป็น winner รอบแรก เพื่อวัด PF บนต้นทุนจริง (ช่องว่าง
paper vs real) · จังหวะปลอดภัย: 0 ไม้ open

## สิ่งที่ทำ

1. **compose VPS**: winner matrix hard-code ทั้งก้อน (ไม่ใช่ `${VAR:-...}`)
   เพราะ host `.env` มี knob เก่าค้าง (RR_RATIO_*=3.0 · MIN_CONFIDENCE_*=
   0.45/0.60 · ATR_MULTIPLIER_C/D=2.5) ที่ interpolation ให้ชนะ default เสมอ
   — คลาสบทเรียนเดียวกับ ML_FILTER_ENABLED (2026-09-21) · strategy_id ใหม่
   ทั้งสามใบ แยกสถิติจากไม้แพ้เก่า
2. **verify_deploy.py**: เพิ่ม `--preset winners` (fail-closed ตาม ISSUE-089)
   + อ่าน `mr_boll_threshold` จาก object + เช็ค hour gate จาก env ใน
   container (entry_hour_gate อ่าน env ตอน call time ไม่ bake ใน object)
3. **Redeploy**: recreate เฉพาะ oracle-engine-train ×2 (รอบสองเพื่อแก้
   ATR_D ตามคำตัดสินคุณหมอ) — mt5b/c/d + Real-A ไม่โดน recreate
   (start-time 10-16 เท่าเดิม)
4. **Verify จาก object จริง**: exit 0 ครบ B/C/D · bridge 3/3 ตอบ
   (healthy + engine login ผ่าน bridge ครบ) · cycle แรกไม่มี error

## การตัดสินใจสำคัญ + หลักฐาน

- **ATR_D 2.0 vs 2.5**: ผมวัดจาก container oracle-engine-farm1 จริงได้
  `ATR_MULTIPLIER_P16=2.0` (P16 = B-TS24 ไม่เคยมี ATR override; ATR2.5
  คือ P15 — PLAN-3PHASES ที่เขียน "P16(TS24+ATR2.5)" ผิด) deploy รอบแรก
  ใช้ 2.0 ตาม winner จริง → คุณหมอตัดสินผ่าน Hermes ให้เป็น 2.5 ตาม spec
  → recreate รอบสองเป็น 2.5 · **ตอนเทียบผล paper-vs-real ของ D ต้องจำ
  delta นี้** (D ไม่ใช่ clone ที่แท้ของ P16 ที่วัดในฟาร์ม)
- **risk_per_trade D = 1%**: object จริงอ่านได้ 0.01 จาก override
  `RISK_PER_TRADE_D=0.01` ใน host `.env` (ยุค slow-but-sure) ขัดกับ
  mr-bet flat 0.5% ที่ compose ตั้งใจ — คุณหมอสั่งแก้เฉพาะ ATR_D
  ("อย่าเปลี่ยนอย่างอื่น") จึงคงไว้ + ถอดออกจาก expectation ของ preset
  winners (ไม่ใช่ knob ของ matrix นี้) → **ค้างตัดสินใจ** ดู handoff
- **M5_SCALP_ENABLED=1 ใน host .env**: override default 0 ใน compose
  (คอมเมนต์ 2026-09-21 บอกปิดถาวร) — สภาพเดิมก่อนงานนี้ ไม่แตะ · flag ใน handoff

## บทเรียน / lessons

1. **"Sync กับ farm เป๊ะ" ต้องอ่านจาก container จริง ไม่ใช่จาก memo** —
   ทั้งโจทย์และ PLAN-3PHASES เขียน P16 ATR2.5 ตายกัน แต่ object จริง
   บอก 2.0 · อย่าเชื่อเอกสารสรุปเหนือ artifact (ISSUE-089 ไม่ใช่แค่
   env echo — รวมถึง plan memo ด้วย)
2. **Hard-code จุดที่ `.env` เคยกัด** — ถ้า knob เคยถูก override เงียบ
   มาก่อน (RR 3.0 ค้างตั้งแต่ยุคก่อน mr-bet) การแก้ default ใน compose
   อย่างเดียวไม่พอ ต้องถอด interpolation ไม่งั้นค่าเก่าชนะโดยไม่มีใครเห็น
3. **Verify ต้อง assert แค่สิ่งที่ spec กำหนด** — ตอนแรกใส่ risk_per_trade
   0.005 เข้าไปใน preset ตามความเชื่อของตัวเอง เจอ D=0.01 จริง —
   ค่าที่ "ไม่อยู่ใน matrix ที่อนุมัติ" ควรเป็น informational ไม่ใช่
   assertion ที่ทำให้ deploy แดงทั้งที่ spec ผ่าน

## ค้าง / next

- **D ติด permanent drawdown block** (equity 296.09 ≤ 302.32 = 15% จาก
  baseline 355.67) — สภาพเดิมก่อน deploy นี้ · D จะไม่เปิดไม้ใหม่จนกว่า
  baseline จะถูกรีเซ็ต (มี precedent: INITIAL_EQUITY_D reset 2026-09-23
  อนุมัติโดยคุณหมอ) — **ต้องการคำอนุมัติคุณหมอ** ก่อน D จะมีข้อมูล
  winner clone จริง
- รอไม้แรกใน DB ใช้ strategy_id `mr-bet-p21-B` / `mr-bet-p22-C` /
  `mr-bet-p16-D` แล้วเทียบ per-signal กับฟาร์ม (วิธีอ่านผลใน handoff)