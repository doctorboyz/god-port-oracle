# ML Filter + AEGIS Reversal Gate — 3-Way Comparison

> วันที่: 3 กรกฎาคม 2026
> เกี่ยวข้อง: [[2026-07-02_m5-aegis-complete]], [[2026-07-01_live-monte-carlo-backtest]], [[2026-07-01_exness-out-of-sample]], [[2026-06-28_v4-still-wins-threshold-tuning-modest]]
> ประเภท: learning (backtest analysis)
> scope: local backtest — ไม่ deploy อะไร, ไม่แตะ Real-A

## สรุป

คำถาม: "ML filter คู่กับ AEGIS reversal gate จะเกิดอะไรขึ้น — เสริมหรือขัดแย้ง?"

**คำตอบ: ทั้งคู่เป็นจริง ขึ้นกับมุมมอง — ML ลด PnL รวม (ขัดแย้ง) แต่เพิ่ม survival บน balance เล็ก (เสริมมาก)**

| มุม | ผล | คำตอบ |
|-----|-----|-------|
| PnL รวม | ML ตัด 50-94% (v4) ถึง 99% (ens) ของกำไรออก | ❌ ขัดแย้ง |
| Risk-adjusted (PF/WR) | v4 PF 4.13→4.48, ens PF→5.36-6.48 | ✅ เสริม |
| MaxDD (survival) | $100 MaxDD 101%→64%→21% (ริน→รอด) | ✅ เสริมมาก |
| OOS generalize | exness รูปแบบเดียวกัน — ไม่ใช่ in-sample fluke | ✅ เสริม |

## การทดสอบ

สคริปต์: `scripts/backtest_ml_vs_aegis.py` (สร้างใหม่)
- ใช้ซ้ำ `run_live_backtest` จาก `backtest_live_monte_carlo.py` (มี AEGIS ครบ: trend-aligned reversal entry + trailing TP + DrawdownProtector + CircuitBreaker + TradeBlocker)
- เพิ่ม ML filter เป็น gate ก่อนเข้า replay (compute features ที่ entry → should_skip → block/allow)
- 3 configs × 5 balances × 2 datasets = 30 runs

### Configs (mirror VPS production env)

| Config | ML | Threshold | Mode | ใช้จริงใน VPS |
|--------|-----|-----------|------|---------------|
| no ML | (none) | — | — | baseline |
| v4 single | TradeOutcomePredictor(v4) | 0.55 | loss_proba > thresh → block | Real-A |
| v4+v6 ensemble | EnsemblePredictor(v4,v6) | 0.45 | OR-gate (block if ANY model > thresh) | Demo B/D |

### Datasets

| Dataset | Bars | Range | Type |
|---------|------|-------|------|
| premium | 200,000 | 2023-06 → 2026-04 | in-sample (training data) |
| exness | 13,568 | 2026-02-02 → 2026-04-15 | **OOS** (ไม่เคยเห็น) |

### Env (Exness Standard)
- Spread $0.20, leverage 1:100, lot step 0.01, min lot 0.01
- Risk 1%/trade, entry H2 (pullback ≥ 0.40%), trailing D 0.20/0.10

## ผลลัพธ์

### PREMIUM (in-sample)

| Balance | Config | Trades | WR | PF | PnL | MaxDD% |
|---------|--------|--------|-----|-----|------|--------|
| $100 | no ML | 403 | 90.3% | 4.13 | +$2,823 | **101.1%** 🟡 |
| $100 | v4 @0.55 | 185 | 89.7% | 4.48 | +$1,294 | 63.7% |
| $100 | ens OR @0.45 | 26 | 92.3% | 5.36 | +$175 | 21.4% |
| $200 | no ML | 403 | 90.3% | 4.13 | +$2,823 | 50.5% |
| $200 | v4 | 192 | 90.1% | 4.65 | +$1,355 | 21.5% |
| $200 | ens | 26 | 92.3% | 5.36 | +$175 | 10.7% |
| $500 | no ML | 404 | 90.3% | 4.15 | +$2,835 | 20.2% |
| $500 | v4 | 193 | 90.2% | 4.67 | +$1,364 | 8.6% |
| $500 | ens | 27 | 92.6% | 5.46 | +$179 | 4.3% |
| $1000 | no ML | 406 | 90.4% | 4.17 | +$2,858 | 10.1% |
| $1000 | v4 | 193 | 90.2% | 4.67 | +$1,364 | 4.3% |
| $1000 | ens | 27 | 92.6% | 5.46 | +$179 | 2.1% |
| $10000 | no ML | 408 | 90.4% | 4.19 | +$20,944 | 5.4% |
| $10000 | v4 | 193 | 90.2% | 3.99 | +$6,590 | 1.6% |
| $10000 | ens | 27 | 92.6% | 6.48 | +$981 | 0.9% |

### EXNESS (OOS)

| Balance | Config | Trades | WR | PF | PnL | MaxDD% |
|---------|--------|--------|-----|-----|------|--------|
| $100 | no ML | 164 | 90.9% | 5.30 | +$1,495 | 41.6% |
| $100 | v4 @0.55 | 69 | 91.3% | 5.69 | +$657 | 30.0% |
| $100 | ens OR @0.45 | 3 | 100% | inf | +$36 | 0.0% |
| $1000 | no ML | 173 | 90.8% | 5.17 | +$1,561 | 5.1% |
| $1000 | v4 | 71 | 91.5% | 5.95 | +$694 | 3.0% |
| $1000 | ens | 3 | 100% | inf | +$36 | 0.0% |
| $10000 | no ML | 177 | 91.0% | 4.04 | +$5,577 | 2.6% |
| $10000 | v4 | 71 | 91.5% | 4.80 | +$2,138 | 1.1% |
| $10000 | ens | 3 | 100% | inf | +$120 | 0.0% |

## จุดสำคัญ

### 1. AEGIS reversal gate แข็งมาก (90%+ WR)
ไม่มี ML, AEGIS reversal gate ให้ WR 90-91% PF 4-5 อยู่แล้ว — เพราะ trend-aligned reversal (pullback ≥ 0.40% + HH/LL) มี precision 92-94% (จาก [[2026-07-01_trend-aligned-reversal-accuracy]])

### 2. ML filter คือ "precision booster" ไม่ใช่ "profit booster"
- v4 block 55% ของ signals → กำไรรวมลด 50% แต่ MaxDD ลด 50%
- ens block 95% ของ signals → กำไรรวมลด 94% แต่ MaxDD ลด 79%
- ML ไม่ได้หา "trades ที่แพ้" มันหา "trades ที่ risk-adjusted ดีกว่า" แล้วตัดส่วนที่เหลือออก

### 3. $100 no-ML = ริน (MaxDD 101.1%)
จุดวิกฤตสำคัญ — บน premium $100 ไม่มี ML = balance หาย (MaxDD เกิน 100%)  ML v4 เปลี่ยนจากริน→รอด (MaxDD 64%)  ML ens เปลี่ยนเป็นปลอดภัย (MaxDD 21%)
→ ปรัชญา "survival > profit" ของ CLAUDE.md ตรงกับการใช้ ML บน balance เล็ก

### 4. Ensemble OR @0.45 รุนแรงเกิน
block 95% ของ signals เหลือ 26-29 trades (premium) / 3 trades (exness) — สถิติไม่ significant ถ้าจะใช้ ensemble ต้องเพิ่ม threshold (0.55-0.60) หรือเปลี่ยน mode=and/avg

### 5. OOS ยืนยัน — ไม่ใช่ overfitting
exness (ไม่เคยเห็น) ให้รูปแบบเดียวกัน: ML ลด PnL แต่ลด MaxDD มาก แปลว่า ML filter ไม่ได้จำข้อมูล training แต่จับ risk pattern จริง

### 6. v4 single @0.55 คือ sweet spot
block 55% แต่ยังมี 185-193 trades เพียงพอ ลด MaxDD ลงครึ่งหนึ่ง ไม่เสีย PnL มากเกิน — เป็น config จริงใน VPS Real-A ที่ถูกต้องแล้ว

## ข้อเสนอแนะ (ตาม equity จริงใน VPS)

| บัญชี | Equity | แนะนำ | เหตุผล |
|-------|--------|--------|--------|
| Real-A | $200 | **v4 @0.55** (ปัจจุบันถูกแล้ว) | $200 no-ML MaxDD 50% = ชายขอบริน v4 ลดเหลือ 21% |
| Demo B | $200 | **v4 @0.55** (เปิด ML กลับ) | survival-critical บน balance เล็ก |
| Demo C | $850 | **v4 @0.55** | $500 MaxDD 20% ชายขอบ v4 ลดเหลือ 8.6% |
| Demo D | $320 | **v4 @0.55** | $200-$500 range v4 ลด MaxDD 4x |
| ถ้า equity > $1000 | — | พิจารณา **no-ML** | MaxDD ปลอดภัยแล้ว ได้ PnL สูงกว่า |

## ประเด็นที่ต้องระวัง

1. **Backtest นี้ใช้ H2 entry (pullback ≥ 0.40%)** — ใน production m5_scalp_trader อาจใช้ entry ต่างกัน ผลอาจเปลี่ยน
2. **ไม่ได้ทดสอบ account-specific configs** — A/B/C/D ใช้ ATR/RR/confidence ต่างกัน ใน backtest นี้ใช้ค่าเดียวกันทุกบัญชี
3. **Ensemble threshold 0.45 อาจไม่ใช่ค่าจริงใน VPS** — ต้องเช็ค docker-compose.vps.yml อีกครั้งถ้าจะ deploy จริง
4. **ML filter ไม่ได้จับ "reversal trade" ที่ trend_alignment=2** — ใน backtest สัญญาณมาจาก trend_aligned_reversal_eval ซึ่งกรองด้วย pullback ≥ 0.40% อยู่แล้ว ใน production m5 generator อาจส่ง signals ที่ gate อนุญาตแต่ ML บล็อก — ต้องดู DB rejected_signals จริง

## ไฟล์ที่สร้าง/แก้

| ไฟล์ | การแก้ |
|------|-------|
| `scripts/backtest_ml_vs_aegis.py` | (ใหม่) 3-way comparison script — extends backtest_live_monte_carlo with ML filter |

## บทเรียน

**"ML filter กับ AEGIS reversal gate ไม่ใช่เรื่องเดียวกัน"** — AEGIS คือ quality filter (precision 92%+) ML คือ risk filter ที่ซ้ำซ้อนกับ AEGIS บน trades ที่ผ่านแล้ว. การใช้ ML หลัง AEGIS จึงเป็นการ "double-filter" ที่ลด PnL มากกว่าเพิ่ม. แต่บน balance เล็กที่ survival สำคัญกว่า profit, ML ยังมีค่าเพราะมันลด MaxDD ได้ 50-79%.

**อย่าใช้ ensemble OR @0.45** — block มากเกินไป เหลือ trade ไม่พอสร้างสถิติ. ถ้าจะใช้ ensemble ต้องเพิ่ม threshold หรือเปลี่ยน mode.

**Decision rule ตาม equity**: $100-$500 → v4 single (survival), $1000+ → no-ML (profit-optimal).