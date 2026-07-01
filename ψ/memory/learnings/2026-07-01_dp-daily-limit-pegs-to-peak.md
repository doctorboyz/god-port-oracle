# DP Daily Limit Pegs to Peak Equity — Profitable Strategies Bypass Daily Drawdown

> Date: 2026-07-01
> Source: scripts/backtest_live_monte_carlo.py — DP set_time verification
> Related: [[2026-07-01_e2e-backtest-entry-trailing-block]], [[2026-07-01_live-monte-carlo-backtest]], [[2026-06-22_drawdown-db-sync]]
> Status: observation — NOT a bug, NOT deployed

## คำถาม

หลังเพิ่ม `set_time()` ให้ DrawdownProtector (mirror จาก CircuitBreaker) เพื่อให้ DP ทำงานใน backtest — DP ยังไม่ fire ทุก balance. ทำไม?

## สาเหตุ

ใน `broky/risk/drawdown_protection.py` `_check_rollover()`:

```python
# Daily rollover at 00:00 UTC
day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
if day_start > self._state.daily_start:
    self._state.daily_start = day_start
    self._state.daily_pnl = 0.0
    self._state.daily_trades = 0
    # Daily start equity = yesterday's close equity
    # We don't have yesterday's close, so use peak/current as proxy
    self._state.daily_start_equity = self._state.peak_equity
```

`daily_start_equity = peak_equity` → daily limit คำนวณจาก peak ไม่ใช่ initial หรือ yesterday's close.

## ผล

สำหรับกลยุทธ์ profitable มาก (PF 4-5, WR 90%):
- $100 → peak $2,923 → daily limit = 20% × $2,923 = **$585**
- $1,000 → peak $3,857 → daily limit = **$771**
- risk/trade ประมาณ $8 (1% ของ initial หรือ 0.3% ของ peak)
- ต้องขาด ~70 ครั้งในวันเดียวถึงจะ trigger daily 20% — เป็นไปไม่ได้

**สำหรับกลยุทธ์ profitable**: DP daily drawdown ไม่มีทาง fire เพราะ limit ขยายตาม peak ที่โตเร็วกว่า loss rate.

## ทำไมเป็น design ไม่ใช่ bug

- ถ้า peg กับ initial_equity ($100) → daily limit = $20 = 2 ขาด → block เร็วเกินไป กลยุทธ์ไม่มีโอกาสทำงาน
- ถ้า peg กับ peak → ยืดหยุ่นตาม equity ปัจจุบัน → fair กว่า แต่ทำให้ profitable มากไม่ fire
- มี comment ในโค้ด: "We don't have yesterday's close, so use peak/current as proxy" — เป็น known limitation

## ผลกระทบ

1. **ใน backtest**: DP ไม่ fire (ยืนยันแล้วทุก balance) — daily 20%/weekly 30%/peak 30% ไม่ trigger
2. **ใน live**: DP จะ fire จริงถ้าเจอ loss cluster หนัก (เช่น 5 ขาดติด × $200 = $1000 > 20% × peak $3857) — แต่ด้วย WR 90% โอกาสน้อย
3. **DP ไม่ได้ถูกทดสอบในสภาพ backtest** — ไม่มีการ prove mechanism ว่า fire ถูกต้อง

## ข้อเสนอ

1. **เขียน synthetic worst-case test** — บังคับ loss cluster ใน backtest ให้ DP fire และ verify ว่า block ถูกต้อง (cooldown, unblock, reset)
2. **พิจารณา peg กับ yesterday's close จริง** — ใน live trading มี equity history อยู่แล้ว ใช้ close ของวันก่อนแทน peak จะ fair กว่า แต่ต้องเปลี่ยน interface
3. **อย่าพึ่ง DP เป็น safety net เดียว** — CircuitBreaker (5 consec) + TradeBlocker ทำงานแทนได้ ใน backtest CB ทำงาน 6-14 ครั้ง
4. **บอก user ตรงๆ** — DP ไม่ fire ใน backtest เพราะกลยุทธ์ profitable ไม่ใช่เพราะ fix ไม่ถูก

## ไฟล์

- `broky/risk/drawdown_protection.py:323` — `self._state.daily_start_equity = self._state.peak_equity`
- `ψ/memory/retrospectives/2026-07/01/12.50_e2e-backtest-live-mc-exness-oos.md` — retro ที่ระบุปัญหานี้