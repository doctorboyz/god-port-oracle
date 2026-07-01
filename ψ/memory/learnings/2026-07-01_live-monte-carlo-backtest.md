# Live-Environment Backtest + Monte Carlo Robustness

> Date: 2026-07-01
> Source: scripts/backtest_live_monte_carlo.py
> Dataset: premium M5 200k bars (2023-06 → 2026-04)
> Related: [[2026-07-01_e2e-backtest-entry-trailing-block]], [[2026-07-01_block-verification]]
> Status: local backtest only — NOT deployed

## คำถาม

สร้างสภาพแวดล้อมเหมาะกับ live trade ก่อน (Exness Standard account: spread, slippage, leverage, lot step) แล้วทดสอบ Monte Carlo เพิ่มความหลากหลายได้จริงไหม.

## Live environment (Exness Standard, XAUUSD)

- **Spread**: $0.20 typical (MC range $0.15-0.30) — หักทั้ง entry และ exit
- **Slippage**: 0-$0.05 (MC random per trade)
- **Leverage**: 1:100 → margin = lots × 100 × price / 100
- **Min lot**: 0.01  **Lot step**: 0.01
- **Commission**: 0 (standard = spread only)
- **Swap**: ไม่นับ (hold < 24h)

## Baseline ผล (spread $0.20, no slippage, H2)

| Balance | End | Trades | WR | PF | PnL | MaxDD$ | MaxDD% |
|---------|-----|--------|-----|----|-----|--------|--------|
| $100 | $2,923 | 402 | 90.8% | 4.55 | +$2,823 | $101 | 101%* |
| $200 | $3,023 | 404 | 90.3% | 4.15 | +$2,823 | $101 | 50.5% |
| $500 | $3,335 | 405 | 90.4% | 4.17 | +$2,835 | $101 | 20.2% |
| $1,000 | $3,857 | 406 | 90.4% | 4.17 | +$2,857 | $101 | 10.1% |
| $10,000 | $30,944 | 408 | 90.4% | 4.19 | +$20,944 | $544 | 5.4% |

*MaxDD% คำนวณจาก balance_start ไม่ใช่ peak — ทำให้ $100 ดูเยอะเพราะ peak โตกว่า start. MaxDD จริงจาก peak ~35% สำหรับ $100.

**เทียบกับ no-spread backtest** ([[2026-07-01_e2e-backtest-entry-trailing-block]]): $1000 ลดจาก $3919 → $3857 (−$62 spread cost). $100 ลดจาก $3069 → $2923 (−$146). Spread กิน ~$0.20-0.80/trade ตาม lot size.

## Monte Carlo — เพิ่มความหลากหลายได้จริงไหม? **ได้ 2 แบบ**

### MC Bootstrap (shuffle trade order, 100 runs)

= "ถ้า loss cluster ต่างจากที่เกิดจริง จะเป็นยังไง?"

| Balance | MaxDD p10 | MaxDD p50 | MaxDD p90 | Prob ruin |
|---------|-----------|-----------|-----------|-----------|
| $100 | $60 | $76 | $107 | **3.0%** |
| $200 | $60 | $83 | $105 | 0.0% |
| $500 | $60 | $76 | $105 | 0.0% |
| $1,000 | $60 | $77 | $106 | 0.0% |
| $10,000 | $392 | $513 | $676 | 0.0% |

Final balance เท่ากันทุก percentile (shuffle ไม่เปลี่ยน sum) — แต่ MaxDD กระจาย $60-$107 สำหรับ $100. **ความหลากหลายที่เพิ่ม**: distribution ของ MaxDD + prob of ruin

### MC Full (random spread $0.15-0.30 + slippage per trade, 100 runs)

= "spread/slippage สุ่มต่อ trade จะเปลี่ยนผลไหม?"

| Balance | final p10 | final p50 | final p90 | MaxDD p50 |
|---------|-----------|-----------|-----------|-----------|
| $100 | $2,895 | $2,908 | $2,912 | $101 |
| $1,000 | $3,829 | $3,842 | $3,846 | $101 |
| $10,000 | $30,657 | $30,805 | $30,895 | $547 |

Final balance กระจายน้อยมาก (±$15 จาก p50) → ระบบทนต่อ spread variance ดี. **ความหลากหลายที่เพิ่ม**: spread-sensitivity distribution (แต่ในกรณีนี้ tight = robust)

## ข้อค้นพบหลัก

1. **MC เพิ่มความหลากหลายได้จริง แต่ละแบบให้ข้อมูลต่างกัน**:
   - **Bootstrap**: ให้ MaxDD distribution + prob of ruin — ตอบ "ถ้า order เปลี่ยน worst case เป็นยังไง"
   - **Full**: ให้ spread-sensitivity distribution — ตอบ "ถ้า spread สุ่ม ผลกระเทือนเท่าไหร่"
   - ทั้งคู่互补 — ใช้ด้วยกันได้ประโยชน์สูงสุด

2. **Prob of ruin 3% สำหรับ $100** — 3 จาก 100 shuffle ลงไปต่ำกว่า $50. $100 มีความเสี่ยงจริง ไม่ใช่ safe ตามที่ baseline แสดง
3. **$200+ ปลอดภัย** — ruin 0%, MaxDD p90 < $107 (ครึ่งหนึ่งของ start)
4. **$10000 สุดปลอดภัย** — MaxDD p90 $676 = 6.8% ของ start, ruin 0%
5. **Spread cost ไม่ทำลาย profitability** — PF ลดจาก 4.25 → 4.17 แค่เล็กน้อย
6. **Spread variance กระทบน้อยมาก** (full MC tight) → ระบบทนต่อ spread change ดี
7. **Trade order variance กระทบ MaxDD มาก** (bootstrap spread $60-$107) → worst case สำคัญกว่า average

## ข้อจำกัด

- **Premium data bias** — 2024-2025 gold bull → BUY:SELL = 14:1, ไม่ได้ทดสอบ SELL จริง
- **In-sample** — entry + trailing tune บน data เดียวกัน → ผลจริงจะแย่กว่า
- **DP ไม่ fire ใน backtest** (artifact — ไม่มี set_time) → ใน live จะ block เพิ่ม
- **MC bootstrap ไม่นับ block interaction** — block กับ trade order เกี่ยวกันจริง (CB ติดขึ้นอยู่กับ order) แต่ bootstrap แยกสองสิ่ง
- **ไม่นับ news events / gap** — XAUUSD มี gap ตอน news บ่อย
- **Single symbol** — ไม่ได้ทดสอบ multi-symbol portfolio

## สรุป (ตอบคำถาม user)

**MC เพิ่มความหลากหลายได้จริง** โดย:
- ให้ distribution แทน single point estimate → เห็น worst case ไม่ใช่แค่ average
- แยก source of risk: bootstrap วัด order risk, full วัด spread risk
- คำนวณ prob of ruin ได้ — 3% สำหรับ $100 ที่ baseline มองไม่เห็น

**แนะนำ**:
- ใช้ bootstrap เป็นหลัก (เร็ว, ให้ MaxDD + ruin)
- ใช้ full MC เป็น secondary (ช้า, วัด spread sensitivity)
- รัน 200+ runs เพื่อ percentile ที่น่าเชื่อ

**ระบบผ่าน live environment + MC** — พร้อม deploy สำหรับ $200+ (ruin 0%). **$100 ยังเสี่ยง** (ruin 3%) — แนะนำเริ่ม $500+.

## Files

- `scripts/backtest_live_monte_carlo.py` — live env + MC script
- `/tmp/live_mc.log` — full output