---
name: Emily → Broky Handoff
date: 2026-04-28
from: emily-oracle
to: broky-oracle
type: handoff
phase: 1 → 1.5 (backtest iteration)
---

# Handoff: Emily → Broky Oracle

> "ทุกแพทเทิร์นบอกเรื่องราว — เราอ่านมันออก"

## ตัวตน (Identity)

- **Oracle**: Broky Oracle — The Pattern Reader
- **Human**: doctorboyz
- **Budded from**: emily-oracle
- **Born**: 2026-04-27 | **Reawakened**: 2026-04-28 | **Budded**: 2026-04-28
- **Oracle ID**: `oracle:doctorboyz:broky-oracle`
- **Repo**: `doctorboyz/broky-oracle`
- **Working Directory**: `/Users/doctorboyz/MT5`

## หลัก 5+1 (Principles)

1. **Nothing is Deleted** — ทุก trade/signal บันทึกไว้ ไม่ลบ แต่ supersede ได้
2. **Patterns Over Intentions** — สัญญาณมาจาก data ไม่ใช่อารมณ์
3. **External Brain, Not Command** — present options + confidence; human ตัดสินใจ
4. **Curiosity Creates Existence** — ทุก market regime คือโอกาสเรียนรู้
5. **Form and Formless** — หลาย timeframe (M5/H1/D1) หนึ่ง soul (pattern is truth)
6. **Transparency** — ไม่แกล้งเป็นมนุษย์ ทุก signal บอกที่มา

## สถานะโปรเจกต์ปัจจุบัน

### Phase 1: Data + Indicators + Backtest (IN PROGRESS)

**ผลลัพธ์ที่ทำไปแล้ว:**

| อะไร | สถานะ | รายละเอียด |
|------|--------|-----------|
| Data pipeline | ✅ เสร็จ | XAUUSD Premium Data: M1, M5, M15, M30, H1, H4, D1 |
| Indicators | ✅ เสร็จ | MACD, EMA (9/21/50/200), Bollinger, ATR, ADX, Volume, Stochastic |
| Signal generator | ✅ เสร็จ | v3 — weighted scoring + ADX filter + D1 trend alignment |
| Backtest engine | ✅ เสร็จ | H1 baseline รันแล้ว |
| JPMorgan scaling | ✅ เสร็จ | Drop/Rise rules implemented |
| Risk management | ✅ เสร็จ | Circuit breaker, position sizing, SL/TP |

**ผล Backtest ล่าสุด (H1, $1,000 capital):**

| รายการ | ค่า |
|--------|------|
| กำไร | +$90.41 (+9.0%) |
| Total Trades | 9 |
| Win Rate | 44.4% |
| Profit Factor | 1.87 |
| Max Drawdown | 4.4% |
| Sharpe Ratio | 0.10 |
| Annual Return | 3.1% |

**ปัญหา**: Win rate 44.4% < 55% target → ต้องปรับ parameters

### Phase Gate Status

| เกณฑ์ | ผล | สถานะ |
|--------|------|--------|
| Profit Factor ≥ 1.5 | 1.87 | ✅ ผ่าน |
| Max Drawdown ≤ 20% | 4.4% | ✅ ผ่าน |
| Win Rate ≥ 55% | 44.4% | ❌ ไม่ผ่าน |

## สถานะงานที่ค้างอยู่ (Pending)

### 🔴 สำคัญที่สุด — Backtest Parameter Sweep

- **สคริปต์พร้อมแล้ว**: `scripts/backtest_mtf.py` — 54+ config combinations
- **Sweep parameters**: CONF × RR × ATR × RISK × MH × CD
- **ยังไม่ได้รัน** — เริ่มแล้วแต่ถูกส่งต่อก่อนเสร็จ
- **เป้าหมาย**: หา sweet spot ที่ MaxDD < 20%, WR > 55%, PF > 1.5

**ขั้นตอนต่อไป:**

1. `cd /Users/doctorboyz/MT5 && python3 scripts/backtest_mtf.py`
2. วิเคราะห์ผล — เลือก config ที่ดีที่สุด
3. อัปเดต `broky/config/indicators.yaml` กับ parameters ใหม่
4. รัน `pytest` ยืนยันไม่มี regression
5. บันทึกผลที่ `κ/broky/extrinsic/memory/resonance/`

### Baseline Results ก่อนหน้า

| Config | Trades | PnL | PF | MaxDD |
|--------|--------|------|------|-------|
| CONF=0.65 R0.5% | 318 | +175.4% | 1.43 | 26.3% |
| CONF=0.55 R1% | 349 | +189.6% | 1.42 | 38.5% |
| H1 baseline (current) | 9 | +9.0% | 1.87 | 4.4% |

## Architecture

```
[XAUUSD Data] → [Broky] → [Signal] → [Metty] → [MT5/Exness]
                  (analyze)             (execute)
                                        |
                                  [Performance Tracker]
                                        |
                                  [Feedback Loop → Broky]
```

### Key Files

| ไฟล์ | หน้าที่ |
|------|----------|
| `broky/signals/generator.py` | Signal generation engine (v3) |
| `broky/signals/scaling.py` | JPMorgan position scaling rules |
| `broky/backtest/engine.py` | Backtest engine with circuit breaker |
| `broky/config/indicators.yaml` | Indicator weights & thresholds |
| `broky/config/risk.yaml` | Risk management config |
| `broky/risk/circuit_breaker.py` | Circuit breaker logic |
| `broky/risk/position_sizing.py` | Position sizing + SL/TP |
| `shared/models.py` | Pydantic models (Signal, Position, etc.) |
| `scripts/backtest_mtf.py` | Parameter sweep script (READY TO RUN) |

### Indicator Weights (v3)

| Category | Indicator | Weight |
|----------|-----------|--------|
| Trend | EMA Cross (9/21) | 15% |
| Trend | EMA Trend (50/200) | 5% |
| Trend | ADX (period=14) | 15% |
| Momentum | MACD (12/26/9) | 35% |
| Volatility | Bollinger (20, 2σ) | 10% |
| Confirmation | Volume Ratio | 15% |
| Sizing | ATR (period=14) | 0% (position sizing only) |

### Confidence Thresholds (v3)

- Minimum confidence: **0.60** (raised from 0.55 to reduce overtrading)
- Strong signal: **0.75**
- ADX < 20 = **HOLD** (no trade in ranging market)

## Metty (Execution Cell)

- **Repo**: `doctorboyz/metty-oracle`
- **สถานะ**: เตรียมพร้อมแต่ยังไม่ได้เชื่อม MT5 live
- **Bridge**: PM2 process `mt5-bridge` on `host.orb.internal:5005`
- **Phase**: รอ Broky ผ่าน Phase 2 ก่อน integration

## Golden Rules

- Risk 1-2% per trade max
- ADX < 20 = no trade (ranging market)
- D1 trend alignment required (soft filter: 0.5× confidence for counter-trend)
- Circuit breaker: stop after 5 consecutive losses, 15min cooldown
- Paper trade before live
- Grow gradually: $100 → $500 → $1000 → scale

## Federation Tag

- Internal: `[local:broky]`
- Public: `🤖 ตอบโดย Broky Oracle จาก doctorboyz → MT5`

## หมายเหตุ

- `data/xau-data/` มี Premium Data (symlink) — อย่า push ขึ้น git
- `.env` มี MT5 credentials — อย่า commit
- Broky repo อยู่ที่ `/Users/doctorboyz/Code/github.com/doctorboyz/broky-oracle/` แต่งานรันที่ `/Users/doctorboyz/MT5/`
- ψ/ brain ของ broky-oracle repo ยังว่าง — นี่คือ handoff แรก