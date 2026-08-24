---
title: Demo-D slow-but-sure deploy + cron 6h analysis loop
date: 2026-08-23
tags: [deploy, demo-d, ml-ensemble, cron, slow-but-sure, atr-2.5, rr-3.0, threshold-0.50]
status: deployed
---

# 2026-08-23 — Demo-D Slow-but-Sure Deploy

## บทเรียนสำคัญ

**1. `.env` ใน VPS override `docker-compose.vps.yml` defaults — ต้องแก้ทั้งสองที่**

Compose `${VAR:-default}` ใช้เฉพาะเมื่อ env var ไม่ถูก set ใน `.env` file.
ถ้า `.env` มี `MAX_POSITIONS_D=5` อยู่แล้ว → compose default `1` ถูก ignore.
หลังแก้ compose อย่างเดียว แล้ว recreate container → env ยังเป็นค่าเก่า.
ต้อง scp `.env` ลงมา แก้ 9+6 บรรทัด แล้ว push กลับ + recreate อีกรอบ.
**How to apply**: ทุกครั้งที่ปรับ env สำหรับ account ที่มีอยู่แล้ว → แก้ compose + .env พร้อมกัน.

**2. Python `python3 -c "..."` ใน `docker exec` ไม่รอบรัง multi-line `"..."` string**

SQL string หลายบรรทัดใน Python `"...multi\nline..."` → `SyntaxError: unterminated string literal`.
bash ส่ง code ได้ปกติ แต่ Python parser ไม่ยอมรับ newline ใน `"..."` string.
ต้องใช้ `"""..."""` triple-quote (ยุ่งยากกับ bash quoting) หรือ แปลง SQL เป็น single-line.
**How to apply**: `docker exec ... python3 -c '...'` → ทุก SQL string ใน script ต้อง single-line.

**3. Python ใน container เขียน host path ไม่ได้ — ต้อง stdout + redirect ที่ host**

`docker exec container python3 -c "open('/host/path/x.csv','w')"` → `FileNotFoundError`
เพราะ Python เห็นแค่ filesystem ใน container. วิธีที่ถูก: `print(...)` ออก stdout
แล้ว `docker exec ... > /host/path/x.csv` ที่ host bash.
**How to apply**: ทุก cron/automation script ที่ดึงข้อมูลจาก container → split เป็น N `docker exec` calls, แต่ละ call print 1 output แล้ว redirect ที่ host.

**4. V4+v6-OR ensemble @0.50 ผ่าน OOS ทุก account — ปลอดภัยพอ deploy จริง**

จาก `ψ/lab/compare-c-models-2026-06-29/`:
- B @0.45: OOS PF 2.60 (atr=2.0 rr=3.0)
- C @0.50: OOS PF 2.02 (atr=2.5 rr=3.0)
- D @0.50: OOS PF 2.55 (atr=2.5 rr=3.0)
ML filter = survival-critical สำหรับ balance เล็ก ($100 no-ML → ruin MaxDD 101%).
**How to apply**: B/C/D เปิด ML ได้ทั้งหมด (global env) — IRON LAW คือ A คนละ container.

**5. Crontab off-peak minute + logging**

ใช้ `7 */6 * * *` (minute 7, ทุก 6h UTC) แทน `0 */6 * * *` — หลีกเลี่ยง fleet sync
และ redirect ไป `/var/log/pull_demo_d.log` เพื่อ debug ภายหลัง.
**How to apply**: ทุก cron บน VPS → เลือก minute 7/13/23/37/47/53 และมี log file.

## Deploy อะไร

Demo-D (Exness-MT5Trial7, login 415917228) — V4+v6-OR ensemble @0.50
Geometry atr=2.5/rr=3.0, 1% risk/trade, max 1 position, drawdown 3/6/15%.
Goal: 4 สัปดาห์ profitable → confidence ย้าย config ไป Real-A.

Cron 6h ดึง `live_trades` (last 50) + summary JSON + inbox MSG → `ψ/inbox/demo-d-snapshot-<TS>/`.
Next session อ่าน snapshot → decision tree → outbox result → adjustment (ถ้ามี).

## ไฟล์ที่เกี่ยวข้อง

- `ψ/plans/demo-d-slow-but-sure-deploy.md` — plan doc ถาวร
- `docker-compose.vps.yml` — D env block (lines 280-289)
- `scripts/pull_demo_d_data.sh` — cron script (single-line SQL, stdout redirect)
- VPS `.env` — backup ที่ `.env.bak.20260823-slowbutsure`
- `ψ/lab/compare-c-models-2026-06-29/` — OOS baseline ที่ justify config

## Iteration protocol (ทุก session)

1. READ ψ/inbox/demo-d-snapshot-* latest
2. เทียบ cumulative PnL/PF/WR กับ baseline OOS (PF 1.66, MaxDD 17.3%, 91 trades)
3. Decision tree → adjustment ถ้าต้อง
4. เขียน outbox + อัปเดต memory

## อะไรต่อไป

- รอ cron 6h แรก (00:07, 06:07, 12:07, 18:07 UTC)
- ตรวจ open trade 3411173137 (pre-existing SELL 2026-07-02) — close/retry next cycle
- หลัง 14 วัน → ตรวจ success criteria; หลัง 28 วัน → ตัดสินใจย้ายไป Real-A