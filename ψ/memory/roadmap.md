# God Port Trading — Roadmap

> Set: 2026-05-21 09:17 | Previous: _(first roadmap)_

## Identity
- **Oracle**: God Port Trading
- **Human**: doctorboyz
- **Born**: 2026-04-29
- **Theme**: 🔮 Pattern is truth, execution is faithful — from signal to reality in one soul

## Active Goals

### G-001: ML-Driven Strategy Optimization
- **Status**: in_progress (O-1 through O-6 done, targets not yet met)
- **Priority**: critical
- **Phase**: mid-term (2-4 weeks)
- **Definition of Done**:
  - Win rate เพิ่มจาก 53% → 58-65%
  - Max drawdown ลดจาก 11.8% → <8%
  - Profit factor เพิ่มจาก 1.64 → 1.8-2.2
  - Trades ต่อวันน้อยลงแต่คุณภาพสูงขึ้น (filter สัญญาณคุณภาพต่ำออก)
  - โมเดล XGBoost v3 เทรนด้วยข้อมูลจริงและใช้ใน production

#### Objectives

| ID | Objective | Status | Evidence | Updated |
|----|-----------|--------|----------|---------|
| O-1 | แก้ M1 scalp candle data — `data/xau-data/` ไม่มีบน VPS ทำให้ M1 scalp ทั้ง 3 accounts เจอ error | completed | retry logic + CSV fallback มาแล้ว, bridge fetch ทำงาน | 2026-05-21 |
| O-2 | สร้างตาราง `trade_outcomes` ใน oracle.db — schema สำหรับ ML training | completed | schema deployed, table มี 2,078 rows | 2026-05-21 |
| O-3 | Backfill trade outcomes จาก `live_trades` 2,086 รายการ + `signals` 14,330 รายการ | completed | 1,741 rows มี features_json (84% match rate) | 2026-05-21 |
| O-4 | เทรน XGBoost v3 ด้วยข้อมูล trade outcomes จริง | completed | 9 โมเดลเทรนด้วย TimeSeriesSplit CV (54.7% overall accuracy) | 2026-05-21 |
| O-5 | เพิ่ม ML filter ใน M5 scalp และ M1 scalp — ก่อนเปิดเทรดให้โมเดลประเมินคุณภาพสัญญาณ | completed | TradeOutcomePredictor + integration ในทั้ง M5ScalpTrader และ ScalpTrader, off by default (ML_FILTER_ENABLED=1 to enable) | 2026-05-21 |
| O-6 | ทดสอบระบบ — normal case (สัญญาณผ่าน filter), edge case (สัญญาณไม่ผ่าน, โมเดล unavailable, cold start) | completed | 15 tests passed (tests/test_ml_predictor.py), predictor verified on VPS | 2026-05-21 |

#### Audit Trail
- 2026-05-21 09:17 — roadmap created from ML training analysis — status: pending
- 2026-05-21 09:48 — O-1 through O-4 completed; O-4 training results: CV=54.7%, test=49.3% (below target 58-65% WR)
- 2026-05-21 09:48 — Reality check: features alone can't predict trade outcomes with current accuracy; ML filter should reject worst trades (high-confidence LOSS predictions) rather than pick winners
- 2026-05-21 10:04 — O-5, O-6 completed: ML filter integrated into both scalpers, 15 tests passed, predictor tested on VPS with 9 models loaded
- 2026-05-21 10:04 — ML filter strategy: opt-in via ML_FILTER_ENABLED=1, uses regime×direction specific models with fallback chain, P(LOSS) > 65% rejection threshold

#### Blockers
- ~~[RESOURCE] `data/xau-data/` ไม่มีบน VPS~~ — แก้แล้ว
- ~~[DEPENDENCY] ตาราง `trade_outcomes` ยังไม่มี~~ — แก้แล้ว
- ~~[DEPENDENCY] ต้องมี trade_outcomes ก่อนเทรนโมเดล~~ — แก้แล้ว
- [CAPABILITY] Features ปัจจุบันทำนาย trade outcome ได้ต่ำกว่าเป้า (50% accuracy vs 58-65% target) — แนะนำเก็บข้อมูลเพิ่ม 1-2 เดือนแล้ว re-train

---

### G-002: System Stability & Data Pipeline
- **Status**: in_progress
- **Priority**: high
- **Phase**: short-term (1-2 weeks)
- **Definition of Done**:
  - M1 scalp ทั้ง 3 accounts (A, B, C) ทำงานได้ไม่มี error
  - M5 scalp เปิดเทรดได้เมื่อไม่มี position ค้าง
  - Trade execution บันทึกผลลง database ครบถ้วน (signal_id linking)
  - ไม่มี `stream has been closed` หรือ `pickling is disabled` error

#### Objectives

| ID | Objective | Status | Evidence | Updated |
|----|-----------|--------|----------|---------|
| O-1 | แก้ไข M1 candle data source — ใช้ MT5 bridge ดึง M1 candles แทน CSV fallback | completed | retry logic + bridge fetch ใน _fetch_candles, fallback to CSV เท่านั้นเมื่อ bridge ล้มเหลวจริง | 2026-05-21 |
| O-2 | เพิ่ม `data/xau-data/` ใน Docker volume หรือเพิ่ม candle cache mechanism | completed | M1 CSV copied to /app/data/xau-data/ on VPS via oracle-data volume | 2026-05-21 |
| O-3 | ตรวจสอบว่า `live_trades` sync กับ MT5 ถูกต้อง — trade_id ไม่ซ้ำ, profit/loss ตรง | pending | — | 2026-05-21 |

#### Audit Trail
- 2026-05-21 09:17 — roadmap created — status: pending
- 2026-05-21 10:04 — O-1, O-2 completed; M1 scalp working with bridge+CSV fallback

#### Blockers
- ~~[RESOURCE] M1 scalp CSV path `data/xau-data/` ไม่ถูก mount ใน Docker container~~ — แก้แล้ว

---

## Completed Goals
_(none yet)_

## Archived Roadmaps
_(none yet)_
