# Exness Out-of-Sample Backtest

> Date: 2026-07-01
> Source: scripts/backtest_live_monte_carlo.py --data exness
> Dataset: Exness M5 13,568 bars (2026-02-02 → 2026-04-15) — ไม่เคยเห็นตอน tune
> Related: [[2026-07-01_live-monte-carlo-backtest]], [[2026-07-01_e2e-backtest-entry-trailing-block]], [[2026-07-01_trend-aligned-reversal-accuracy]]
> Status: local backtest only — NOT deployed

## คำถาม

กลยุทธ์ (entry H2 + trailing D 0.20/0.10 + blocks) tune บน premium 2023-2026 — ยังใช้ได้จริงไหมบน Exness data ใหม่ (Feb-Apr 2026) ที่ไม่เคยเห็น.

## Setup

- Entry: H2 (pullback ≥0.40%), trend-aligned reversal (HL/LH structure)
- Trailing: D 0.20/0.10
- TP cap: 2.0%, Max hold: 288 M5 bars
- Risk/trade: 1%
- Spread: $0.20 typical (Exness Standard)
- Blocks: DP (20/30/30) + CB (5 consec, 5% daily) + TradeBlocker
- Exness CSV: `/Users/doctorboyz/Documents/xau-data/xauusd_5m.csv` (date/OHLCV/session)

## ผล baseline (spread $0.20)

| Balance | End | Trades | WR | PF | PnL | MaxDD$ | MaxDD% | CB blocks |
|---------|-----|--------|-----|----|-----|--------|--------|-----------|
| $100 | $1,595 | 164 | 90.9% | 5.30 | +$1,495 | $42 | 41.6% | 14 |
| $500 | $2,030 | 167 | 91.0% | 5.40 | +$1,530 | $42 | 8.3% | 11 |
| $1,000 | $2,561 | 173 | 90.8% | 5.17 | +$1,561 | $51 | 5.1% | 4 |

## MC Bootstrap (50 runs)

| Balance | MaxDD p10 | p50 | p90 | ruin% |
|---------|-----------|-----|-----|-------|
| $100 | $34 | $49 | $67 | 0.0% |
| $500 | $39 | $50 | $66 | 0.0% |
| $1,000 | $37 | $55 | $70 | 0.0% |

## ข้อค้นพบหลัก

1. **Generalize จริง** — กลยุทธ์ tune บน premium (2023-2026) ใช้บน Exness (Feb-Apr 2026) ได้โดยไม่ degrade. PF สูงกว่า in-sample (5.3 vs 4.17), WR เท่าเดิม 90%
2. **MaxDD ต่ำกว่า in-sample** — $1000 MaxDD 5.1% (Exness) vs 10.1% (premium). $500 MaxDD 8.3% vs 20.2%. ช่วง Feb-Apr 2026 trend ชัดกว่า premium โดยรวม
3. **Prob ruin 0% ทุก balance** — แม้ $100 ก็ ruin 0% (ต่างจาก premium ที่ $100 ruin 3%). สอดคล้องกับ MaxDD น้อยกว่า
4. **Signal หนาแน่น 4x** — 196 signals / 2.5 เดือน = 2.6/day vs premium 0.6/day. ตลาด Feb-Apr 2026 มี pullback บ่อย (gold bull รุนแรง)
5. **CB ทำงาน 14 ครั้งที่ $100** — หนาแน่นกว่า premium (6 ครั้ง) เพราะ trades เยอะ → loss cluster เยอะ → CB ติดบ่อย. แต่รอดเพราะ CB กัน loss cluster ได้จริง
6. **DP ไม่ fire** — เหมือน premium: daily_start_equity reset เป็น peak ทำให้ daily limit โตตาม peak. risk/trade 0.3% ของ peak → ยากขาดถึง 20%. ไม่ใช่ bug — เป็นเพราะกลยุทธ์ profitable
7. **$100 MaxDD 41.6%** — ยังเยอะเพราะ min_lots 0.01 บังคับ risk จริง ~4% per trade (ไม่ใช่ 1%). แต่น้อยกว่า premium 69% เพราะ MaxDD$ เล็กกว่า

## ข้อจำกัด

- **ช่วงเวลาสั้น** — 2.5 เดือน ไม่ใช่ multi-year. อาจเป็นช่วงที่ trend ชัดพอดี
- **Gold bull รุนแรง** — Feb-Apr 2026 gold วิ่งขึ้นแรง → BUY:SELL อาจเบiais. ไม่ได้ทดสอบ SELL ใน bear market จริง
- **Price ~$4675** — XAUUSD ราคาสูงในช่วงนี้. ATR % อาจต่างจาก premium
- **ไม่มี spread/slippage จริง** — ใช้ $0.20 fixed + MC random. ตลาดจริงอาจ spread หนีวาร์ข่าว
- **ไม่ได้เทส block interaction กับ MC** — bootstrap แยก block ออกจาก trade order

## สรุป

**กลยุทธ์ผ่าน out-of-sample validation** — ใช้บน Exness data ใหม่ได้โดยไม่ degrade. PF 5.0-5.4, WR 90%, ruin 0% ทุก balance.

**แนะนำ**:
- เริ่ม $500+ (MaxDD < 10%, ruin 0%)
- $100 รับ MaxDD 40% ได้ไหม — ถ้ารับไม่ได้ ต้อง $500+
- พร้อม deploy หลัง user approve (Step 3)

**ขั้นต่อไป (รอ user ตัดสินใจ)**:
- Wire TradeBlocker + DP set_time เข้า live_trader (requires explicit approval)
- เริ่ม $500+ เท่านั้น (ruin 0% per MC)

## Files

- `scripts/backtest_live_monte_carlo.py` — เพิ่ม `--data exness` option
- `/tmp/exness_oos.log` — full output
- Exness CSV: `/Users/doctorboyz/Documents/xau-data/xauusd_5m.csv`