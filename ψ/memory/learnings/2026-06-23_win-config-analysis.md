---
name: win-config-analysis
description: Winning V4 configuration analysis — good era vs broken era, key parameters, and recommendations
metadata:
  type: project
---

# Win Config Analysis — V4 Good Era vs Broken Era

**Date**: 2026-06-23
**Purpose**: เปรียบเทียบ configuration ที่ทำกำไร (ก่อน Jun 15) กับที่ขาดทุน (หลัง Jun 15) เพื่อศึกษาว่าอะไรทำงาน อะไรไม่ทำงาน

---

## 1. สรุปด่วน

| Metric | Good Era (ก่อน Jun 15) | Broken Era (Jun 15+) |
|--------|------------------------|----------------------|
| **Trades** | 938 | 100 |
| **Win Rate** | 43% | 33% |
| **PnL** | **+$1,997** | **-$179** |
| **BUY WR** | 45% (235/526) | 36% (10/28) |
| **SELL WR** | 40% (166/412) | 32% (23/72) |
| **Profitable Days** | 19/34 (56%) | 2/5 (40%) |
| **Avg trades/day** | 27.6 | 20 |
| **Avg lot size** | 0.013 (BUY), 0.012 (SELL) | similar |

**Key insight**: WR ตกจาก 43% → 33% แค่ 10% แต่ PnL กลับจาก +$1,997 เป็น -$179 เพราะ **risk management พัง** (LEARNING_MODE=1 ข้ามทุก check) + **sync_pnl_from_db bug** ทำให้ drawdown protection ไม่ทำงาน

---

## 2. สิ่งที่เปลี่ยนไประหว่าง Good Era → Broken Era

| # | การเปลี่ยนแปลง | ผลกระทบ | แก้แล้ว? |
|---|----------------|----------|----------|
| 1 | **LEARNING_MODE=1** ตั้งใน .env | ข้าม circuit breaker, ML filter, max positions → ทำให้เทรดเยอะเกิน ขาดทุนเยอะเกิน | ✅ เปลี่ยนเป็น 0 |
| 2 | **sync_pnl_from_db bug** | DrawdownProtector ไม่รู้ว่าขาดทุน → ไม่หยุดเทรดต่อ | ✅ เพิ่ม method ใน VPS |
| 3 | **get_pnl_summary missing** | sync_pnl_from_db เรียก function ที่ไม่มี → crash | ✅ เพิ่ม function ใน VPS |
| 4 | **Regime filters เพิ่ม** (volatile=skip, ranging=x0.3) | ลดสัญญาณในบางช่วง | ✅ ยังอยู่ แต่ไม่ใช่สาเหตุหลัก |
| 5 | **Counter-trend penalty** (trend_alignment=-1 → x0.5) | ลด confidence สำหรับ counter-trend trades | ✅ ยังอยู่ แต่ไม่ใช่สาเหตุหลัก |
| 6 | **BUY min confidence ขึ้นเป็น 0.50** | ลด BUY trades ที่เข้า | ✅ ยังอยู่ |
| 7 | **Account D เพิ่ม** | เพิ่ม account ทดสอบ | ✅ ไม่กระทบ A |
| 8 | **VPS code ไม่ sync กับ local** | Bug หลายตัวไม่ได้ deploy | ✅ แก้โดย scp + rebuild |

**Root cause หลัก**: LEARNING_MODE=1 + sync_pnl_from_db bug ทำให้ risk management พังทั้งระบบ

---

## 3. V4 Model — Sub-model Performance

### Crown Jewel: trending_SELL

| Sub-model | Accuracy | WR | Profit Factor | Notes |
|-----------|----------|-----|---------------|-------|
| **trending_SELL** | **0.687** | **0.462** | **3.00** | ⭐ BEST — ทำกำไรหลัก |
| overall | 0.631 | 0.423 | 1.00 | baseline |
| direction_SELL | 0.607 | 0.389 | 1.00 | ใช้ได้ |
| trending_BUY | 0.375 | 0.429 | 0.50 | ❌ แย่ — ทำลายกำไร |
| direction_BUY | 0.363 | 0.467 | 0.52 | ❌ แย่ |
| regime_ranging | 0.415 | 0.389 | 0.36 | ❌ แย่ |
| ranging_BUY | 0.250 | 0.400 | 0.00 | ❌ เลวร้าย |
| ranging_SELL | 0.220 | 0.182 | 0.28 | ❌ เลวร้าย |
| regime_trending | 0.531 | 0.448 | 1.67 | ✅ ใช้ได้ |
| regime_volatile | ❌ missing | — | — | V4 ไม่มี model นี้ |

**Key takeaway**: V4 ทำกำไรจาก **trending_SELL** เป็นหลัก (PF=3.0) BUY และ ranging trades ทำลายกำไร

---

## 4. Winning Configuration Detail

### 4.1 SELL Performance (ทำกำไรหลัก)

| ช่วงเวลา (UTC) | Trades | WR | PnL | หมายเหตุ |
|----------------|--------|-----|------|----------|
| **23h** | 10 | **90%** | **+$444** | 🏆 SELL hour ที่ดีที่สุด |
| **1h** | 29 | **79%** | **+$442** | 🏆 London pre-open |
| **0h** | 18 | **72%** | **+$404** | 🏆 Asian session |
| **8h** | 20 | 60% | +$200 | London open |
| **2h** | 23 | 65% | +$92 | Asian session |

### 4.2 BUY Performance (ทำลายกำไรเป็นหลัก)

| ช่วงเวลา (UTC) | Trades | WR | PnL | หมายเหตุ |
|----------------|--------|-----|------|----------|
| 23h | 30 | **20%** | **-$318** | ❌ แย่ที่สุด |
| 14h | 37 | 19% | -$303 | ❌ |
| 10h | 29 | 21% | -$201 | ❌ |
| 13h | 38 | 29% | -$166 | ❌ |
| 9h | 28 | 32% | -$141 | ❌ |

### 4.3 BUY Hours ที่ดี

| ช่วงเวลา (UTC) | Trades | WR | PnL |
|----------------|--------|-----|------|
| **19h** | 30 | **73%** | **+$520** | NY evening |
| **18h** | 14 | 86% | +$250 | NY afternoon |
| **16h** | 13 | 85% | +$259 | NY open |
| **6h** | 48 | 58% | +$457 | Asian morning |
| **17h** | 24 | 67% | +$202 | NY afternoon |

### 4.4 วันในสัปดาห์

| วัน | BUY PnL | SELL PnL | รวม | หมายเหตุ |
|-----|---------|----------|------|----------|
| **Tue** | -$175 | **+$1,824** | **+$1,649** | 🏆 SELL day ที่ดีที่สุด |
| **Wed** | +$901 | -$1,060 | -$159 | BUY ดี SELL แย่ |
| **Thu** | -$315 | +$460 | +$145 | SELL ดี |
| **Mon** | +$646 | -$358 | +$288 | BUY ดี |
| **Fri** | -$32 | +$162 | +$130 | SELL ดี |
| **Sat** | — | +$86 | +$86 | SELL เท่านั้น (5 trades) |
| **Sun** | -$88 | -$52 | -$140 | ❌ หลีกเลี่ยง |

---

## 5. Key Parameters (Winning Config)

### 5.1 Risk Management (ที่ทำงานจริงใน good era)

```
max_risk_per_trade: 2%
circuit_breaker.daily_loss_limit: 5%
circuit_breaker.consecutive_loss_limit: 5
circuit_breaker.cooldown_minutes: 15
atr_multiplier: 1.5 (SL = 1.5 × ATR)
risk_reward_ratio: 2.5 (TP = 2.5 × SL distance)
trailing_stop: true
trailing_atr_multiplier: 2.0
spread_buffer: 2.5 pips
max_concurrent_positions: 2
```

### 5.2 SL/TP Distance (จากข้อมูลจริง)

| | BUY | SELL |
|---|-----|------|
| **Avg SL distance** | 12.05 price units (~$12) | 13.54 price units (~$14) |
| **Avg TP distance** | 29.11 price units (~$29) | 33.56 price units (~$34) |
| **Avg RR ratio** | 2.39 | 2.47 |

### 5.3 Exit Reasons (Good Era)

| Exit Reason | BUY trades | BUY PnL | SELL trades | SELL PnL |
|-------------|-----------|---------|------------|---------|
| take_profit | 136 | +$3,611 | 36 | +$1,383 |
| max_holding | 146 | +$1,346 | 198 | +$2,194 |
| stop_loss | 243 | -$4,011 | 176 | -$2,515 |
| phantom | 1 | -$9 | 0 | — |

**Key insight**: `max_holding` เป็น exit ที่ทำกำไรได้ดี (exit ก่อนที่จะกลับ) และ SELL ใช้ max_holding เยอะกว่า take_profit

### 5.4 Trade Statistics

- **Avg winning trade**: $23.41
- **Avg losing trade**: $13.81
- **Win/Loss ratio**: 1.69 (หมายความว่าชนะได้เงินเยอะกว่าแพ้ 1.69 เท่า)
- **Max win streak**: 22 trades
- **Max loss streak**: 26 trades
- **Longest consecutive profitable days**: 5 days

---

## 6. SELL vs BUY Strategy Differences

### SELL (ทำกำไรหลัก $928.25)

- **ช่วงเวลาดี**: 0h-2h (Asian), 8h (London open), 23h (NY late)
- **ช่วงเวลาแย่**: 4h-5h (late Asian, low volume)
- **วันดี**: Tue, Thu, Fri
- **วันแย่**: Wed (แย่มาก -$1,060)
- **Session**: Asian + London pre-open เป็นช่วงที่ SELL ทำกำไรดีที่สุด
- **WR**: 40% แต่ avg win > avg loss 1.69x → ทำกำไรได้

### BUY (ทำกำไร $890.54 แต่ inconsistent)

- **ช่วงเวลาดี**: 6h (Asian morning), 16h-20h (NY session)
- **ช่วงเวลาแย่**: 9h-14h (Europe overlap), 23h (late night)
- **วันดี**: Mon, Wed
- **วันแย่**: Thu, Sun
- **WR**: 45% แต่ inconsistency สูง — มี hours ที่ขาดทุนหนักมาก

### สรุป SELL vs BUY

| | SELL | BUY |
|---|------|-----|
| **ข้อดี** | consistent, PF สูงใน trending | ทำกำไรได้ใน NY session |
| **ข้อเสีย** | แย่มากใน Wed | แย่มากใน Europe hours |
| **ชั่วโมงทอง** | 0h-2h, 8h, 23h | 6h, 16h-20h |
| **Model ที่ใช้** | trending_SELL (PF=3.0) | direction_BUY (PF=0.52) ❌ |

---

## 7. สิ่งที่ทำงาน (Keep) vs ไม่ทำงาน (Fix/Drop)

### ✅ Keep (ทำงานดี)

1. **trending_SELL model** — PF=3.0, accuracy=0.687 → หัวใจของระบบ
2. **ATR-based SL** (1.5×) → SL distance เฉลี่ย 12-14 price units เหมาะสม
3. **RR ratio 2.5** → TP distance 2.5x SL → avg RR จริง ~2.4
4. **Circuit breaker** (5 consecutive losses, 5% daily limit) → ป้องกัน drawdown รุนแรง
5. **max_holding exit** → ทำกำไรได้ดี โดยเฉพาะ SELL
6. **Trailing stop (2× ATR)** → lock in profits ใน trending moves
7. **Asian session SELL** → 0h-2h WR 65-79%

### ❌ Fix or Drop (ไม่ทำงาน)

1. **direction_BUY model** — PF=0.52, accuracy=0.363 → **ควร disable BUY หรือ train BUY model ใหม่**
2. **ranging_SELL** — PF=0.28 → **ควร skip trades ในช่วง ranging**
3. **ranging_BUY** — PF=0.00 → **ไม่มีข้อมูลเลย**
4. **BUY ช่วง Europe (9h-14h)** → WR 19-32%, PnL -$800+ → **ควร filter ออก**
5. **BUY ช่วง late night (23h)** → WR 20%, PnL -$318 → **ควร filter ออก**
6. **LEARNING_MODE=1** → **ห้ามใช้** มันข้ามทุก risk check
7. **SELL ช่วง Wed** → WR 17%, PnL -$1,060 → **อาจ filter ช่วงเวลานี้**

---

## 8. Current Working Config (หลังแก้ bug แล้ว)

### Account A (Real, V4 model — 10 sub-models)

```
ML_MODEL_DIR_A=/app/data/models/trade_outcome_v4
LEARNING_MODE=0
INITIAL_EQUITY_A=1000
```

### Accounts B/C/D (Demo, Mixed model — 12 sub-models)

```
ML_MODEL_DIR=/app/data/models/trade_outcome_mixed
LEARNING_MODE=0
INITIAL_EQUITY_B/C/D=100
```

### Mixed Model Composition

- **SELL models**: จาก V11 (regime-specific: trending_SELL, ranging_SELL, volatile_SELL, etc.)
- **BUY models**: จาก V6 (ยังไม่มี V11 BUY model → train ใหม่ทีหลัง)
- **Regime models**: จาก V11 (trending, ranging, volatile)

---

## 9. Recommendations

### 9.1 ทันที (Now)

1. ✅ เปลี่ยน LEARNING_MODE=0 — แก้แล้ว
2. ✅ แก้ sync_pnl_from_db bug — แก้แล้ว
3. ✅ Deploy mixed model — แก้แล้ว
4. 🔲 **เพิ่ม time filter** — หยุด BUY ช่วง 9h-14h UTC, หยุด BUY 23h UTC
5. 🔲 **เพิ่ม day filter** — ลด SELL ในวัน Wed

### 9.2 สั้นที่สุด (1-2 สัปดาห์)

1. 🔲 **Train V11 BUY model** — ใช้ข้อมูลที่เก็บจาก good era BUY trades เท่านั้น
2. 🔲 **Disable direction_BUY ใน V4** — หรือเพิ่ม confidence threshold สูงขึ้น (≥0.60)
3. 🔲 **เพิ่ม regime_volatile model ใน V4** — ตอนนี้ไม่มี ทำให้ skip volatile periods ได้ไม่ดี

### 9.3 กลางวัน (1-2 เดือน)

1. 🔲 **Session-aware confidence** — ลด confidence สำหรับ SELL ใน Wed, BUY ใน Europe hours
2. 🔲 **Adaptive lot sizing** — เพิ่ม lot size ในช่วงเวลาที่ WR สูง (0h-2h SELL, 19h BUY)
3. 🔲 **Scalp-specific model** — train model แยกสำหรับ M5 scalp (ยังไม่มีข้อมูลพอ)

---

## 10. V4 Crown Jewel Summary

**trending_SELL model** คือหัวใจของระบบ:

- PF = 3.00 (ดีมาก)
- Accuracy = 0.687 (ดี)
- WR = 46.2% (ไม่สูง แต่ avg win > avg loss 1.69x)
- ทำกำไรได้ดีใน Asian session (0h-2h UTC) และ London pre-open (8h UTC)
- ช่วง Wed แย่ → อาจต้อง filter

**สิ่งที่ต้องรู้**: V4 ทำกำไรได้เพราะ **SELL ใน trending market** โดยเฉพาะ Asian session ไม่ใช่เพราะ BUY ดี BUY โดยรวม PF=0.52 แย่กว่า random → **ควร focus ที่ SELL strategy และ improve BUY หรือ limit BUY เฉพาะช่วงที่ดี**

---

*สรุปโดย God Port Oracle — 2026-06-23*
*ข้อมูลจาก Account A (live), 938 trades (good era) + 100 trades (broken era)*