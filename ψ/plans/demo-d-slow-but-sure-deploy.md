# Plan: Demo-D Slow-but-Sure Deploy + 6h Cron Analysis Loop

**Status**: ✅ Deployed 2026-08-23 14:23 UTC
**Branch**: 2026-07-01-live-trader-bugfix
**Account**: Demo-D (Exness-MT5Trial7, login 415917228)
**Baseline**: V4+v6-OR ensemble @0.50, atr=2.5/rr=3.0 → OOS PF 1.66, MaxDD 17.3%, 91 trades
**Goal**: 4-week profitable run → confidence to re-fund Real-A with this config

## Context — ทำไมต้อง deploy ชุดนี้

doctorboyz ถอนเงินจริงออกจาก Real-A หมด → Real-A หยุดเทรด (drawdown circuit breaker ทำงาน)
แต่ demo accounts ทั้ง 3 (B/C/D) ก็หยุดเทรด 2026-06-23 เพราะ MT5 connection loss
และตั้งแต่ 2026-07-02 B/C/D ปิด `ML_FILTER_ENABLED=0` เลยไม่ได้ใช้ ML gate

จาก `ψ/lab/compare-c-models-2026-06-29/`:
- V4+v6-OR ensemble ผ่าน OOS PF > 1.5 ทุก account (B 2.60, C 2.02, D 2.55)
- @0.50 = sweet spot สำหรับ "slow but sure" — @0.45 บางเกินไป, @0.55+ เยอะเกินไป
- ML filter = survival-critical สำหรับ balance เล็ก ($100 no-ML = ruin MaxDD 101%)

เป้าหมาย: deploy ชุด conservative บน Demo-D (cleanest slate) + ดึงข้อมูลทุก 6 ชม
เพื่อวิเคราะห์ + iterate จนกว่าจะได้ slow-but-sure จริง แล้วค่อยย้ายไป Real-A

## Deployed Config (Demo-D)

Source: `docker-compose.vps.yml` oracle-engine-train service, account D env block.
`.env` บน VPS override compose defaults — ทั้งสองที่แก้พร้อมกัน (ไฟล์เดิมทำให้ container ใช้ค่าเก่า)

| Env var | Value | Old | Rationale |
|---------|-------|-----|-----------|
| `ML_FILTER_ENABLED` | 1 | 0 | เปิด ML gate — survival-critical |
| `ML_ENSEMBLE_MODE_D` | or | (unchanged) | OR-gate over V4+v6 |
| `ML_ENSEMBLE_THRESH_D` | 0.50 | (new) | OOS PF 1.66 baseline |
| `MAX_POSITIONS_D` | 1 | 5 | single position = slow |
| `MIN_POSITIONS_D` | 1 | (new) | floor — drawdown ไม่อดเทรด |
| `ATR_MULTIPLIER_D` | 2.5 | (unchanged) | OOS baseline geometry |
| `RR_RATIO_D` | 3.0 | (unchanged) | OOS baseline geometry |
| `MIN_CONFIDENCE_D` | 0.60 | 0.45 | เน้น quality entries |
| `BUY_MIN_CONFIDENCE_D` | 0.55 | 0.45 | BUY มี WR ต่ำกว่า SELL |
| `INITIAL_EQUITY_D` | 200 | 100 | รองรับ 1% risk ต่อ trade ที่ lot ขั้นต่ำ |
| `RISK_PER_TRADE_D` | 0.01 | (new) | 1% per trade = slow + capital preservation |
| `DRAWDOWN_DAILY_LIMIT_D` | 0.03 | 0.10 | kill loss วันเดียวเร็ว |
| `DRAWDOWN_WEEKLY_LIMIT_D` | 0.06 | 0.20 | tight weekly cap |
| `DRAWDOWN_ACCOUNT_LIMIT_D` | 0.15 | 0.50 | MaxDD cap 15% (slow-but-sure) |
| `DRAWDOWN_COOLDOWN_HOURS_D` | 4 | 2 | พักนานกว่าหลัง drawdown breach |
| `TRADE_BLOCKER_DAILY_LIMIT` | 10 | 20 | max 10 trades/day |
| `TRADE_BLOCKER_WEEKLY_LIMIT` | 30 | 80 | max 30 trades/week |
| `TRADE_BLOCKER_HARD_MAX_LOTS` | 0.05 | 0.50 | hard lot cap ขนาดเล็ก |

**IRON LAW preserved**: Real-A อยู่ใน `oracle-engine` container คนละตัว — `ML_ENSEMBLE_MODE_A` ไม่เคย set.

## Data Pipeline — Cron 6h

**Script**: `scripts/pull_demo_d_data.sh` (pushed to VPS at `/root/god-port-oracle/scripts/`)
**Crontab** (UTC, off-peak minute 7):
```
7 */6 * * * /root/god-port-oracle/scripts/pull_demo_d_data.sh >> /var/log/pull_demo_d.log 2>&1
```

**Output**: `ψ/inbox/demo-d-snapshot-<TS>/`
- `summary.json` — open/closed/PnL/PF/WR 24h+7d+cumulative + max consecutive losses
- `trades.csv` — last 50 D trades (entry/exit/ML/regime/atr/confidence)
- `MSG.md` — inbox message พร้อม decision tree ให้ next session
- `query.err` — stderr สำหรับ debug (ควรว่าง)

**Data source**: `live_trades` table only (user approved 2026-08-23 — rejected_signals empty)
**Filter**: JOIN `accounts WHERE name='D'` (robust ต่อ account_id changes — D=4 วันนี้, อาจเปลี่ยน)
**Cumulative baseline**: trades ที่ `exit_time > '2026-08-23'` (เริ่มนับจาก deploy day)

### แก้บักสำคัญ (2026-08-23)

1. **CSV FileNotFoundError**: Python รันใน container เขียน host path ไม่ได้ → แก้ให้ print CSV ออก stdout แล้ว redirect ที่ host
2. **Python multi-line `"..."` SyntaxError**: SQL string หลายบรรทัดใน `python3 -c "..."` invalid → แปลงทุก SQL เป็น single-line

## Verification (ทำครบ 2026-08-23 14:23)

- ✅ Container recreate สำเร็จ (รอบ 2 หลังแก้ `.env` ให้ override compose defaults)
- ✅ ML filter health check ผ่าน: `ensemble or OK (2/2: trade_outcome_v4, trade_outcome_v6)`
- ✅ Demo-D balance $321.90 (จาก MT5 ping)
- ✅ 17 D env vars verified ใน container (docker exec env | grep _D)
- ✅ Signal evaluation ทำงาน — HOLD conf=0.65, counter-trend blocked (ถูกต้องตามกฎ)
- ✅ Cron ทดสอบ manual รันได้ — snapshot เขียนครบ 4 ไฟล์
- ✅ Snapshot สะอาด — query.err ว่าง, summary.json valid JSON, trades.csv 50 rows
- ⚠️ Open trade ticket 3411173137 (เปิดตั้งแต่ 2026-07-02) — close failed, retry next cycle (pre-existing, ไม่เกี่ยวกับ deploy)

## Success Criteria — 4 สัปดาห์

- Cumulative PnL > 0
- MaxDD ≤ 15%
- PF ≥ 1.5 (on closed trades ≥ 20)
- ไม่มี consecutive losses ≥ 3

## Kill Switch — หยุดแล้วปรับ

- Cumulative MaxDD > 20% → pause + re-backtest
- 7 วันไม่มี trade ใหม่ → ลด threshold ลง 0.05
- Cumulative PnL < -10% หลัง 14 วัน → pause + re-backtest
- Consecutive losses ≥ 3 → ตรวจ circuit breaker ทำงานรึเปล่า

## Iteration Protocol — ทุก session ที่ Claude Code เปิดใน repo นี้

1. **READ ψ/inbox/demo-d-snapshot-* latest** — หา snapshot ล่าสุด
2. **อ่าน summary.json + trades.csv** — เทียบ PnL/PF/WR กับ baseline OOS (PF 1.66, MaxDD 17.3%)
3. **Decision tree**:
   - PF≥1.5 + MaxDD≤15% → คง config ต่อ → เขียน outbox "on track"
   - PF<1.5 + MaxDD≤15% → เพิ่ม threshold 0.05 (เช่น 0.50 → 0.55)
   - MaxDD>15% → ลด risk_per_trade 0.005 (เช่น 0.01 → 0.005)
   - 0 trades ใน 48h → ดู candle data + ตรวจ ML threshold ว่า block หมด
   - Consecutive losses ≥3 → ตรวจ circuit breaker ทำงานรึเปล่า
4. **เขียน ψ/outbox/result_demo-d-<TS>.md** — สรุป + adjustment ถ้ามี
5. **อัปเดต ψ/memory/learnings/** ถ้ามีบทเรียนใหม่
6. **ถ้ามี adjustment** — push config ไป VPS + restart container + verify env

## Files Modified / Created

| Path | Action | Status |
|------|--------|--------|
| `docker-compose.vps.yml` | Edit D env block | ✅ |
| `scripts/pull_demo_d_data.sh` | Create cron script | ✅ |
| `ψ/plans/demo-d-slow-but-sure-deploy.md` | Create plan doc | ✅ (ไฟล์นี้) |
| `ψ/memory/learnings/2026-08-23_demo-d-slow-but-sure-deploy.md` | Learning memory | ✅ |
| VPS `/root/god-port-oracle/.env` | Edit 9 + append 6 lines | ✅ |
| VPS crontab | Install 6h cron | ✅ |

## Risks & Caveats

- **Statistical thinness**: 91 trades OOS อาจ noise สูง — ต้องการ ≥30 trades จริง ก่อนตัดสินใจ iterate
- **First 48h อาจไม่มี trade**: ensemble @0.50 block ~80% ของ signals ใน sample ที่ทดสอบ — ไม่ใช่ bug
- **MT5 connection**: เคยหยุด 2026-06-23 — ตรวจ `docker logs mt5d` ถ้า 7 วันไม่มี trade
- **Demo-D balance**: เริ่ม $321.90 — รองรับ 1% risk (lot ขั้นต่ำ 0.01) ได้สบาย
- **AEGIS vs ML**: B/C/D เปิด ML แล้วทั้งหมด (global env) — B @0.45, C @0.50, D @0.50 — ไม่กระทบ A (คนละ container)
- **Open trade ticket 3411173137**: pre-existing SELL ตั้งแต่ 2026-07-02 — รอ next cycle close/retry

## Next Session Checklist

- [ ] อ่าน ψ/inbox/demo-d-snapshot-* latest (cron ทุก 6h)
- [ ] เทียบ cumulative PnL/PF/WR กับ baseline OOS
- [ ] ถ้ามี adjustment → push config + restart + verify
- [ ] ตรวจ open trade 3411173137 ว่า close หรือยัง
- [ ] ถ้าครบ 4 สัปดาห์ + ผ่าน success criteria → plan re-fund Real-A