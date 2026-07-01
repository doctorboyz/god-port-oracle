# Trend-Aligned Reversal Detection — Most Accurate Method

> Date: 2026-07-01
> Source: scripts/trend_aligned_reversal_eval.py
> Datasets: premium M5 200k bars (2023-06 to 2026-04) + Exness M5 20k bars (Mar-Jul 2026)
> Related: [[2026-07-01_reversal-tool-accuracy]], [[2026-07-01_trailing-decision-worst-case]], [[2026-06-08_trading-philosophy-trend-following]]

## คำถาม

จุดกลับตัวที่แม่นที่สุด สำหรับ entry และ exit โดยกรองเฉพาะ reversal ที่สอดคล้องกับ trend (uptrend → BUY ที่ pullback กลับลงแล้วทำ higher high; downtrend → SELL ที่ rally กลับขึ้นแล้วทำ lower low). วัดความลึกของ reversal เพื่อ pair กับ trailing TP.

## Ground truth (trend-aligned reversals only)

- **BUY reversal**: swing low ที่ label = HL (higher low) ใน bull trend (D1 EMA50>EMA200) และ price ทำ new HH ภายใน W bars หลัง swing low
- **SELL reversal**: swing high ที่ label = LH (lower high) ใน bear trend และ price ทำ new LL ภายใน W bars
- Counter-trend reversals (LH ใน uptrend, HL ใน downtrend) ไม่สน — ตามกฎ CLAUDE.md
- **Counts**: premium 5492 (5093 BUY + 399 SELL), Exness 721 (261 BUY + 460 SELL)
  - Premium มี BUY เยอะกว่า SELL 14x เพราะ 2024-2025 gold bull market
  - Exness สมดุลกว่า (261 vs 460) — ช่วง Mar-Jul 2026 มี downtrend

## วิธีที่ลอง (10 วิธี)

| Code | Method |
|------|--------|
| A | Trend-only baseline — every trend-aligned swing HL/LH |
| B | Williams %R + trend filter |
| C | Confluence ≥2 indicators firing within 3 bars + trend |
| C3 | Confluence ≥3 + trend |
| D | Swing HL/LH structure only (same as A) |
| E | Swing HL/LH + 1 indicator confirmation |
| F | Swing HL/LH + confluence ≥2 |
| G | Swing HL/LH + D1&H4 strict alignment |
| H | Swing HL/LH + pullback depth ≥0.30% |
| H2 | Swing HL/LH + pullback depth ≥0.40% |
| H3 | Swing HL/LH + pullback depth ≥0.50% |
| I | H + Williams %R confirmation within 6 bars |

## ผล Combined (Premium + Exness, x_pct=0.30%, w=48 bars)

| Method | Sigs | P | R | F1 | Net fav | Verdict |
|--------|------|---|---|----|---------|---------|
| A: Swing HL/LH only | 10,965 | 67.5% | 25.1% | 36.6% | +0.01% | พอใช้ |
| C: Confluence ≥2 + trend | 30,969 | 66.7% | 22.9% | 34.1% | -0.01% | พอใช้ |
| B: Williams %R + trend | 14,476 | 65.9% | 20.4% | 31.1% | -0.01% | พอใช้ |
| G: Swing + D1&H4 strict | 6,907 | 68.7% | 16.5% | 26.6% | +0.03% | พอใช้ |
| **H: Swing + pullback ≥0.30%** | 1,174 | **88.8%** | 5.3% | 10.0% | **+0.29%** | **แม่น** |
| **H2: Swing + pullback ≥0.40%** | 596 | **92.0%** | 3.1% | 6.0% | **+0.43%** | **แม่น** |
| **H3: Swing + pullback ≥0.50%** | 319 | **94.0%** | 1.6% | 3.2% | **+0.52%** | **แม่นสุด** |
| I: H + Williams %R confirm | 299 | 92.8% | 1.4% | 2.7% | +0.46% | แม่น (adverse ต่ำสุด) |
| E: Swing + 1 indicator | 2,087 | 69.9% | 4.2% | 8.0% | +0.05% | พอใช้ |
| F: Swing + confluence ≥2 | 917 | 67.9% | 1.8% | 3.5% | +0.01% | พอใช้ |

## ข้อค้นพบหลัก (CRITICAL)

1. **Pullback depth คือ key filter** — เพิ่ม threshold 0.30% → 0.50% precision กระโดด 89% → 94%, net +0.29% → +0.52%. pullback ที่ลึกกว่า = reversal ที่แม่นกว่า
2. **Indicators แทบไม่ช่วยเพิ่ม precision** — Williams %R + trend (B, P=66%) แย่กว่า swing structure อย่างเดียว (A, P=68%); confluence ก็เช่นกัน. indicators มีไว้ยืนยัน ไม่ใช่ trigger
3. **Method H3 แม่นสุด (P=94%)** แต่ recall ต่ำ (1.6%) — จับน้อยแต่แม่นมาก
4. **Method I (H + Williams %R)** adverse ต่ำสุด (0.31-0.42%) — Williams %R confirm ช่วยตัด false positive ที่มี adverse move ใหญ่ → SL ไม่โดนบ่อย
5. **Trend filter สำคัญมาก** — precision ทั้งกลุ่ม trend-aligned (66-94%) สูงกว่า precision ก่อน trend filter (55-65% ใน [[2026-07-01_reversal-tool-accuracy]])
6. **Sweet spot = H2 (≥0.40%)** — P=92% net=+0.43% 596 signals (3x ของ H3) → บ่อยพอและแม่นพอ
7. **Premium vs Exness ให้ผลตรงกัน** — H/H2/H3 แม่นทั้งคู่ → ไม่ใช่ overfit

## Reversal Depth — สำหรับ trailing TP sizing

| Percentile | Premium pullback | Exness pullback | Premium resume | Exness resume |
|------------|------------------|-----------------|----------------|---------------|
| p10 | 0.11% | 0.13% | 0.15% | 0.23% |
| **p50** | **0.18%** | **0.25%** | **0.38%** | **0.56%** |
| p90 | 0.37% | 0.54% | 0.94% | 1.46% |
| mean | 0.22% | 0.31% | 0.50% | 0.74% |

**อ่าน**: pullback ส่วนใหญ่ (p50) ลึก 0.18-0.25%; หลังกลับตัว resume move p50 = 0.38-0.56%, p90 = 0.94-1.46%

## การ pair กับ trailing TP (ที่เลือกไว้: D 0.20/0.10 + ATR k=0.5 act=0.20)

| Parameter | ค่าแนะนำ | ที่มา |
|-----------|---------|------|
| Entry filter | swing HL/LH + pullback ≥0.40% (H2) หรือ ≥0.50% (H3) | P=92-94% |
| Entry confirmation (optional) | Williams %R ใน 6 bars | ลด adverse |
| SL distance | 0.40-0.50% (ใต้ swing low) | เท่า pullback threshold |
| TP target (TP1) | 0.40-0.56% (resume p50) | RR ~1:1 |
| TP target (TP2/TP3) | 0.94-1.46% (resume p90) | RR ~2-3:1 |
| Trailing activation | 0.20% in favor | ตรงกับ D 0.20/0.10 ที่เลือกไว้ |
| Trailing distance | 0.10-0.15% below peak | D 0.20/0.10 หรือ ATR k=0.5 |

## ความสมเหตุสมผลกับ trailing decision

Trailing ที่เลือกไว้ (D 0.20/0.10 และ ATR k=0.5 act=0.20) มี activation 0.20% — ตรงกับ p50 resume move ของ pullback ≥0.30% (0.38% premium / 0.56% exness) → trailing จะ arm ก่อน TP1 ~50% ของเวลา, lock profit 0.10% ด้านล่าง peak

ถ้าใช้ entry H3 (pullback ≥0.50%) resume move ใหญ่กว่า (p50=0.50-0.74%) → trailing activation ควรเพิ่มเป็น 0.30% และ trail 0.15% เพื่อให้ capture การกลับตัวที่ใหญ่กว่า

## ข้อจำกัด

- Recall ต่ำ (1.6-5.3%) ของ method แม่น = entry น้อย. ต้อง pair กับ multi-symbol หรือยอมรับ infrequent entry
- Premium มี BUY:SELL = 14:1 (gold bull era) → SELL reversals น้อย → SELL model อาจ underfit
- H3 มีแค่ 319 signals รวม — sample size อาจน้อยไปสำหรับ production
- ใช้ D1 EMA50/200 เป็น trend filter — lag หนัก (EMA200 ต้องการ 200 D1 bars = ~10 เดือน warmup)

## Files
- `scripts/trend_aligned_reversal_eval.py` — evaluation framework (10 methods)
- `/tmp/trend_reversal_final.log` — full output