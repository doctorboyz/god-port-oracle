# Retro — Hardening follow-up 3 ชิ้น (anchor flake / env-wins audit / flatten fail-loud)

**งาน**: เก็บกวาดความแข็งแรง 3 ชิ้นจาก retro งาน 4 (คุณหมออนุมัติผ่าน Hermes 17/11/2026)
**สาขา**: mr-bet-bcd · **ผู้ทำ**: maw oracle session · **วันที่**: 2026-11-17

## 1. anchor flake ตัวสุดท้าย — test_forming_bucket_is_invisible

- **สาเหตุจาก artifact จริง**: spike 10 แท่งถูกต่อตรง `_now()` เลย ถ้ารันช่วง <50 นาทีก่อน H4 boundary spike จะปิด bucket ตัวเองและ gate เห็น UP — RED proof ด้วย monkeypatch `_now` ที่ minute 195 (gate อ่าน UP จริง, แดง ~21% ของวัน = minute ≥190)
- **แก้**: ต่อ base ผ่าน `_anchored_base` (helper เดียวกับงาน 4) ก่อนต่อ spike
- **จับได้เพิ่มจาก cross-context review (สำคัญที่สุดของงานนี้)**: `_anchored_base` ของงาน 4 เองมีบั๊กแฝง 2 ตัว ทำใาว family-tail tests ของงาน 4 ก็ยัง flake อยู่:
  1. **floor ทิ้งแท่ง** — frame จริงพันธนาการตามวินาทีของ `_now()` (ตรวจแล้ว: `pd.date_range(end=...)` **ไม่ snap** ไป 5-min grid) แต่ boundary เป็น grid-aligned → Δ ไม่ลงตัว → `//300` floor ทิ้งหนึ่งแท่ง → แท่งแรกของ tail หลุดเข้า bucket ที่กำลังปิด และเพราะ H4 close = close ของแท่งสุดท้ายใน bin **แท่งเดียวก็พลิก gate** (sweep จริงก่อนแก้: wrong-state 128 จุด + crash 12 จุด; แดง 36 จาก 50 minute ที่ sec=0)
  2. **extend_bars=0 → IndexError** — เมื่อ tail เริ่มห่าง boundary <5 นาที สูตรเกินคำนวณ 0 แท่ง → `make_m5([])` ระเบิด (หน้าต่าง ~นาที 235-239 ของทุก bucket)
  - ตัว test แรกที่ผมเขียนก็เลือก offset พหุคูณของ 5 พอดีตรงจุด floor==ceil เลยเขียวโดยบังเอิญ — reviewer จับได้เลย
- **แก้ที่ helper จุดเดียว**: `bucket_end` = วันปิดของ bucket ที่เก็บแท่งแรกของ tail (exact on-grid start บังคับข้ามไป bucket ถัดไป) + `extend_bars = max(1, math.ceil(...))` + เข้า natural-fit เฉพาะเมื่อ `bucket_end > last_tail_close`
- **พิสูจน์แบบ exhaustive ไม่ใช่ sampling**: 240 นาที × วินาที {0,1,27,59} × 4 scenario = **3,840 จุด 0 bad** + reviewer พิสูจน์ซ้ำรวม microsecond (3,904 จุด 0 bad) + วิเคราะห์ invariant เชิงคณิต — ไฟล์เขียวทุก offset ของวันจริงๆ ตอนนี้
- **บทเรียน**: "เขียวทุก offset ที่เลือกไว้" ≠ "เขียวทุก offset" — ต้อง sweep ทั้ง cycle + อย่าเชื่อว่า timestamp synthetic อยู่บน grid ของ freq เสมอไป; และ cross-context review จับบั๊กที่คนแก้ (ผม) พิสูจน์ "เขียวแล้ว" ด้วยตัวอย่างโชคดี

## 2. audit 'if not risk_config' ทั้ง engine — เจอจริง 1 จุด

- **รายการที่ตรวจครบ** (grep `or risk_config` / `if not risk_config` ทั้ง metty/ broky/ scripts/):
  - `live_trader.py:215` `risk_config or RiskConfig(...)` — สะอาด (risk_per_trade/RR/conf/ATR/KNOB/ptp/tp1r/rrsi/trail เป็น env-winsหรือ unconditional แล้วทั้งหมด)
  - `m5_scalp_trader.py:144` `risk_config or M5ScalpRiskConfig()` — ตัวแปรนี้เองไม่ใช่บั๊ก
  - **`m5_scalp_trader.py:170` `if not risk_config:` — บั๊กจริง** ครอบ `RR_RATIO_*`/`MIN_CONFIDENCE_*`/`M5_MAX_SPREAD_*` ทั้งสามตัว ตายบน live path เพราะ `run_m5_scalp_trader` (oracle_runner.py:284-292) ส่ง risk_config เสมอ → env โดนทับเงียบมาตลอด — คลาสเดียวกับ RR bug 2026-09-23 และ risk_per_trade 17/11 ร้อยเปอร์เซ็นต์
  - นอกจากนี้ตรวจแล้วว่า knob อื่นใน m5_scalp เป็น env-wins อยู่แล้ว: ATR (unconditional), ptp/tp1r/rrsi/trail (unconditional), BUY_MIN_CONFIDENCE (unconditional) — เหลือแค่สามตัวใน gate นี้จริงๆ; `risk_per_trade` ไม่มี per-account env ใน m5_scalp เลย (runner เป็นเจ้าของ) = ไม่มีอะไรโดนทับ
- **พิสูจน์ RED→GREEN**: test ใหม่ 5 ตัว — RED ก่อนแก้ 3 ตัว (env ตั้ง RR_RATIO_B=0.8/MIN_CONFIDENCE_B=0.55/M5_MAX_SPREAD_B=25 แล้ว risk ยังเป็น 2.0/0.50/30 เป๊ะ) → แก้ตาม pattern env-wins ของ live_trader (per-account → global → ไม่ set เลย = ค่า risk_config ยืน) → GREEN 15 passed (รวม neighbor suites)
- **ผลกระทบ production ที่ต้องรับทราบ (MEDIUM จาก review)**: oracle-engine (Real-A) เป็น service เดียวที่ `M5_SCALP_ENABLED=1` และ compose ตั้ง `RR_RATIO_A=2.5`/`MIN_CONFIDENCE_A=0.45`/`M5_MAX_SPREAD_A=45` ไว้ชัดเจน → หลังแก่นี้ m5-scalp ของ A จะใช้ค่าเหล่านี้จริง (เดิมเงียบๆ ใช้ default 2.0/0.50 + spread 30 จาก M5_SCALP_SPREAD_MAX) — นี่คือ intent ของ compose ที่ถูกละเมิดมาก่อน ไม่ใช่การเปลี่ยนใจกลางอากาศ แต่ **คุณหมอควร acknowledge** และรู้ว่า `M5_SCALP_SPREAD_MAX` ของ A จะเป็นค่าตายเมื่อ `M5_MAX_SPREAD_A` ตั้งอยู่ (knob ซ้อนกัน 2 ตัว)
- หมายเหตุ LOW จาก review (บันทึกไว้ ไม่แก้ — เกินขอบเขต/ไม่มี production impact วันนี้): (a) env-wins mutate ตัว risk_config ของ caller ใน place — ตาม pattern เดิมของ live_trader และ runner สร้าง object ใหม่ต่อ trader เสมอ (b) default เกลี่ย: A spread 40→30 เฉพาะ path ที่สร้าง M5ScalpTrader โดยไม่ส่ง risk_config และไม่มี env (ไม่มี production site ทำ)

## 3. kill_switch flatten fail-loud

- **บั๊กจาก artifact จริง**: `positions()` คืน None เมื่อ bridge พัง แต่ `flatten()` ใช้ `if not positions:` เห็น None เป็น [] เงียบๆ → `_act` รายงาน "flatten: 0 closed, 0 failed" สะอาดหู ทั้งที่ไม่รู้เลยว่ามีไม้ค้าง — kill switch เชื่อว่าบช. flat โดยไม่เคยอ่านมัน
- **แก้ 3 จุดใน flatten path เท่านั้น**: `flatten()` คืน None + log ABORTED · `_act` รายงาน "flatten: READ-FAILED — ... NOT confirmed flat (manual check required)" (docker stop ยังทำงานเหมือนเดิม — engine คือต้นน้ำของไม้) · `_enforce_blocked` log warning + ไม่อ้างว่า flatten สำเร็จ
- **RED→GREEN**: test ใหม่ 5 ตัว — RED ก่อนแก้ 4 ตัว (flatten คืน [] ทั้งที่ read fail / alert ไม่มี READ-FAILED) → GREEN ทั้งไฟล์ 47 passed (รวมตัว shape-guard กัน over-block: positions=[] จริง ยังคืน [] ไม่ใช่ None)
- caller ครบตามขอบเขต: grep ทั้ง repo ยืนยัน `.flatten()` มีแค่ 2 จุดใน kill_switch.py (ไม่มีที่อื่นพังจาก return type เปลี่ยน)

## ตัวเลขจริง

- รอบ 1 full suite (หลังแก้ครบ ก่อน manifest update): **1569 passed, 2 skipped, 1 failed (695.27s)** — fail เดียว = test_core_integrity manifest lock "ตามคาด" (แก้ metty/execution/m5_scalp_trader.py ตามอำนาจงาน)
- manifest `--update`: diff บรรทัดเดียว = hash m5_scalp_trader.py เท่านั้น ตรวจด้วยตาแล้ว
- รอบ 2 full suite: **1570 passed, 2 skipped, 0 failed (694.62s) EXIT=0**
- หลัง review จับบั๊ก helper → แก้ ceil/bucket_end → ไฟล์เป้าหมาย 52 passed + exhaustive sweep 3,840 จุด 0 bad + reviewer re-sweep 3,904 จุด 0 bad
- รอบ 3 full suite (state commit ปัจจุบัน): **1588 passed, 2 skipped, 0 failed (692.97s) EXIT=0**
- คณิต: baseline 1550 + ใหม่ 20 (m5 scalp 5 + kill switch 5 + forming-bucket 10) = 1570 รอบ 2 · รอบ 3 เพิ่มอีก 18 จุด parametrize จากการขยาย clock tests (forming 10→24 จุด, family-tail 2→4 จุดทั้งคู่) = **1588 เป๊ะ**

## IRON รักษาครบ

- แตะเฉพาะ: metty/execution/m5_scalp_trader.py (env-wins จุดเดียว ตาม pattern ที่มีอยู่), scripts/kill_switch.py (flatten path 3 จุด), tests/ 3 ไฟล์ + manifest 1 บรรทัด
- **ไม่แตะสูตร sizing/SL/TP แม้บรรทัดเดียว** (reviewer ยืนยัน clean) · ไม่แตะ compose ทุกไฟล์ / farm_variants.yml / docker/mt5/ / broky/ / oracle_runner.py
- ไม่มีอะไรถูก deploy/start ทั้ง VPS และเครื่องนี้ · ไม่มี secret ติด diff (grep สแกนแล้ว)

## งานที่ค้างเสนอ

- คุณหมอ acknowledge ผล m5-scalp Real-A (RR 2.0→2.5 / conf 0.50→0.45 / spread 30→45 ตาม compose intent) หรือถ้าไม่เอา ให้แก้ที่ compose ไม่ใช่โค้ด
- ปรับ `M5_SCALP_SPREAD_MAX` กับ `M5_MAX_SPREAD_*` ให้เหลือ knob เดียว (ตอนนี้ซ้อนกัน)
- (LOW) ถ้าวันหน้ามี caller แชร์ risk_config ระหว่างบช. → env-wins mutate จะ leak ข้ามบช. — ควรมีกติกา "runner สร้าง config ต่อ trader" ไว้ใน comment
