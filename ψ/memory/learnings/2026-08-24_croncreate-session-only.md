---
title: CronCreate jobs are session-only — VPS crontab is the durable part
date: 2026-08-24
tags: [croncreate, auto-loop, session-lifecycle, vps, durable-automation]
status: learned
---

# 2026-08-24 — CronCreate Session-Only Limitation

## บทเรียนสำคัญ

**CronCreate jobs อยู่ได้แค่ใน Claude session — ปิด session แล้ว cron ตาย**

ScheduleWakeup (สำหรับ /loop) และ CronCreate (general) ทั้งคู่ session-only.
ไม่ survive session exit, `--resume`, `--continue`, หรือการ kill session.
Cron job ที่ตั้งไว้จะถูกลบทันทีเมื่อ Claude process หยุด.

**สิ่งที่ durable จริง**: VPS system crontab (`/etc/cron.d/`, `crontab -e`).
VPS cron ทำงานคู่ขนามกับ Claude session — ไม่ dependent กัน.
Cron VPS ดึง snapshot เข้า ψ/inbox/ ได้ตลอด แม้ Claude จะปิด.
แต่ Claude ต้องเปิดอยู่ถึงจะ read snapshot + apply decision tree + write outbox ได้.

**How to apply**: 
1. สำหรับ "pull data ทุก 6h" → VPS crontab (durable)
2. สำหรับ "Claude wake มา analyze ทุก 6h" → ต้องมี external scheduler ยิง Claude ใหม่
   - ตัวเลือก: `maw` background runner, cron VPS เรียก `claude -p "..."` one-shot,
     หรือ external CI (GitHub Actions on schedule) ที่ส่ง prompt เข้ามา
3. อย่าใช้ CronCreate เป็น long-running auto-loop — มันตายตอน session ปิด
4. CronCreate เหมาะกับ one-shot หรือ short-loop ภายใน session เท่านั้น

**อนุมานผิดที่เคยทำ**: คิดว่า "auto-loop via CronCreate + tmux keep-alive" จะได้ 24/7 loop.
จริง: tmux keep session ได้ แต่ Claude session ภายใน tmux ก็ยังเป็น single process
ที่ cron jobs ผูกอยู่กับมัน. ปิด tmux หรือ kill Claude → cron ตาย.

## สิ่งที่ต้องทำต่อ

- ถ้า user อยาก 24/7 auto-loop จริง → ออกแบบ external scheduler ที่ยิง Claude ใหม่
- ถ้าไม่ → ใช้ VPS cron ดึง data + user manually open Claude วันละครั้งเพื่อ analyze
- Track as ISSUE: build durable auto-loop mechanism (not CronCreate-based)

## ไฟล์ที่เกี่ยวข้อง

- [[2026-08-23_demo-d-slow-but-sure-deploy]] — deploy + cron 6h pipeline (VPS side, durable)
- `ψ/plans/demo-d-slow-but-sure-deploy.md` — plan doc with iteration protocol
- `ψ/outbox/test-wake-confirmation.md` — test result confirming CronCreate fires on schedule