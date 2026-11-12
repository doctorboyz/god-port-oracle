# Retro — Regime Playbook (T1-T4) · 2026-11-13

**งาน:** ขยาย grid เป็น 7 geometries (252 ledgers) + วัดความแม่น "ทำนายล่วงหน้า" ของ detector 3 ตัว + เมนู bet ต่อ regime (แชมป์/สำรอง/ต้องเลี่ยง) + แพ็กเกจ playbook ให้คุณหมอเปิดอ่านเลือกได้ทันที
**โจทย์:** ada-task-20261113-regime-playbook.md (คุณหมอสั่งผ่าน Hermes 13/11)
**ผล:** T1-T4 เสร็จครบเกณฑ์ · regression 576 cells เดิมตรงเป๊ะ 0 mismatch · spot-check อิสระ 4 ค่าตรงเป๊ะ (forward 3 ค่า + new-geometry cells 2 ค่า) · full suite **1356 passed, 2 skipped** (baseline 1335 + tests ใหม่ 21 ตัว: 11 forward + 10 playbook) ผ่านหมด

## สิ่งที่ทำ

- **T1** `scripts/bet_regime_playbook.py` — geometry เพิ่ม 3 ตัว (TP4:SL8 เล็ก · TP12:SL12 สมมาตรใหญ่ · TP16:SL32 ใหญ่) รวม 7 × 6 events × 2 ขา = 84 ledgers/detector → **1,008 cells** (84×3 detectors×4 states) · `run_conditional` รับ geometries param (default เดิม — ตัวเลขเดิมไม่ขยับ) · **regression check อัตโนมัติ**: 576 cells เดิมเทียบกับ json 12/11 ทุก field (n/wr/e/streak/quarterly) = **PASS 0 mismatch** และ script **refuse เขียนรายงาน** ถ้า FAIL (fail-closed)
- **T2** `broky/betbracket/forward.py` — วัด "พูดแล้วราคาไปตามไหม" แทน "ตรงกับ truth ย้อนหลังไหม": forward 4h/24h/72h ต่อ detector×state (hit-rate · mean move · n) + baselines · persistence (survival + median run) · transition matrix ต่อ detector · ensemble agreement (UP×3/DOWN×3/UP×2/DOWN×2/no-signal)
- **T3** `broky/betbracket/playbook.py` — pass rule เดิมของคุณหมอ (n≥200 · WR>BE · stable ≥3/4 ไตรมาส) + E>0 · avoid = n≥200 และ E≤−$5 · **robust = ผ่าน ≥2 detectors** (กัน multiple-comparison ใน grid 1,008 เซลล์) · playbook ต่อ state เลือก detector ที่มีสูตรผ่านเยอะสุด
- **T4** รายงาน `ψ/outbox/bet_regime_playbook_2026-11-13.{md,json}` — 4 ส่วนตามโจทย์ ทุกตารางมี n คู่ BE

## บั๊กจริงที่จับได้ (1 ตัว) + บทเรียนคาดเลขใน test

1. **pandas None→NaN ใน transition matrix:** `pd.Series(["UP", None, "DOWN"])` ใน pandas-3 แปลง None เป็น NaN → guard `is None` ใน transition_matrix พลาด NaN หลุดมาเป็น "state ปลอม" ใน matrix เงียบๆ (row ปลอม + อาจ normalize ผิด) — แก้ด้วย `pd.isna()` (ครอบทั้ง None และ NaN) + test lock
   - **บทเรียน:** ใน object series ที่ pandas อาจ infer dtype ให้ "ค่าว่าง" มี 2 ตัว (None/NaN) — guard ต้องใช้ `pd.isna` ไม่ใช่ `is None`
2. **คาดเลขใน test ผิดเอง 3 จุดจาก 5 ที่ fail รอบแรก** — คิด `c[7]−c[3]=13` (จริง 10) · ลืมนับ anchor censored ปลายข้อมูลใน ensemble n · คิด survival ของ run 4 แท่งว่า "รอด" ทั้งที่ 4 แท่งข้างหน้าพ้น run ไปแล้ว
   - **บทเรียน:** อัตรา fail ของ test ใหม่รอบแรกควรอ่านเป็นสัญญาณให้ไล่ทีละ case ว่า "implementation ผิด หรือ มือเราคำนวณผิด" — ครั้งนี้ 3:1:1 (มือผิด : จริง : จริง) — อย่ารีบแก้ implementation ก่อนคำนวณมือซ้ำ

## ผลลัพธ์หลัก

### 1) Detector "ทำนายล่วงหน้า" แทบไม่ต่างจากเหรียญ — และ ensemble กลับตัว contrarian

- ต่อ detector×state hit-rate 4h/24h/72h อยู่ช่วง **43–55%** (baseline P(up)~50%): h4_ema UP 24h hit 51.1% แต่ **mean −3.3 $/oz** · h4_ema DOWN 24h hit 47.4% mean **+2.8** · d1_structure UP 72h hit 46.6% mean **−19.9**
- **ensemble ยิ่งตรงกัน ยิ่งกลับตัวชัด:** DOWN×3 (ทั้ง 3 detector บอก DOWN) → 24h hit **39.3%** mean **+14.6 $/oz** · 72h hit 34.6% mean +21.1 — คือทุกตัวยืนยันช้า ตอนเห็นตรงกันคือไม้วิ่งแล้ว
- **แต่ persistence จริง:** survival 24h ของ h4_ema UP/DOWN ~78–81% · transition stay ~90% — state เป็น "เข็มทิศโครงสร้างตลาด" ที่อยู่นานได้ ไม่ใช่ "สัญญาณทิศราคาล่วงหน้า"
- **อ่านรวม:** edge ของเมนูต่อ regime มาจาก *โครงสร้างการจบไม้* (FLAT → extreme แล้วสวิงกลับในกรอบ = SELL ชนะทุก geometry) ไม่ใช่จากการทายทิศถูก — detector ใช้เป็น**ตัวกรองสภาพตลาด** ไม่ใช่ตัวทาย

### 2) เมนูต่อ regime (151/1,008 เซลล์ผ่านเกณฑ์)

| State | Detector | แชมป์ | สำรอง | ต้องเลี่ยง (ตัวอย่าง) |
|---|---|---|---|---|
| FLAT | h4_ema | **RSIRev-SELL-TP12SL6** E+19.92 แข็งแรง×3 | StochRev-SELL-TP12SL12 +17.95 ×3 · RSIRev-SELL-TP12SL12 +14.10 ×3 | StochRev-BUY-TP12SL6 −25.21 |
| DOWN | h4_ema | **RSIRev-SELL-TP12SL12** E+3.50 แข็งแรง | RSIRev-SELL-TP16SL32 +3.25 · StochRev-SELL-TP10SL20 +2.45 | StochRev-BUY-TP12SL6 −9.78 |
| UP | d1_structure | **EMACross-SELL-TP12SL12** E+6.46 (ไม่มีป้าย) | EMACross-SELL-TP10SL20 +5.51 · EMACross-SELL-TP12SL6 +5.49 | RSIRev-SELL-TP12SL6 −15.67 |

- geometry ใหม่ 3 ตัวไม่ใช่ของแถม: **TP12:SL12 สมมาตร** ขึ้นแชมป์/สำรองเยอะที่สุดใน FLAT (StochRev-SELL +17.95, MACDCross-SELL +10.53, BollTouch-SELL +6.64 ทุกตัวแข็งแรง×3) · **TP4:SL8 เล็ก** โผล่เฉพาะเมนู BUY ใน UP (RSIRev-BUY +1.88 แข็งแรง — จบไว avg 4 แท่ง) · **TP16:SL32 ใหญ่** โผล่ใน DOWN (RSIRev-SELL +3.25 · Breakout20-SELL +1.73 · EMACross-BUY +2.55 — ทน trend ยาว)
- สูตรเดียวกันสลับขั้วตาม state: RSIRev-SELL-TP12SL6 คือแชมป์ FLAT (+19.92) แต่อยู่ avoid list ของ h4_ema UP (−15.96) — ยืนยันว่า gate ตาม regime ไม่ใช่ตัวประดับ

### 3) คำตอบคำถามคุณหมอ "แต่ละ trend ทำนายแม่นแค่ไหน"

**แม่นในฐานะ state ไม่ใช่ในฐานะสัญญาณ** — ตรง truth ย้อนหลัง h4_ema 67.9% (จากรายงาน 12/11) · ทำนายทิศ 24/72h ได้แทบไม่ต่างเหรียญ · คงทน (survival/stay) สูง — เหมาะใช้ gate "สภาพตลาด" ประกบเมนู bet ตาม playbook ไม่เหมาะใช้ทายราคา

## ค้าง / next

- bar-mode two-edge ยังเป็น resolution หลัก — tick archive ค้าง upgrade เหมือนเดิม
- ปีเดียว era DOWN เดียว — เมนู DOWN พิสูจน์แค่ 1 ยุค; ensemble contrarian อาจเป็นฤดูกาลของปีนี้
- grid 1,008 เซลล์ = multiple comparison กินแรง — ป้าย "แข็งแรง" (≥2 detectors) คือเกณฑ์ขั้นต่ำก่อนเลือกใช้จริง
- ข้อเสนอถึงคุณหมอ: เริ่มจาก **FLAT→SELL (RSIRev/StochRev TP12SL6+TP12SL12)** ก่อนอย่างเดียว เพราะ n มากสุด + edge หนาสุด + แข็งแรง×3 ครบ · UP→เมนู d1 EMACross-SELL รอ paper ยืนยันต่ออีกหน่อยเพราะแชมป์ไม่มีป้าย robust
