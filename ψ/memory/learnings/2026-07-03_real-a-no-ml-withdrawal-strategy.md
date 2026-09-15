# Real-A No-ML Deploy + Withdrawal Strategy

> วันที่: 3 กรกฎาคม 2026
> เกี่ยวก้อับ: [[2026-07-03_ml-vs-aegis-3way]], [[2026-06-28_good-era-config-restore]], [[2026-07-02_aegis-ml-off-deploy]]
> ประเภท: learning (deploy + strategy)
> scope: Real-A deploy (user-approved) + manual withdrawal protocol

## สรุป

Deploy no-ML config ไปยัง Real-A (oracle-engine) ตามคำขอ user:
"ฉันจะทดลองใช้ no ML balance 400 บน Real A และถอนกำไรออกเรื่อยๆ เพื่อป้องกัน ช่วยจัดให้หน่อย deploy ให้สมบูรณ์นะ"

เปลี่ยน Real-A จาก v4 single @0.55 (ML filter on) → no-ML (AEGIS reversal gate + DP + CB + TradeBlocker เท่านั้น)
เพราะ backtest 3-way ([2026-07-03_ml-vs-aegis-3way]) แสดงว่าบน balance $400 ML ลด PnL 50% แต่ MaxDD ลดแค่ ~14%
(จาก 50.5% → 21.5% สำหรับ $200) — ส่วนต่าง PnL สูญเปล่าเพราะ MaxDD อยู่ในเลขปลอดภัยอยู่แล้ว

เพิ่มกลไก "ถอนกำไรออกเรื่อยๆ" เพื่อ lock-in survival — ไม่ใช่ auto-deploy แต่เป็น manual protocol ที่ user ทำเองผ่าน Exness terminal.

## การเปลี่ยน config (4 รายการ)

| Var | ก่อน | หลัง | เหตุผล |
|-----|------|------|--------|
| `ML_FILTER_ENABLED` | 1 | **0** | no-ML — backtest 3-way แสดง ML ลด PnL มากกว่าลด MaxDD บน $400 |
| `INITIAL_EQUITY_A` | 100 | **400** | baseline equity จริงใน MT5 (top-up sequence: verify MT5 equity ก่อนอัปเดต var) |
| `INITIAL_BALANCE_A` | 100 | **400** | sync กับ INITIAL_EQUITY_A |
| `DRAWDOWN_DAILY_LIMIT_A` | 0.20 | **0.05** | good-era kill switch — no-ML MaxDD สูงกว่า ต้อง halt เร็วกว่า |
| `DRAWDOWN_WEEKLY_LIMIT_A` | 0.30 | **0.10** | good-era |
| `DRAWDOWN_ACCOUNT_LIMIT_A` | 0.30 | **0.20** | good-era — total account ruin cap |
| `DRAWDOWN_COOLDOWN_HOURS_A` | 4 | **4** | (ไม่เปลี่ยน) |

ค่าดั้งเดิมของ `MAX_POSITIONS_A=3` `BUY_MIN_CONFIDENCE_A=0.45` `ATR_MULTIPLIER_A=1.5` `RR_RATIO_A=2.5` คงไว้ — dynamic max_positions จะ floor ตาม equity ($400 → 2 positions) ตาม [[2026-06-26_dynamic-max-positions-from-equity]]

## Iron Law ที่ปฏิบัติ

1. **Top-up sequence** (per [[2026-06-28_good-era-config-restore]]): verify MT5 equity ($401.21) BEFORE อัปเดต `INITIAL_EQUITY_A` → $400
2. **ไม่ปิด MT5 positions** ที่เปิดอยู่ (user: "ไม่ต้องปิด ดูความเสียหายได้เลย") — ตรวจพบ 0 open positions อยู่แล้ว
3. **Deploy script confirmation** — deploy-vps.sh require "I AM DEPLOYING TO REAL A" (IRON LAW gate)
4. **User explicit approval** — ผู้ใช้พิมพ์ชัดเจน: "deploy ให้สมบูรณ์นะ" ถึง deploy ได้

## Verification หลัง deploy

| Check | Result |
|-------|--------|
| Container health | `oracle-engine Up (healthy)` ภายใน 20s |
| Config loaded | `ML_FILTER_ENABLED=0`, `INITIAL_EQUITY_A=400`, `DRAWDOWN_DAILY_LIMIT_A=0.05` (verify ผ่าน `docker exec env`) |
| MT5 bridge | `Connected to MT5 bridge at mt5a:8001` — สำเร็จ |
| Signal generation | Swing + M5 scalp ทำงาน — มี HOLD signal (conf=0.18 below 0.45) |
| Reversal gate | ทำงานจริง — บล็อก counter-trend SELL (trend_alignment=1.0 bullish + made_higher_high=1.0) เห็นใน log |
| DB balance | $400 (synced) |
| Open positions | 0 (clean state) |

## Withdrawal Strategy (Manual Protocol)

หลักการ: "Lock-in survival by extracting profits before drawdown can reclaim them."

### เงื่อนไขไขที่จะถอน

| เมื่อไร | ถอนเท่าไหร่ | ทำไม |
|--------|-------------|------|
| Balance ≥ $500 | ถอน $100 → เหลือ $400 | ล็อกกำไรแรก รักษา baseline |
| Balance ≥ $600 | ถอน $200 → เหลือ $400 | ดึงส่วนเกินออก ไม่ให้เลี้ยง drawdown |
| Balance ≥ $800 | ถอน $400 → เหลือ $400 | compounding brake — กัน over-leverage |
| เกิด drawdown 10% weekly | หยุดถอน รอ recovery | cooldown 4h จะทำงานเอง |

### ทำไม $400 ไม่ใช่ $100

- Backtest premium $200 no-ML MaxDD 50.5% — ชายขอบริน
- $400 no-ML MaxDD ต่ำกว่า 50% ปลอดภัยกว่า
- แต่ถ้า $100 MaxDD 101% = RUIN — ไม่รับ
- $400 = sweet spot ระหว่าง survival + มี room โต

### ทำไมถอนเรื่อยๆ

- no-ML MaxDD บน $400 ประมาณ 30-40% (estimate จาก premium $500 MaxDD 20.2%) — ยังเสี่ยง
- ถอนกำไรออก = balance กลับ $400 → MaxDD % คงที่ ไม่โตตาม equity
- ป้องกัน "paper profit evaporate" — ถ้ารอจะถอนวันเดียว อาจโดน drawdown ก่อน
- แต่ถ้า balance < $400 ไม่ถอน เพราะจะทำให้กลายเป็น undercapitalized

### ข้อจำกัด

1. **Manual** — ไม่มี auto-withdraw API ในระบบ user ต้องเข้า Exness terminal ถอนเอง
2. **ห้ามถอนระหว่าง open position** — เพราะ margin จะเปลี่ยน อาจ trigger margin call
3. **ห้ามถอนถ้า balance < $400** — จะทำให้ drawdown limit trigger เร็วเกิน (good-era kill switch คำนวณจาก peak)

## ไฟล์ที่แก้

| ไฟล์ | การแก้ |
|------|-------|
| `.env` | 4 edits: ML_FILTER_ENABLED=0, INITIAL_EQUITY_A=400, drawdown 5/10/20%, INITIAL_BALANCE_A=400 |
| `ψ/memory/MEMORY.md` | (จะเพิ่ม) pointer ไฟล์นี้ |

## บทเรียน

**"ML filter ไม่ใช่ free lunch"** — บน balance เล็ก ML ลด MaxDD ได้แต่ราคาคือ PnL 50%+. บน balance ที่ survival ปลอดภัยแล้ว ($400+ with good-era kill switch) การเปิด ML = เสีย PnL สูงกว่าค่า survival ที่ได้. Decision rule: $100-$200 → ML on (survival-critical), $400+ → no-ML (profit-optimal).

**"Withdrawal = active risk management"** — การถอนกำไรออกเรื่อยๆ คือกลไกป้องกัน ruin ที่ user ควบคุมเอง นอกเหนือจาก DP/CB ในระบบ. แต่ละครั้งที่ balance โต → drawdown % เล็กลง แต่ถ้าไม่ถอน drawdown $ เดียวกันจะกินเปอร์เซ็นต์น้อยลง = safer. อย่างไรก็ตาม no-ML MaxDD $ ยังโตตาม balance ถ้าไม่ถอน → ต้องถอนเพื่อล็อคเปอร์เซ็นต์ MaxDD.

**"Top-up sequence สำคัญก่อน deploy"** — ถ้าอัปเดต `INITIAL_EQUITY_A` ก่อน MT5 equity จริง เกิด mismatch → DP คำนวณ drawdown ผิด → อาจ trigger หรือ miss trigger. ต้อง verify MT5 equity ≥ target ก่อนเสมอ (per [[2026-06-28_good-era-config-restore]]).

## สถานะปัจจุบัน (2026-07-03)

- Real-A: no-ML, $400 balance, 0 positions, container healthy
- Kill switch: daily 5% / weekly 10% / account 20% / cooldown 4h (good-era)
- Expected: WR ~90% PF ~4 (per backtest premium no-ML), MaxDD ~30-40% on $400
- Next: สังเกตผล 1-2 สัปดาห์ — ถ้า MaxDD ใกล้ 20% → pause และพิจารณาเปิด ML กลับ (v4 @0.55)