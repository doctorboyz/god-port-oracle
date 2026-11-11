# Retro — Conditional Bracket (T1-T4) · 2026-11-12

**งาน:** ติดป้าย trend state (UP/DOWN/FLAT) ให้ทุก event ของ bracket grid ด้วย detector 3 ตัว → รัน grid แยกตาม state → ตอบ 3 คำถามคุณหมอ
**โจทย์:** ada-task-20261112-conditional-bracket.md (คุณหมออนุมัติ 12/11 ผ่าน Hermes)
**ผล:** T1-T4 เสร็จครบเกณฑ์ · full suite **1335 passed, 2 skipped** (baseline 1320 + 15 ใหม่) · spot-check อิสระ 2 cells ตรงเป๊ะ

## สิ่งที่ทำ

- **T1** `broky/betbracket/htf.py` — M5→H1/H4/D1 (UTC buckets, หยุดตลาด = bucket หายเฉยๆ) + detector 3 ตัว:
  h4_ema (EMA50 slope, ε=2bp/แท่ง, warmup 50) · d1_structure (recent 2 vs prior 3: HH+HL/LL+LH) · h1_adx (ADX14≥25 + EMA20 ทิศ)
- **No-lookahead**: state ของไม้ที่เข้า ณ จุด close ของ M5 signal bar = แท่ง HTF ที่**ปิดสนิทแล้วล่าสุดเท่านั้น** — causal tests 4 ชั้น:
  forming-bar ใช้ bucket ก่อนหน้า · mutate แท่งใน forming bucket แล้ว label เดิมไม่ขยับ · boundary 00:00 ตรงเป๊ะ · gap สุดสัปดาห์ fallback bucket ที่ปิดล่าสุด
- **T2** `scripts/bet_bracket_conditional.py` — resolve ครั้งเดียว 48 ledgers แล้วแยกตาม (detector × state) = 576 cells + stability rule เดิม
- **T3** detector quality: truth ย้อนหลัง = D1 close vs SMA(63) (5 transitions ในปี) · lag · FP · conditional WR vs overall WR
- **T4** รายงาน `ψ/outbox/bet_bracket_conditional_2026-11-12.{md,json}` (reproducible ทั้งไฟล์ รวมคำตอบ 3 ข้อ)

## บั๊กจริงที่จับได้ (2 ตัว — ทั้งคู่ "เงียบ" ไม่ crash)

1. **pandas-3 resolution trap:** resample ให้ index เป็น `datetime64[us]` — การ hand-build epoch **ns** แล้วเทียบกับ ints จาก `astype("int64")` (µs) = หน่วยคลาด 1000 เท่า → searchsorted ตกท้าย array → lag censored **ทั้ง 5 transitions** แทนที่จะ crash มันแค่คืนค่าว่างเฉยๆ
   - แก้ที่ต้นเหตุ: helper `_epoch_ints()` บังคับให้ทุก timestamp มาจาก representation เดียวกัน + causal test `test_lag_not_censored_unit_consistency` lock ไว้
   - **บทเรียน:** ข้าม pandas version ใหญ่ (2→3) = อย่าเชื่อ unit ของ int64 จาก datetime — ห้าม mix "ค่าที่คำนวณเอง" กับ "ค่าที่แปลงจาก index"
2. **Assumption แทนการอ่าน contract (2 ครั้งใน spot-check ของตัวเอง):**
   - ครั้งแรก: กรอง event เฉพาะ polarity ตรงข้า้า → n ได้ครึ่งเดียว (bracket คือ theory-free: **ทุก fire เปิดทั้ง BUY+SELL**) — n=1152 กลายเป็น 557
   - ครั้งที่สอง: เทียบ `outcome == "TP"` ตัวใหญ่ ทั้งที่ contract ใช้ `'tp'/'sl'` lowercase → นับ TP ได้ 0 (แก้ด้วยการ import constant มาเทียบ)
   - **บทเรียน:** ตรวจงานด้วย "สิ่งที่เราคิดว่าค่าควรเป็น" = วิธีเจ็บช้ำสองต่อ — ต้อง import/อ่าน constant จาก contract จริงเสมอ; และถ้า spot-check ไม่ตรง อย่าสรุปว่า "คงใกล้เคียงพอ" ให้ไล่จนตรงเป๊ะ (ปรากฏว่าตรงสนิททั้ง WR และ E$/bet)

## ผลลัพธ์หลัก (ตอบ 3 คำถาม)

1. **SELL กระจุก UP? ไม่จริง — กระจุก FLAT:** 91 cells ผ่านเกณฑ์ stable+E>0 → SELL|FLAT **59** vs SELL|UP 11 · สูตรชนะหลัก StochRev-SELL-TP10SL20 พลิกเป็นจมที่ UP ของ h4_ema (E −1.14) แต่แรงสุดที่ FLAT (E +9.42, WR 73.6%)
2. **BUY mirror โผล่ที่ DOWN? โผล่แต่เฉพาะเจาะจง:** EMACross-BUY-TP12SL6 ผ่านเกณฑ์คงตัว**ทั้ง 3 detector** (E +1.01/+8.08/+3.85) — mirror ของสูตรหลัก TP10SL20 ไม่เกิดจริง · n DOWN ใหญ่พอตัดสิน (~2,232/ledger) แต่มาจาก era ขาลงเดียว (มี.ค.–ส.ค. 2026)
3. **FLAT รอดไหม? มีและเยอะสุด:** 59/91 cells เป็น SELL-in-FLAT แทบทั้งหมด ฝั่ง BUY แทบไม่มี — FLAT ไม่ใช่ม้านิ่ง แต่คือบ่อหลักของระบบปีนี้

**Detector:** h4_ema แม่นทิศสุด 67.9% (FP 32.1%) · d1_structure 63.4% · h1_adx 56.5% (แยะสุด) → เสนอ gate ด้วย h4_ema · lag median 0 แท่ง = detector พลิกก่อน SMA63 ยืนยัน (truth ถูกออกแบบให้ล่าช้า)

**สูตร conditional ที่เสนอ:** FLAT → SELL ทั้งกลุ่ม (E +5.6 ถึง +19.9) · UP → งด SELL + เล่น RSIRev-BUY-TP12SL6 (+11.10) · DOWN → EMACross-BUY-TP12SL6 + StochRev-SELL-TP10SL20 ยังชนะ (+2.45) — ผิวสมมติฐานเดิมของคุณหมอทั้ง 3 ข้อ

## ค้าง / next

- bar-mode two-edge (pes) อยู่ — tick archive ยังโหลดเบื้องหลัง → tick-resolved conditional ยกระดับต่อไปได้เลยด้วย machinery เดิม
- ปีเดียว 5 truth transitions — DOWN era ใหญ่ชุดเดียว: ผล DOWN-side ควรอ่านว่า "พิสูจน์ใน 1 ยุค"
- ข้อเสนอถึงคุณหมอ: ถ้าจะเอา conditional ไปใช้จริง ควร gate ด้วย h4_ema และเริ่มจาก FLAT→SELL เท่านั้นก่อน (n มากสุด + edge หนาสุด) — paper farm รันต่อไป้เก็บข้อมูลจริงนอก sample
