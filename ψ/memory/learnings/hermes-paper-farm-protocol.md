---
name: hermes-paper-farm-protocol
description: สัญญาการสื่อสารระหว่าง Hermes (scope AI Investor) กับ paper trade farm ของ god-port — รูปแบบ proposal, ตำแหน่ง league table, กฎ out-of-sample
metadata:
  type: reference
---

# Hermes ↔ Paper Farm Protocol

> สร้าง: 2026-09-30 · เจ้าของฝั่งรับ: god-port (Broky/Metty) · เจ้าของฝั่งวิเคราะห์: Hermes (scope AI Investor)

## ภาพรวม

Paper trade farm รันบน **Mac mini (OrbStack)** แบบ **brokerless** — ไม่มี MT5, ไม่มี Exness:
- **Feed**: `paper-fetcher` ดึง yfinance `GC=F` → CSV M5/H1/D1 (premium format) ทุก 5 นาที
- **Engines**: ก้อนละ ~25 variants (account `P<digits>`, กฎ mr-bet MR-only `TRENDING_HARD_BLOCK=1`), `DRY_RUN=1`, trade เข้า SQLite จริงใน container
- **ความหน่วง**: ราคา delay ~5-10 นาที, ไม่มี spread จริง (gate ผ่านเป็น None) — ใช้เรียนรู้/คัดกรองเชิงสถิติ ไม่ใช่พิสูจน์ execution

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