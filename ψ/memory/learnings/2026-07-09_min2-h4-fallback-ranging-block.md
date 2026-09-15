# 2026-07-09 — Real-A 3 fixes: min positions 2 / h4 fallback / ranging hard-block

## Context

วันนี้ (2026-07-09) วิเคราะห์ Real-A post-deploy พบ 3 ปัญหาเรียงตาม priority:

1. **Position limit บล็อก 35+ signals ใน 24 ชม.** — equity ตก $346-379 →
   `max(1, min(cap, floor(equity/200)))` = 1 → มี position เปิดอยู่ก็บล็อกหมด.
   สาเหตุหลักของ gap ระหว่างจริง (WR 71%, PF 1.47) กับ AEGIS backtest (90%, 5.3).
2. **m5 BUY #5553 ขาดทุน $8 ในวัน SELL-bias** — `d1_trend=unknown` ทำให้
   `compute_trend_alignment_value` return 0 (neutral) → gate ปล่อย. m5 ใช้ H1-proxy
   บอก d1 ไม่ได้บ่อย. `h4_trend` มีอยู่ใน generator signature แต่ไม่ถูกใช้ (dead parameter).
3. **AEGIS ไม่บล็อก ranging ตามกฎ "Ranging = พัก"** — regime=ranging แค่ label
   ไม่มี hard-block. m5 ปล่อย BUY ใน ranging ได้ (#5553). swing ปล่อย ranging SELL ได้.

## Fixes

### Fix 1 — min positions floor (env-configurable)

**Symptom**: equity ตก < $400 → max_positions=1 → มี position เปิดอยู่ → block ทุก signal ใหม่
**Cause**: `_calculate_max_positions` formula = `min(cap, floor(equity/per_pos))` ไม่มี floor ขั้นต่ำ
**Fix**: formula ใหม่ = `min(cap, max(min_positions, floor(equity/per_pos)))` — floor เป็น env var
**Causal test**: `tests/test_calculate_max_positions.py::TestCalculateMaxPositionsRealAScenario`
  - $78 → 2 (was 1), $200 → 2 (was 1), $400 → 2 (was 2), $600 → 3 (unchanged)
  - `test_cap_below_floor_cap_wins`: cap=1, floor=2 → result=1 (cap is hard ceiling)
**Files**: `metty/execution/live_trader.py`, `metty/execution/m5_scalp_trader.py`
**Env**: `MIN_POSITIONS_A=2` (Real-A only), default 1 for B/C/D
**Result**: 78/78 tests pass

### Fix 2 — h4 fallback ตอน d1=unknown

**Symptom**: m5 BUY #5553 ผ่าน gate ทั้งที่ h4=bearish (counter-trend) เพราะ d1=unknown → trend_alignment=0
**Cause**: `compute_trend_alignment_value` ใช้ d1 เป็น primary HTF; เมื่อ d1=unknown return 0 (neutral)
  ทิ้ง h4_trend ที่เป็น dead parameter — m5 ใช้ H1-proxy บอก d1 ไม่ได้บ่อย
**Fix**: เมื่อ d1=unknown ให้ fall back ไป h4_trend; both unknown → 0 (neutral เหมือนเดิม)
  - d1 known → d1 wins (primary HTF, current behavior preserved)
  - d1 unknown + h4 known → h4 decides (aligned=1, counter=-1)
  - both unknown → 0 (neutral)
**Causal test**: `tests/test_reversal_signal.py::TestComputeTrendAlignmentH4Fallback` (9 tests)
  - `test_d1_unknown_h4_bearish_buy_counter` — #5553 scenario → -1 (was 0)
  - `tests/test_m5scalp_reversal_gate.py::TestH4FallbackIntegration` — end-to-end gate blocks #5553
**Files**: `broky/signals/generator.py:compute_trend_alignment_value`
**Known limitation**: `compute_reversal_signal` short-circuits ตอน d1=unknown → `has_reversal=False`
  เสมอเมื่อ d1=unknown. นั่นหมายความว่า h4-counter reversal trade (d1=unknown + h4=known +
  reversal evidence) จะถูก block เพราะ `has_reversal=False` → `trend_alignment=-1` (ไม่ใช่ 2).
  rare case — ยอมรับชั่วคราว, บันทึกเป็น known limitation.
**Result**: 37/37 + 16/16 tests pass

### Fix 3 — ranging hard-block (Real-A only, env-driven)

**Symptom**: AEGIS ปล่อย BUY/SELL ใน regime=ranging ทั้งที่กฎเหล็กบอก "Ranging = พัก"
**Cause**: regime=ranging แค่ label ใน Signal (confidence multiplier ถูก disable) ไม่มี hard-block
  ที่ generator level — `REGIME_RANGING_CONFIDENCE_MULT = 1.0` (disabled)
**Fix**: `RANGING_HARD_BLOCK` module constant (env-driven) — เมื่อ True + regime=ranging +
  not learning_mode → return HOLD ที่ generator level (mirror `REGIME_VOLATILE_SKIP` pattern)
**Causal test**: `tests/test_regime_consistency.py::TestRangingHardBlockSwing` (5 tests)
  + `TestRangingHardBlockM5` (2 tests)
  - `test_ranging_hard_block_returns_hold` — block ON + ranging → HOLD with reason
  - `test_trending_not_blocked_when_hard_block_on` — trending ผ่านปกติ
  - `test_learning_mode_bypasses` — learning_mode bypass เก็บ ML data ต่อ
**Files**: `broky/signals/generator.py` (swing), `broky/signals/m5_scalp_generator.py` (m5)
**Env**: `RANGING_HARD_BLOCK=1` (Real-A only via oracle-engine docker-compose),
  default 0 for B/C/D (oracle-engine-train) เก็บ ML outcomes ต่อ
**Design choice**: module-level constant (not class attribute) เพราะ generator ใช้
  module-level functions ไม่ใช่ class. Per-container env separation พอ (oracle-engine รัน A
  เท่านั้น, oracle-engine-train รัน B/C/D) — ไม่ต้องใช้ `_A` suffix ใน generator
**Result**: 24/24 tests pass

## Deploy

- Commit: `f393b0c fix: min positions 2 + h4 fallback trend gate + ranging hard-block (Real-A)`
- Branch: `2026-07-01-live-trader-bugfix`
- Target: `oracle-engine` (Real-A) only — IRON LAW confirmation "I AM DEPLOYING TO REAL A"
- ML smoke tests: all passed (10 models, predictor healthy)
- Container: `oracle-engine` healthy, MT5 re-login successful (equity $374.25)
- Env verified in container: `MIN_POSITIONS_A=2`, `RANGING_HARD_BLOCK=1`
- B/C/D (`oracle-engine-train`) ไม่กระทบ — env defaults off

## Verification

### คาดหวังหลัง 1-2 ชม.

- `rejected_signals` table มี `ranging_hard_block` reason ปรากฏ (ถ้าตลาด ranging)
- `rejected_signals` มี `counter_trend_no_reversal:*` ที่อ้าง h4 (ไม่ใช่ d1)
- position limit log ขึ้น `max_positions=2` ไม่ใช่ `1/1`
- ML filter ยังทำงานปกติ (ไม่กระทบ)

### max_positions calculation หลัง deploy

equity $374.25, EQUITY_PER_POSITION_A=200, MAX_POSITIONS_A=3, MIN_POSITIONS_A=2:
`min(3, max(2, floor(374/200))) = min(3, max(2, 1)) = min(3, 2) = 2` ✅

## CPT Summary

ทั้ง 3 fixes ใช้ CPT (Causal Proof Testing):
- hypothesis: เขียนเป็น testable claim ("Bug occurs because [component] does [wrong behavior] when [condition]")
- RED: test fail ก่อน implement (ยืนยัน cause ผูกกับ symptom)
- GREEN: test ผ่านหลัง implement (ยืนยัน fix แก้ cause จริง)
- regression: 743/743 tests ผ่าน (ไม่มี side effect)

## Lessons

1. **Dead parameter คือ latent bug** — `h4_trend` อยู่ใน signature แต่ไม่ถูกใช้ ทำให้
   เชื่อว่า h4 มีผล ทั้งที่จริงไม่มี. Review every function parameter ว่าถูกใช้จริงไหม.
2. **Module-level env var พอสำหรับ per-container separation** — ถ้าแต่ละ container รัน
   process คนละตัว (oracle-engine vs oracle-engine-train) ไม่ต้องใช้ `_A` suffix ใน generator.
   Per-account suffix ใช้เฉพาะ trader (รันหลาย account ใน process เดียว).
3. **Cap ต้องเป็น hard ceiling เสมอ** — formula `min(cap, max(floor, calc))` ไม่ใช่
   `max(floor, min(cap, calc))` ไม่งั้น floor ละเมิด cap เมื่อ cap < floor.
4. **Ranging = พัก ต้องบังคับที่ generator** — label ใน Signal ไม่พอ. regime=ranging
   WR 41% ในประวัติ — block ดีกว่าเสี่ยง. #5552 (ranging SELL +$3.61) จะถูก block แต่
   ยอมรับเพราะกฎเหล็กชัดเจน.
5. **importlib.reload ไม่ใช่วิธี test module constant** — trigger re-registration ของ
   decorator-based registry. ใช้ `monkeypatch.setattr` บน module attribute แทน.

## Next Steps

- เก็บ post-deploy data 1-2 ชม. — query `rejected_signals` ดู `ranging_hard_block` และ
  `counter_trend_no_reversal:*_vs_*_h4` ปรากฏ
- ถ้า ranging block หนักมาก (block >50% ของ signals) อาจต้อง relax — แต่ถ้า WR ดีขึ้น
  หลัง deploy แปลว่ากฎเหล็กถูกต้อง
- ทดสอบ known limitation ของ Fix 2 (h4-counter reversal) — ถ้าเจอกรณีจริง อาจต้อง
  แก้ `compute_reversal_signal` ให้ไม่ short-circuit ตอน d1=unknown

## Related

- [[2026-07-09-real-a-performance-analysis]] — analysis ที่นำไปสู่ 3 fixes
- [[2026-07-02-counter-trend-gate-m5]] — m5 counter-trend gate (Fix 2 build on this)
- Plan: `~/.claude/plans/reflective-bouncing-comet.md`
- Commit: `f393b0c`