# Lesson: `up -d --build <service>` อาจ rebuild+recreate ทั้ง dependency chain — deploy ตู้ที่บูตนานต้องแบ่งสองจังหวะ

**Date**: 2026-09-23
**Source**: deploy แก้ RR/D ครั้งที่ 2 (retro 2026-09/23/23.28, ISSUE-090)
**Tags**: deploy, docker-compose, healthcheck, god-port-oracle

## The Lesson

`docker compose up -d --build <service>` ไม่ได้แตะแค่ service ที่ระบุ — มัน rebuild และ** recreate dependency ที่ image ของมันเปลี่ยนด้วย** (เช่น base image ถูก pull ใหม่ระหว่าง build) และถ้า service หลักมี `depends_on: condition: service_healthy` กับตัวที่บูตนาน (MT5 terminal ใต้ Wine ~30 นาที) compose จะปฏิเสธสตาร์ท service หลักทิ้งไว้ในสถานะ Created

กรณีจริง 2026-09-23: deploy oracle-engine-train → mt5b/c/d โดน recreate → healthcheck start-period 180s ตัดสิน unhealthy ก่อน terminal บูตเสร็จ → train สตาร์ทไม่ได้ ต้องมานั่งรอ 30 นาทีแล้ว up ซ้ำเอง

## How to Apply

1. **ก่อน deploy ถามก่อนว่า dependency จะโดน recreate ไหม** — ถ้า image ของ dep เปลี่ยน (base image pull, Dockerfile แก้) ให้ deploy สองจังหวะ: `up -d <deps>` → รอ healthy → `up -d <main>`
2. **HEALTHCHECK start-period ต้องยาวกว่า boot จริง** — ค่าที่ตั้งใจให้เป็นจริงต้องอ้างอิงจากการวัด ไม่ใช่ความหวัง (mt5: 180s ที่ตั้งไว้ vs บูตจริง 600-1800s)
3. **เวลาตรวจสถานะแบบ substring ให้ anchor ให้ exact** — `grep "healthy"` ตรงกับ "unhealthy" ด้วย ทำให้ monitor โกหกได้ (เจอจริงรอบนี้: ประกาศ ALL_HEALTHY ทั้งที่ทั้งสามตัว unhealthy) — ใช้ `:healthy$` หรือเทียบค่าเต็มเสมอ

## Related

- [[2026-09-23_env-set-does-not-mean-env-used]] — บทเรียนเช้าของ session เดียวกัน (ตรวจรับที่ object จริง)
- ISSUE-090 — tracking การแก้ start-period + runbook
- ISSUE-089 — ตรวจรับ config จาก object จริง
## Recurrence 2026-10-03 (same trap, new lessons)

เจอซ้ำอีกครั้งใน deploy crontab round: `up -d oracle-engine-train` (ไม่มี --build) ก็ recreate mt5b/c/d ตามอีก (config hash เปลี่ยนจาก git pull ของ compose file/env) → train ค้าง **Created** เพราะ dependency gate รอ mt5b/c/d healthy — อาการเดิมจาก ISSUE-090 ทุกประการ และคนทำซ้ำคือ Claude เองที่มี learning นี้อยู่แล้วแต่ไม่ได้เปิดอ่านก่อน deploy → **บทเรียนระดับบทเรียน: ก่อนทุก deploy ที่มี `up -d` ให้ grep `compose.*recreates` ใน ψ/memory/learnings/ ก่อนสั่ง**

ข้อค้นพบใหม่จากรอบนี้:

1. **แก้ train ค้าง Created ได้ด้วย `docker start oracle-engine-train`** — bypass dependency gate (train มี spaced-retry รอ terminal เองอยู่แล้ว) ไม่ต้องรอ dep healthy ทั้งหมดก่อน
2. **mt5 container ตายกลางทางไม่มีตัว revive** — entrypoint สตาร์ท terminal ครั้งเดียว คืนนี้ mt5d terminal ตายจาก `MQL5.com community authorization failed` (exit 10053) แล้วเงียบ ต้อง `docker restart mt5d` manual → ควรมี watchdog loop (ยังไม่แก้ รออนุมัติ)
3. **`[7/7] Failed to start the mt5linux server` เป็น non-fatal** — syntax error ใน mt5linux 1.1.1 (`copy_rates_from` f-string ขาด) เกิดกับทุก container รวมตัวที่ทำงานปกติ อย่าไล่แก้ error นี้ถ้าไม่มีอาการจริง (อาการจริงที่ต้องดูคือ terminal log: `/config/.wine/drive_c/Program Files/MetaTrader 5/logs/YYYYMMDD.log` อ่าน UTF-16 มี space ทุกตัวอักษร)
