# Retro: Repurpose บช. A → P0 — P22 champion บน Exness Standard USD ($100)

**วันที่**: 2026-11-17 (ค่ำ)
**สาขา**: mr-bet-bcd
**คำสั่ง**: คุณหมอผ่าน Hermes ("เปลี่ยนชื่อ Real A เป็น P0 รันจริงด้วย P22 balance 100" — งานด่วน คิวต่อจากงาน 9)

## ทำอะไร

Repurpose บช. A เดิม (Exness-MT5Real15 · standard USD $100 ฝากแล้ว) จาก
config Real-A อายุ 4 สัปดาห์ (ranging_hard_block=1 + m5-scalp + ML veto +
code ก่อน bugfix ชุดหลัง) เป็น **P0 = P22 champion ชุดเดียวกับ P1-P3
แต่ baseline สกุล USD** — เหตุผลเร่งด่วน: engine กำลัง LIVE ด้วย setup
เก่าที่ไม่ใช่เจตนาคุณหมอ (ตอนนี้ไม่เข้าไม้เพราะ config เก่าบล็อก
ตลาด ranging — พอเงื่อนไข config เก่าตรง มันจะเทรดจริงด้วย setup ผิด)

## ไฟล์ที่แตะ (3 ไฟล์ — เท่าที่จำเป็น)

1. `docker-compose.vps.yml` — service `oracle-engine` เท่านั้น:
   config ทั้งบล็อก → P22 champion (ATR 2.0 · RR 1.20 · conf 0.55 ·
   BOLL 0.85 · TS 12 · trail 1 · CB 1440 · risk 0.005 · SLcap 12 ·
   spread 30 · cost 3x · strategy_id `mr-bet-p22-P0`) · DRY_RUN=0
   hard-code (กัน stale host .env ชนะทั้ง arm ทั้ง disarm) ·
   RANGING_HARD_BLOCK ถอด (P22 เข้าตอน ranging) · m5-scalp/ML veto ปิด ·
   baseline 100 USD (ไม่ใช่ USC!) · DISPLAY_NAME_A=P0
2. `metty/core/account_registry.py` — จุดเดียวตามคำสั่ง: helper
   `_display_label()` + override `DISPLAY_NAME_<NAME>` (env-wins) ใช้
   ทั้ง `get_display_name()` และ property `AccountConfigInfo.display_name`
   — internal key "A" ไม่ถูกแตะที่ไหนเลย (registry/DB/kill-switch)
3. `tests/test_p0_repurpose_causal.py` — ไฟล์ใหม่ 30 ตัว ตามแบบ
   test_p22_cent_deploy_causal: compose contract (knob matrix ·
   ห้าม ${...} บนคีย์วิกฤต · RANGING_HARD_BLOCK หาย · stale knobs หาย ·
   baseline 100 · DRY_RUN=0 · label P0/key A · **exact key-set บนผิว _A**
   (กัน drift เงียบจาก key ใหม่ในอนาคต — ข้อเสนอรอบ review) · mt5a
   ไม่ถูกแตะ · kill-switch DB wiring ครบ) + object-level causal (env →
   LiveTrader จริง → DD baseline 100.0 · TradeBlocker 12/3.0/0.05 ·
   max_positions เสมอ 1 · display_name "P0") + sizing min-lot floor +
   USD DD both ways

## จุดอันตรายที่กันไว้ (บทเรียนสำคัญ)

- **สกุลเงินกลับด้านจาก P-cent**: P1-P3 เจอ trap "USD baseline ต่อ USC
  equity = DD line ตายเงียบ" — บช.นี้กลับทาง: ถ้า copy-paste 10000
  (USC-style) มาใส่ baseline ต่อ equity $100 → engine อ่านเป็น -99%
  ตั้งแต่ cycle แรก = **freeze ถาวรตั้งแต่บูต** (ทางกลับของ trap เดิม
  รากเดียวกัน: หน่วยผิด) — พิสูจน์ both ways ใน test
- **บทเรียน /app/.env ใน image**: การถอด RANGING_HARD_BLOCK แบบ
  "unset" ปลอดภัยเฉพาะเมื่อ image ใหม่ (มี .dockerignore ตั้งแต่
  ISSUE-103 แล้ว — ไม่มี .env ติดไปใน image → load_dotenv เป็น no-op
  → default 0 ตาม champion) — runbook บังคับ rebuild image ก่อน
  ข้ามขั้นนี้ไม่ได้

## RED→GREEN (CPT)

- RED: test ใหม่รันกับ artifact ก่อนแก้ = **แดง 11 ตัวพอดี** (compose
  contract 7 · display_name object · DD baseline · registry override 2 —
  ตัวไม่เกี่ยวยังเขียว = แดงตรงจุด)
- แก้ 1 จุดตอน RED: test "cent-style baseline" เขียนทิศผิด (มันไม่ใช่
  dead layer แต่ freeze-at-boot) — แก้ให้ตรง semantics จริงก่อนแตะโค้ด
- GREEN: 30/30 หลังแก้ compose + registry (+ exact key-set 1 ตัวจากรอบ
  review)
- RED ยืนยันซ้ำกับ artifact เก่า (git stash): compose contract **แดง 8/13
  พอดี** — 5 ตัวที่เขียวเป็นกลุ่ม control ที่ต้องเขียวทั้งสองฝั่ง
  (credentials ยัง env-sourced · mt5a ไม่แตะ · kill-switch wiring ·
  no-P-leak · back-compat label)
- ชุดวงแคบ (contract เพื่อนบ้าน): 163 passed 1 skipped
- Full suite: **1756 passed 2 skipped 0 failed EXIT=0** (รอบ 2 หลังอัปเดต
  contract ให้ guard 3 ตัว — ดูหัวข้อถัดไป)

## CROSS-CONTEXT REVIEW (บังคับงานเสี่ยง) — รอบแรก PASS

Reviewer อิสระ (ไม่มี context คนแก้) ตรวจจาก artifact จริง: diff hunks
อยู่ในบล็อก oracle-engine เดียวเป๊ะ · เทียบ env กับ P1 แบบเครื่องช่วย
58 keys ไม่มี drift · พิสูจน์ RED กับ compose เก่าเอง (8 ตัวแดง) ·
ผ่าน. ข้อเสนอที่รับไปใช้: exact key-set บน _A (กลายเป็น test ใหม่) ·
คำเตือน up-without-build ลง handoff · ยืนยัน runtime XAUUSDm/leverage
ลง handoff. ข้อที่บันทึกไว้เป็นความรู้: property `AccountConfigInfo.
display_name` ของ cent_real ครั้งเดิมคืน "Demo-Pn" (บั๊กเดิม — ไม่ตรง
กับ get_display_name ที่คืน "Cent-Pn") — การรวม logic ผ่าน helper เดียว
แก้ให้ตรงกันเอง (label เท่านั้น ไม่มีผล routing)

## IRON ที่รักษาครบ

- mt5a ไม่ถูกแตะแม้บรรทัด (test ล็อก MT5_CMD_OPTIONS/ports/volume)
- service อื่นทั้งหมดไม่ถูกแตะ (git diff = 3 ไฟล์เป๊ะ)
- metty/ แตะจุดเดียวที่คำสั่งอนุญาต: display label ใน account_registry
- ไม่มีอะไรถูก deploy/start (repo เท่านั้น — Hermes เป็นคน deploy)
- credentials ยังเป็น ${...} จาก VPS .env เท่านั้น (test ล็อกกัน
  hard-code)

## Full suite รอบ 1 — guard 3 ตัวจับเรา (ทำงานถูก ต้องอัปเดต contract ที่เฝ้า)

- `test_core_integrity` — manifest IRON ล็อก hash `account_registry.py`
  (มัน build paper-farm image ด้วย) → อัปเดตด้วย `--update` diff ออกมา
  บรรทัดเดียวเป๊ะ (hash ไฟล์เดียว) ตรวจตาก่อน (แบบเดียวกับ f6f9a3f)
- `test_env_coverage_guard` — หลังถอด config เก่าของ Real-A แล้ว มี 3
  env family ที่โค้ดยังอ่านแต่ไม่มี config ใดตั้งอีกเลย (M5_MAX_SPREAD_ ·
  TRAILING_ACTIVATION_PCT_ · TRAILING_TRAIL_PCT_) — เดิมครอบครัว
  TRAILING มีแค่ _A=0.40/0.20 ของ Real-A เป็นคนตั้งเดียว พอถอด = dangling
  ทั้งครอบครัว → ใส่ ALLOWED_OPTIONAL พร้อมเหตุผล (default 0.20/0.10
  คือค่า champion ตั้งใจ · scalp path ตายทุก service) — ตาม mechanism
  ที่ guard เองกำหนดไว้
- `test_risk_env_override_causal` — expected set ของบช.ที่ตั้ง
  RISK_PER_TRADE_* ใน compose เพิ่ม "A" (P0 เข้า ladder ตามคำสั่ง)
- รอบ 1: 1753 passed 3 failed 985.30s (3 ตัวแดง = guard ทั้งสาม)
- รอบ 2 หลังอัปเดต contract: **1756 passed 2 skipped 0 failed
  978.23s EXIT=0** จาก artifact จริง (baseline c18fc52 = 1726 + ใหม่
  30 = 1756 เป๊ะ)

## บทเรียน

- **Guard 3 ตัวนี้คือ "คนเฝ้าประตูที่ถูกต้อง"** — การแดงของมันไม่ใช่
  false positive แต่คือการบังคับให้เราตอบคำถาม: "env ที่ถูกถอด
  ตั้งใจให้ default จริงไหม" และ "บช.ใหม่ที่ตั้ง risk ครบตาม ladder จริงไหม"
  — ตอบเป็นลายลักษณ์อักษรใน allowlist/expected set แล้วมันเขียว
- เวลาแตะ contract ของบช.จริง ให้คาดไว้เลยว่าจะโดน guard จับ 2-3 ชั้น
  (manifest · env-coverage · risk-intent) — เตรียมคำตอบพร้อมเหตุผล
  อย่าเพิ่ง override guard

## งานต่อคิว

- Deploy ตาม runbook ใน handoff (ลำดับบังคับ: rebuild ก่อน → ตรวจ
  equity_ts → ตรวจ log "P0" → mt5a startedAt ไม่เปลี่ยน)
- engine state-sync ไม้ค้าง is_open=1 (ค้างจาก c18fc52)
- ตั้ง KILL_SWITCH_DAILY_LOSS_PCT รายบช. เมื่อ kill-switch กลับมา armed
  (ค่าเริ่มต้น 3% กลาง + min-lot SL $6-12 = 6-12%/ไม้ บน P0 — ไม้เดียว
  ทลาย threshold กลางได้ง่าย)
