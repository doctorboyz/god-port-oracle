# Retro — Conditional Live Cells (T1-T4) · 2026-11-13

**งาน:** เปิด 2 conditional cells สดใน bet farm — **BET-RF** = RSIRev-SELL-TP12:SL6 และ **BET-SF** = StochRev-SELL-TP12:SL12 ทั้งคู่ gate `h4_ema=FLAT` (แท่ง H4 ที่ปิดแล้วเท่านั้น) seed $1,000/cell — เก็บ out-of-sample จากผลแชมป์ของ conditional playbook
**โจทย์:** ada-task-20261113-conditional-live-cells.md (คุณหมออนุมัติผ่าน Hermes 13/11)
**ผล:** T1-T4 เสร็จครบเกณฑ์ · 9 cells เดิม + farm1/farm2 + VPS/บช.จริง **ไม่แตะแม้ตัวเดียว** (compose diff = additive ล้วน) · full suite **1401 passed, 2 skipped** (baseline 1378 + 23: 20 ตัวใหม่ + infra +1 + parametrize FAMILIES +2) · พิสูจน์สด 4 ข้อจาก container (ดูหัวข้อ "พิสูจน์สด")

## สิ่งที่ทำ

- **T1 spec+engine** — `CellSpec` มี field `gate` (default None = 9 cells เดิมไม่ขยับ) · `broky/bet/gates.py` ใหม่: `gate_state_on_last_bar()` **ใช้ machinery เดิม** `resample_htf` + `state_h4_ema` + `align_state_to_m5` จาก betbracket/htf.py (มี causal test แล้ว — ไม่เขียนใหม่) · `entries.py` เพิ่ม family **R** (RSIRev) และ **S** (StochRev): ยิง **SELL ที่ทุก event ทั้งสอง polarity** — เรียก polarity series จาก `betbracket/events.py` ตรงๆ แล้ว flatten เป็น SELL เดียว (theory-free ผลที่ชนะ ห้ามเติมทฤษฎีกลับ) · `cells.py`: geometry axis เพิ่ม TP12:SL6 + TP12:SL12 (แชมป์) และ parse `BET_GATE_DETECTOR/BET_GATE_STATE` fail-closed (ครึ่งเดียว/ชื่อผิด = ValueError ตอน boot ไม่ใช่เงียบกลางทาง) · yml เพิ่ม BET-RF/BET-SF + regenerate compose (diff: ACCOUNTS ต่อท้าย 2 ตัว + env 16 บรรทัดใหม่ **เท่านั้น**)
- **T2 tests** — ไฟล์ใหม่ `test_bet_conditional_cells.py` 20 ตัว: fire SELL ทุก event ทั้ง BUY/SELL polarity + fire indices ตรง bracket machinery · gate causal (mutate forming bucket ไม่ขยับ / bucket ที่ปิดตรง query time เห็นทันที / warmup → None) · live path (FLAT+event เปิด SELL ด้วย geometry แชมป์ · UP ข้ามพร้อม log `skipped — gate h4_ema=FLAT required, got UP` · ไม่มี event ไม่เปิด · transition log ทีเดียวต่อการเปลี่ยน state) · config errors fail-closed · แก้ 4 contract tests ที่ล็อก "9 cells" เดิมเป็น 11 (เกณฑ์เดิมทุกข้อคงไว้: 9 ตัวเก่า family/geometry/seed/gate=None ไม่เปลี่ยน + idempotent seed ยังผ่าน)
- **T3 deploy+พิสูจน์สด** — restart oracle-engine-farmbet: containers healthy · 11 cells ครบใน DB seed $1,000 (engine seed ตอน boot idempotent) · gate ทำงานจาก log (transition `gate h4_ema state: X` + skip/armed ตรง state) · ยิง SELL ทุก event — ดูหัวข้อ "พิสูจน์สด" ด้านล่าง
- **T4 ปิดงาน** — retro นี้ + handoff `~/.hermes/invest/handoffs/2026-11-13_conditional-live-cells.md`

## พิสูจน์สด (หลัง restart 01:55 UTC 13/11)

1. **Containers healthy:** oracle-engine-farmbet/farm1/farm2/paper-fetcher ทั้งหมด healthy · RestartCount=0 ไม่มี crash loop · ไม้เปิดเดิมรอด restart (graceful close ทำ WAL checkpoint ตามที่ออกแบบ)
2. **11 cells ครบใน DB:** log boot `seeded 2 bet cells: BET-RF, BET-SF` → `bet engine starting — 11 cells` · DB: BET-RF (R, TP12:SL6, seed 1000, balance 1000) + BET-SF (S, TP12:SL12, seed 1000, balance 1000)
3. **Gate ทำงานจริงกับ feed สด:** รอบแรกหลัง boot log ทันที `[BET-RF] gate h4_ema state: DOWN` + `[BET-SF] gate h4_ema state: DOWN` — ตรงกับ probe อิสระก่อน restart เป๊ะ (อ่านจาก CSV เดียวกัน ด้วย machinery เดียวกัน) · state ปัจจุบันไม่ใช่ FLAT = ทั้งสอง cell กำลังถูก gate กั้นอยู่จริง
4. **ไม้เปิดจริงเมื่อ FLAT+event ตรงกัน:** ค้างตามจังหวะตลาด — event เกิด ~4-8 ครั้ง/วันรวมสอง indicator และ FLAT กินเวลา ~20% → คาด log `skipped` ตัวแรกภายในไม่กี่ชั่วโมง และไม้จริงตามหลัง · **กลไกนี้ถูกพิสูจน์แล้วที่ชั้น test** (20 ตัว: FLAT+event เปิด SELL · UP ข้าม · causal) — ที่เหลือคือเวลา ไม่ใช่ความถูกต้อง · watcher ติดไว้เก็บบรรทัด log เมื่อมาถึง

## สูตรเต็ม (ล็อกไว้เป็นหลักฐาน)

| Cell | Event | ทิศ | Geometry | Gate | Seed | Backtest (75k แท่ง, bars two-edge) |
|------|-------|-----|----------|------|------|-----------------------------------|
| BET-RF | rsi_reversal (RSI14 ตัด 30 ขึ้น/70 ลง — **ทุก event ทั้งสอง polarity**) | SELL เสมอ | TP12:SL6 | h4_ema=FLAT แท่งปิดเท่านั้น | $1,000 | n=387 · WR 41.1% vs BE 33.3% · E +19.92$ · แข็งแรง 4/4 |
| BET-SF | stoch_reversal (%K14,3 ตัด 20 ขึ้น/80 ลง — **ทุก event ทั้งสอง polarity**) | SELL เสมอ | TP12:SL12 | h4_ema=FLAT แท่งปิดเท่านั้น | $1,000 | n=1152 · WR 59.8% vs BE 50.0% · E +17.95$ · แข็งแรง 4/4 |

สัญญาอื่นคงเดิมทุกข้อ: 1 ไม้/cell/รอบ · ถือจนจบ · tick resolution สด (fallback bar-conservative) · risk 1.5% ของ balance ปัจจุบัน · spread $0.20/oz round trip

## เกณฑ์ตัดสิน out-of-sample (ตกลงกับ Hermes)

- รวบรวมจน **n≥50 ไม้ที่ resolve แล้วต่อ cell**
- **ผ่าน** = WR > BE + 2pp (BET-RF: BE 33.3% → ต้อง > 35.3% · BE รวม spread = (6+0.2)/18 = 34.4% → > 36.4%) · (BET-SF: BE 50.0% → ต้อง > 52.0% · BE รวม spread = 50.8% → > 52.8%)
- **ไม่ผ่าน** = ถึง n แล้ว WR ต่ำกว่าเกณฑ์ → ปิด cell รายงานคุณหมอ (ไม่แก้สูตรกลางทาง — สูตรตายตัว)
- ระหว่างทางไม่ตัดสินจากไม้เดียว — n คือหลักฐานเดียว

## บั๊ก

1. **บั๊กจริงใน implementation หลัก: 0 ตัว** — 4 tests ที่ fail รอบแรกเป็น contract tests ที่ล็อก "9 cells" ตามเกณฑ์เดิมพอดี (ทำงานถูก แจ้งถูกเรื่อง) — อัปเดตเป็น 11 โดยเก็บเกณฑ์เดิมทุกข้อ ไม่ใช่ bug ของโค้ดใหม่
2. **พลาดในการเขียน test เอง 1 จุด (แก้ที่ความคาด):** synthetic history รอบแรกสั้นเกิน (7 H4 buckets < warmup 50) ทำให้หา event ที่ gate=FLAT ไม่เจอ — แก้ด้วยการ craft "base 54 buckets + tail อยู่ใน forming bucket" ซึ่งแฝดกับ causal contract พอดี: gate มองไม่เห็น forming bucket โดย construction จึงกะได้แม่นโดยไม่ต้องเดา state
3. **เดี่ยวที่ควรระวังใน live:** gate state อ่านจาก **feed M5 ที่ fetcher เขียนเฉพาะแท่งปิดแล้วเท่านั้น** — ถ้า feed ค้าง (stale guard 1800s) ไม่มีไม้ใหม่แต่ resolution ยังเดินด้วย tick ตามเดิม (farm parity เดิม)

## ค้าง / next

- เก็บ out-of-sample จริง — คาดหมายครบ n=50 ต่อ cell ประมาณ 3-6 สัปดาห์ (RF: ~387 fires/12 เดือน ทั้งหมด → gate FLAT คือ ~20% ของเวลา · SF: ~1152 fires/12 เดือน)
- ระหว่างทางอ่าน log: `gate h4_ema state:` (ต่อการเปลี่ยน state) + `skipped — gate ... required, got ...` (ต่อ event ที่ถูกข้าม) — สองบรรทัดนี้คือ audit trail ว่า gate ยังเห็น data จริง
- ถ้าผ่านเกณฑ์ → เสนอคุณหมอ scale seed ตามลำดับ $100→$500→$1,000 ต่อ cell จริง (หลัง paper)
- ถ้า FLAT window หิว n นานเกิน — ตัวเลือกถัดไปจาก playbook เดิม (ไม่แตะสูตรสองตัวนี้): RSIRev-SELL-TP12:SL12 (E +14.10) เป็นตัวเก็บที่สอง
