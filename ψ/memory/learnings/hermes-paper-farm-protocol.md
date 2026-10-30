---
name: hermes-paper-farm-protocol
description: สัญญาการสื่อสารระหว่าง Hermes (scope AI Investor) กับ paper trade farm ของ god-port — รูปแบบ proposal, ตำแหน่ง league table, กฎ out-of-sample
metadata:
  type: reference
---

# Hermes ↔ Paper Farm Protocol

> สร้าง: 2026-09-30 · เจ้าของฝั่งรับ: god-port (Broky/Metty) · เจ้าของฝั่งวิเคราะห์: Hermes (scope AI Investor)

## ภาพรวม

Paper trade farm รันบน **Mac mini (OrbStack)** — เดิม brokerless ล้วน ตั้งแต่ **30/10/26 เป็น read-only bridge client**:
- **Feed**: `paper-fetcher` ดึง **Exness XAUUSD realtime จาก bridge mt5q บน VPS ผ่าน Tailscale** (`FEED_SOURCE=bridge`, poll 60s) → CSV M5/H1/D1 (premium format) — ราคาเดียวกับ demo B/C/D เป๊ะ · ยุคเก่า (ถึง 30/10/26 10:00 UTC) = yfinance `GC=F` หน่วง 5-15 นาที · ช่วง 10:00→10:45 UTC 30/10 ใช้ mt5b (race กับ engine B — แก้ด้วย mt5q, ดู retro mt5q-dedicated-bridge)
- **Engines**: ก้อนละ ~25 variants (account `P<digits>`, กฎ mr-bet MR-only `TRENDING_HARD_BLOCK=1`), `DRY_RUN=1`, trade เข้า SQLite จริงใน container
- **ความหน่วง**: แท่งปิด → CSV ภายในรอบ 60s · fill ฟรีไม่มี spread จริง (ตามแบบ: ชั้น paper = realtime + fill ฟรี, gap paper↔demo ที่เหลือ = execution costs ล้วน) — ดู §7 era

## 1. ตำแหน่งข้อมูล

| อย่าง | ที่ไหน |
|---|---|
| **League table** (Hermes อ่านประจำ) | `ψ/outbox/league_table_<YYYY-MM-DD>.md` — regenerate โดย cronjob ทุก 30 นาที |
| **ตัวเลขดิบ** | `data/farm/<group>/*.db` (SQLite, ตาราง `live_trades` JOIN `accounts`, `rejected_signals`) |
| **นิยาม variant ทั้งหมด** | `farm_variants.yml` — source of truth เดียว |
| **คอลัมน์ league table** | Variant, Strategy, Trades, Open, WR, Σpnl, Equity, PF, MaxDD, เก็บข้อมูลถึง |

## 2. การเสนอ variant ใหม่ (Hermes → farm)

แก้ `farm_variants.yml` แล้วสั่ง deploy — **ไม่ต้องแตะโค้ด**:

```yaml
variants:
  P21:                       # ชื่อ P<digits> ห้ามชนกับที่มีอยู่
    note: "เหตุผล/สมมติฐานสั้นๆ"
    env:
      RR_RATIO: "1.50"       # knob ที่ต่างจาก common
```

แล้วรัน:
```bash
cd ~/Code/github.com/doctorboyz/god-port-oracle
python3 scripts/paper_farm_apply.py --up   # regenerate compose + redeploy
```
- DB เดิมคงอย้่ ไม่โดน recreate; account ใหม่ seed อัตโนมัติ (balance 100)
- ถ้าจำนวน variant ทำก้อนเกิน ~25 ตัว → เพิ่ม group ใหม่ใน `groups:` (DB แยกไฟล์)

### Knob ที่ใช้ได้ (key ใน `common`/`env` — script จะเติม suffix `_P<digits>` ให้)

| Knob | ความหมาย | ค่า mr-bet มาตรฐาน |
|---|---|---|
| `RR_RATIO` | อัตรา TP:SL | 0.8 / 1.0 / 1.2 |
| `MR_BOLL_THRESHOLD` | ความลึก Bollinger ที่ยอมเข้า (%B) | 0.70 / 0.80 / 0.85 |
| `MIN_CONFIDENCE` / `BUY_MIN_CONFIDENCE` | ประตู confidence | 0.55 |
| `ATR_MULTIPLIER` | ระยะ SL เป็นจำนวน ATR | 2.0 |
| `TIME_STOP_BARS` | time stop (จำนวนแท่ง M5) | 12 |
| `ENTRY_HOURS` / `BLOCKED_HOURS` | หน้าต่างชั่วโมงเข้า (UTC) | `6,8,18,19,20` / `3,4,5,9,14` (ค่าว่าง = ปิด gate) |
| `RISK_PER_TRADE` | % equity ต่อ trade | 0.005 |
| `SL_CAP` | SL สูงสุด ($) | 12.0 |
| `MR_COST_MULT` | TP ต้อง ≥ N เท่าของ spread | 3.0 |
| `SWING_MAX_SPREAD` | gate spread (pts, inert ใน farm) | 30 |
| `MAX_POSITIONS` | จำนวน position พร้อมกัน | 1 |
| `CIRCUIT_BREAKER_COOLDOWN_MINUTES` | พักหลังแพ้ 3 ติด | 1440 |

**ข้อจำกัดก้อน**: `TRENDING_HARD_BLOCK` เป็น process-global — ก้อนเดียวรัน mode เดียวทั้งก้อน ถ้าอยากเทส trend-following ต้อง group ใหม่ที่ตั้ง `TRENDING_HARD_BLOCK=0` (แก้ใน `FARM_MODE_ENV` ของ `scripts/paper_farm_apply.py` ให้รับ per-group override — ยังไม่ทำใน pilot)

## 3. กฎ out-of-sample (กัน overfitting) — บังคับ

1. **Selection window**: Hermes เลือก variant ชนะจาก league table ช่วงใดก็ได้ แต่ต้องระบุวันที่ใน proposal
2. **Validation window**: variant ชนะต้องเก็บข้อมูลช่วงใหม่ (ที่ไม่ใช่ช่วง selection) อีกอย่างน้อยเท่ากับ selection window และยังชนะ/ไม่แตก ก่อนเลื่อนขั้น
3. **trades < 20 = ไม่ตัดสิน** — noise ยังเยอะ ห้าม kill/promote
4. **สู่ VPS mr-bet**: variant ที่ผ่าน validation จะทดสอบต่อบน VPS (B/C/D pattern, demo ก่อน) — คนละสภาพ spread/latency ต้องยืนยันใหม่
5. **กฎเหล็กของ scope invest**: อะไรที่แตะ Real-A หรือบช.จริง = ขออนุมัติคุณหมอก่อนเสมอ

## 4. Cronjob (host Mac mini)

League table regenerate ทุก 30 นาที:
```
13,43 * * * * cd ~/Code/github.com/doctorboyz/god-port-oracle && /usr/bin/python3 scripts/league_table.py >> data/farm/league_cron.log 2>&1
```

## 5. สิ่งที่ farm ไม่รับประกัน

- ราคา delay 5-10 นาที + ไม่มี spread → ผล league เป็นตัวคัดกรองเชิงสถิติ ไม่ใช่พิสูจน์กำไรสุทธิ
- ยังไม่ scale 100 variants — pilot 10 ตัววัด RAM ก่อน (`docker stats oracle-engine-farm1`)
## 7. Feed era — ห้าม mix ข้ามยุค (สำคัญตอนเทียบตัวเลข)

| Era | ช่วงเวลา | Feed | ราคา |
|---|---|---|---|
| **yfinance** | เริ่มฟาร์ม → 30/10/26 10:00 UTC | yfinance `GC=F` futures หน่วง 5-15 นาที | ระดับ futures (ต่างจาก spot หลายดอลลาร์) |
| **bridge** | 30/10/26 10:00 UTC → ปัจจุบัน | Exness XAUUSD realtime ผ่าน bridge (Tailscale) — mt5b ถึง 10:45 UTC แล้ว mt5q ตั้งแต่นั้น | ราคา spot เดียวกับ demo B/C/D เป๊ะ |

- **ห้ามเทียบ paper↔demo ข้าม era** — ราคาต่างกันเป็นสิบๆดอลลาร์ + timing คนละจังหวะ ผลเทียบเป็นเท็จโดยสร้าง (สถิติ league ก่อน 30/10 = era yfinance เท่านั้น)
- Fetcher บริหาร era เอง: marker `XAUUSD_<TF>.csv.era` ใน feed dir — สลับ source = **replace history ทั้งชุด** ไม่ merge (spot กับ futures ต่อกัน = gap เทียม พัง indicator ทั้งฟาร์ม) · bridge ล่ม = retry เท่านั้น **ไม่ fallback ไป yfinance**
- watchdog อ่าน marker นี้เพื่อเลือกปฏิทิน (CME ↔ Exness — halt ต่างชั่วโมงกันหลัง DST สหรัฐฯ 1/11/26): `scripts/farm_watchdog.py` + `shared/market_calendar.py` (feed=exness)
- สัญญา read-only ของ fetcher ต่อ bridge: initialize / symbol_select / symbol_info / copy_rates_from_pos_json เท่านั้น — ห้าม order/position methods · อย่าเรียก shutdown เอง · **mt5q เป็น terminal ของ fetcher อย่างเดียว (30/10/26 12:51 UTC เป็นต้นไป — ไม่มี engine ใช้ร่วม)**: ก่อนหน้านั้นแชร์ mt5b กับ demo engine B แล้วเกิด race ฝั่งเดียว (engine shutdown ทุก cycle ↔ fetcher heal กลับ — ฟาร์มตาบอดเป็นช่วง bar age >10 นาที · ส่วน stream-closed ที่เห็นใน log engine เป็น baseline ของ engine เอง B=C=D เท่ากันเป๊ะ ไม่ใช่ผลจาก fetcher — ดู retro mt5q-dedicated-bridge) บทเรียน: **terminal MT5 = ทรัพยากร process-global แชร์ข้ามเจ้าของ lifecycle ไม่ได้ ต้องแยกคนละ container** · ยังรักษา heal re-initialize ไว้ (ป้องกัน shutdown จาก client อื่นที่อาจบุกในอนาคต) ดู test TestBridgeSharedTerminalCausal
