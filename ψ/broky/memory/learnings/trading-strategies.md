# Trading Strategies Knowledge Base

> ความรู้จาก NotebookLM "Algorithmic Crypto Trading" + ประสบการณ์ AI-XAUUSD-Trading + JPMorgan Position Scaling

## JPMorgan Position Scaling Rules

> Senior executive at JPMorgan Chase: "Discipline and long-term growth."

| Price Change | Action | Position Adjustment |
|---|---|---|
| -10% | Hold | Wait for confirmation |
| -20% | Buy +15% | Scale in 15% of original |
| -30% | Buy +30% | Scale in 30% of original |
| +10% | Hold | Let it run |
| +20% | Hold | Let it run |
| +30% | Sell 10% | Take profit 10% off |
| +40% | Sell 20% | Take profit 20% off |
| +50% | Sell 30% | Take profit 30% off |
| +60% | Sell 40% | Take profit 40% off |
| +100% | Sell 60% | Take profit 60% off |

### Implementation
- `broky.signals.scaling.calculate_scaling_action(price_change_pct)` → ScalingDecision
- `broky.signals.scaling.calculate_position_adjustment(original_lot, current_lot, decision)` → new lot size
- BUY adjustments use % of original position size
- SELL adjustments use % of current position size
- Minimum lot = 0.01 (XAUUSD standard)
- All 10 rules have comprehensive test coverage in `tests/test_scaling.py`

## Indicator Combinations

### Trend Following
- **Golden Cross**: SMA(50) ตัดขึ้น SMA(200) = bullish ระยะยาว
- **EMA Cross**: EMA(9) ตัด EMA(21) = short-term momentum
- **Gaussian Channel**: ราคาปิดเหนือ upper band + green slope = bullish

### Mean Reversion
- **Bollinger + RSI**: ราคาแตะ lower band + RSI < 30 = oversold, target กลับไป middle band
- **RSI(14)**: overbought > 70, oversold < 30

### Recommended XAUUSD Parameters
| Indicator | Parameters | Weight | Role |
|-----------|-----------|--------|-----|
| RSI | 14, OB=70, OS=30 | 0.20 | Momentum |
| MACD | 12/26/9 | 0.25 | Trend |
| Bollinger Bands | 20, 2.0 std | 0.15 | Volatility |
| EMA Cross | 9/21 | 0.20 | Trend |
| ATR | 14 | sizing | Position sizing |
| Stochastic | 14/3/3 | 0.10 | Momentum |
| Volume | MA(20) | 0.10 | Confirmation |

## Risk Management Framework

### Position Sizing
- Risk per trade: **1-2%** ของ equity
- Leverage เริ่มต้น: **3-5x** (ไม่เกินนี้สำหรับบัญชีเล็ก)
- Isolated Margin mode เท่านั้น
- Max concurrent positions: **2**

### Circuit Breaker (Kill Switch)
- **Level 1**: หยุดรับสัญญาณใหม่เมื่อ volatility สูง
- **Level 2**: ยกเลิก pending orders
- **Level 3**: ปิดทุก positions + ปิดระบบถ้า flash crash (10% drop in 5 min)

### Drawdown Limits
- Daily loss limit: **5%** ของ equity
- 3 consecutive losses = **15 นาที cooldown**
- Trailing stop: ATR-based activation + distance

## Session-Based Trading (XAUUSD)

| Session | Hours (UTC) | Spread | Volatility | Lot Size |
|---------|-------------|--------|-----------|----------|
| Asian | 00:00-08:00 | 3-5 pips | ต่ำ | เล็กลง |
| London | 08:00-16:00 | 1-2 pips | สูง | ปกติ |
| NY | 13:00-22:00 | 1-2 pips | สูงมาก | ปกติ |
| Overlap | 13:00-16:00 | < 1 pip | สูงสุด | ปกติ+ |

- London session: spread < 2 pips เหมาะ algorithmic trading
- Asian session: liquidity ต่ำ = ลด lot size
- Overlap: โอกาสดีที่สุด แต่ต้องระวัง news spikes

## Multi-Timeframe Analysis

| Timeframe | ใช้ทำอะไร | Indicators |
|-----------|----------|-----------|
| D1 | ดู trend หลัก | SMA(50), SMA(200) |
| H4 | ดู trend รอง | EMA(9/21), MACD |
| H1 | ดู pullback | RSI, Bollinger |
| M5 | ดู entry | Stochastic, Volume |

## Backtesting Methodology

### Walk-Forward Optimization
1. Train on 70% ของข้อมูล
2. Validate on 30%
3. Roll forward, repeat
4. ต้องผ่านอย่างน้อย 6-12 เดือนข้อมูล (bull + bear + sideways)

### Monte Carlo Validation
- รัน 1,000 simulations บน backtest results
- เช็คว่า profit factor > 1.5 ใน 95% ของ simulations
- เช็คว่า max drawdown < 20% ใน 95% ของ simulations

### Phase Progression
| Phase | Capital | Max Lot | Risk/Trade | Max Positions |
|-------|---------|---------|-----------|-------------|
| Backtest | $1,000 (sim) | 0.10 | 3% | 2 |
| Forward | $1,000 (sim) | 0.05 | 2% | 1 |
| Paper | $1,000 (demo) | 0.01 | 1% | 1 |
| Live Start | $100-1,000 | 0.01 | 1% | 1 |
| Live Growth | Scale | Scale | 1-3% | 2 |

## ML/LSTM Assessment

### What Works
- **Random Forest**: ดีสำหรับ multi-feature classification (trend up/down/range)
- **ARIMA-LSTM Hybrid**: ARIMA จับ linear trend, LSTM จับ non-linear residuals
- **Ensemble Voting**: หลายโมเดล vote เสียงข้างมาก

### What Doesn't Work
- **Pure RL without goals**: random exploration ในตลาดที่มี noise สูง = ไม่เวิร์ค
- **Overfitted models**: รุ่นที่ fit กับอดีตมาก = พังเมื่อ regime เปลี่ยน
- **Single LSTM**: ไม่ดีพอเดี่ยวๆ ต้องรวมกับ feature engineering

## Spread & Slippage Handling
- คำนวณ spread + slippage ใน backtest เสมอ
- ใช้ limit orders ใน live trading เพื่อควบคุม entry price
- Slippage guard: ถ้าราคาเปลี่ยนเกิน 0.5% จาก signal = ไม่ส่งคำสั่ง
- เพิ่ม spread buffer ใน SL calculation (2-3 pips สำหรับ XAUUSD)