# Retro — TF-Close Regime (T1-T5) · 2026-11-13

**งาน:** ทดสอบสมมติฐานคุณหมอ "TF regime ห่าง H4/D1 เกินไปจนไม่เกิดความสัมพันธ์กับ bet M5" → detector ใหม่บน M30/M15 (m30_ema, m15_ema, m30_adx) no-lookahead + forward-accuracy 1h/4h/24h/72h + conditional grid 2,016 cells + head-to-head กับ h1/h4 + ปรับเมนู + ข้อสรุป TF
**โจทย์:** ada-task-20261113-tf-close-regime.md (คุณหมอสั่งผ่าน Hermes 13/11)
**ผล:** T1-T5 เสร็จครบเกณฑ์ · regression 1,008 cells เดิม **PASS 0 mismatch** (fail-closed) · spot-check อิสระ 3 ค่าตรงเป๊ะ · full suite **1378 passed, 2 skipped** (baseline 1356 + tests ใหม่ 22 ตัว: 12 close-TF htm-causal + 6 forward-close-tf + 5 script)** (baseline 1356 + tests ใหม่ 23 ตัว: 12 close-TF htf + 6 forward-close-tf + 5 script)

## สิ่งที่ทำ

- **T1** `broky/betbracket/htf.py` — detector ใหม่ 3 ตัว additive ล้วน: m30_ema/m15_ema (EMA50 slope, **eps=0.2σ** ตาม design rule ของ h4_ema: σ_M30=3.67bp, σ_M15=2.56bp → FLAT ~23%) · m30_adx (ADX14≥25+EMA20, clone h1_adx) · ทดสอบ eps 2 ค่าให้ data เลือก: 0.5σ ทำ FLAT>50% (หิว n) และ forward hit ต่าง <1pp → เลือก 0.2σ · NEW_DETECTORS registry แยกจาก DETECTORS เดิม · `build_detector_states` รับ param (default สามตัวเดิม byte-identical) · causal test ผ่านทั้ง 3 ตัว (mutate forming M30/M15 bucket)
- **T2** `broky/betbracket/forward.py` — horizon **1h เพิ่ม** (1/4/24/72h) · package รับ detector set + horizons param (default เดิมเป๊ะ) · transition matrix บนแท่ง TF ของ detector ใหม่เอง
- **T3** resolve ครั้งเดียว แท็กทั้ง 6 detectors → **2,016 cells** (84 ledgers × 6 × 4 states) · regression เทียบรายงาน 13/11 ทุก field ทั้ง 1,008 cells เดิม · head-to-head สูตรแชมป์ (StochRev-SELL-TP10SL20 · RSIRev-SELL-TP12SL6 · EMACross-BUY-TP12SL6) × state × 5 detectors + ตาราง ΔWR
- **T4** เมนูใหม่ต่อ detector ใหม่×state (เกณฑ์คุณหมอ 12/11 ไม่ขยับ) + ป้ายแข็งแรงนับข้าม 6 detectors + **verdict compute จากตัวเลข** (กฎตายตัว: median ΔWR > +2pp และชนะ>แพ้ · hit ทิศ >55%) ไม่ใช่ความเห็น
- **T5** รายงาน `ψ/outbox/bet_tf_close_regime_2026-11-13.{md,json}` + handoff

## บั๊ก

1. **บั๊กจริงใน implementation หลัก: 0 ตัว** — regression 1,008/1,008 ตรงเป๊ะยืนยัน machinery เดิมไม่โดนแตะ
2. **บั๊กในตัวตรวจเอง 1 ตัว (ซ้ำบทเรียน 12/11):** spot-check มือรอบแรกกรอง `polarity == 'SELL'` ก่อนเดิน bar → n เหลือ **672 vs จริง 1,346 = ครึ่งพอดี** — bracket machinery เป็น theory-free เปิดทั้งสองขาทุก fire, SELL ledger รับทุก fire ไม่ใช่เฉพาะ fire ขาลง
   - **บทเรียนซ้ำครั้งที่ 2 (12/11 เคยโดนครั้งแรก):** ก่อน spot-check ต้องอ่าน contract ของ machinery ที่กำลังตรวจก่อนเสมอ — "สัญญาเปิดทั้งสองขา" เขียนอยู่ใน docstring ของ bracket.py แล้ว · สัญญาณเตือนที่ควรจับได้: n หารสองลงตัวพอดี = มีการกรอง polarity ไม่ใช่ความบังเอิญ
   - **แก้ที่ตัวตรวจ:** ตัด filter ออก ทุก fire เข้าทั้ง SELL ledger → ตรงเป๊ะ n/WR/E ทั้งสามค่า
3. **คาดเลขใน test ผิดเอง 2 จุดจาก 8 ที่ fail รอบแรก** (ประสบการณ์เดียวกับ retro 13/11): คาด weekend-gap lookup ผิดบาร์ (ลืมว่า anchor แรกปิด 21:00 ไม่ใช่ 22:00) · คาด baseline 50.000% เป๊ะทั้งที่ median ของ n คู่มี tie (tolerance เดิมของ suite คือ 0.01) — แก้ที่ความคาด ไม่ใช่ implementation

## ผลลัพธ์หลัก

### 1) คำตอบคุณหมอ: TF ใกล้ขึ้น edge **ไม่ได้**เข้มขึ้น

- **แยก edge เสมอ:** ΔWR ของ m15/m30/m30_adx เทียบ h4_ema ต่อสูตรแชมป์×state — median **−0.01pp** (27 ช่อง) · ชนะเกิน 2pp 5 ช่อง = แพ้เกิน 2pp 5 ช่อง · 17 ช่องอยู่ใน noise ±2pp
- **ทำนายทิศไม่ได้อีกเหมือนเดิม:** hit 1h/4h ของ detector ใหม่ทุกตัว 47–50% (สูงสุด 53.3%) — TF ใกล้ไม่ได้ซื้อการทำนายสั้น
- **ความหนาแน่นต่ำกว่า:** ผ่านเกณฑ์ h4_ema 52 · d1_structure 52 · h1_adx 47 vs m15_ema 45 · m30_adx 43 · m30_ema 42
- **ต้นทุนที่เพิ่มมาแถม:** state พลิกเร็วมาก — run มัธยฐาน m15 0.5–0.9 ชม. · m30 1–2.5 ชม. (vs H4 ระดับวัน) → เมนูพลิกบ่อยโดยไม่ได้ซื้อ edge คืน
- **สูตรแชมป์ใน state ที่ h4_ema แรงสุด:** StochRev-SELL-TP10SL20|FLAT h4_ema +9.42 ชนะ M30-EMA +7.32 · RSIRev-SELL-TP12SL6|FLAT h4_ema +19.92 ทิ้ง M30-ADX +1.88 · EMACross-BUY-TP12SL6|DOWN h1_adx +3.85 ชนะทุกตัว

### 2) ข้อสรุป TF สุดท้าย — เก็บ h4_ema เป็น regime gate

ความสัมพันธ์ระหว่าง regime กับผล bet ไม่ได้อยู่ที่ "ระยะ TF ใกล้ signal แค่ไหน" — มันอยู่ที่**โครงสร้างการจบไม้ต่อสภาพตลาด** (FLAT → SELL ชนะทุก geometry) ซึ่งเห็นเหมือนกันหมดทุก TF ที่ลอง · ยิ่ง TF ใกล้ ยิ่งได้ state ที่พลิกถี่แต่ไม่แยก edge ได้ดีกว่า · h4_ema ยังเป็น gate ที่ดีที่สุดของระบบ (และ h1_adx เป็น gate เสริมที่ DOWN mirror แรงสุด)

### 3) ผลบวกที่ไม่คาด: "FLAT=บ่อหลัก" ยืนยันข้าม 6 detectors

top E ของ detector **ทั้ง 6 ตัว** (เก่า 3 + ใหม่ 3) อยู่ที่ state FLAT หมด: h4_ema RSIRev-SELL-TP12SL6|FLAT +19.92 · d1/h1/m30_ema/m30_adx StochRev-SELL-TP12SL12|FLAT +10.18–10.67 · m15 MACDCross-SELL-TP10SL20|FLAT +5.74 — ผล 12-13/11 ไม่ใช่ artifact ของ H4 · อีกทาง: ensemble ของสามตัวใหม่ก็ contrarian (DOWN×3 → mean +2.95) ตรงกับ pattern "detector ยืนยันช้า"

## ค้าง / next

- ปีเดียว 1 era — verdict นี้พิสูจน์ในปี 2025-10→2026-11; ถ้าตลาดเปลี่ยนสกุล regime ควร re-run สคริปต์เดิม (deterministic)
- robust label นับข้าม 6 detectors — M15/M30 สายเดียวกัน correlate กันเอง (แผนแก้: ถ้าใช้จริง ให้อ่าน cross-family เท่านั้น)
- bar-mode two-edge ยังเป็น resolution หลัก — tick upgrade ค้างเหมือนเดิม
- ข้อเสนอถึงคุณหมอ: **ไม่ต้องเปลี่ยน regime TF** — ต่อยอดผล 12-13/11 ตรงๆ (เริ่ม FLAT→SELL ก่อนอย่างเดียว · เมนู DOWN ใช้ h1_adx เสริมสำหรับ EMACross-BUY mirror) · ปิดสายลับ "M30/M15 จะเข้มกว่า" ด้วยตัวเลขแล้ว
