# Pipe กลืน exit code — build ล้มแต่ background task รายงาน "completed (exit 0)"

**Date**: 2026-10-03
**Context**: deploy oracle-engine-train บน VPS ผ่าน ssh

## เหตุการณ์

คำสั่ง deploy รันใน background มีหน้าตาประมาณ:

```bash
ssh vpsdeluna 'cd /opt/god-port-oracle && docker compose -f docker-compose.vps.yml build oracle-engine-train 2>&1 | tail -15'
```

Notification กลับมาเป็น **"completed (exit code 0)"** — แต่ output จริงจบด้วย `ERROR: failed to extract layer ... UtimesNanoAt ... no such file or directory` และ `target oracle-engine-train: failed to solve` — build ล้มสนิท

## สาเหตุ

- exit code ของ pipeline คือ exit code ของ **คำสั่งสุดท้าย** (`tail`) ซึ่งสำเร็จเสมอ
- ssh ไม่ได้ส่ง exit code ของ remote pipeline กลับมาให้ (ถ้าไม่ pipe มันจะส่ง แต่พอมี pipe ข้างหลัง สิ่งที่ local เห็นคือ exit ของ tail)
- บวกกับการอ่านแค่ notification line ("completed, exit 0") โดยไม่อ่าน output จริง = เข้าใจว่า deploy ผ่าน

## อาการที่ต้องระวัง

- Background task รายงาน success ทั้งที่งานหลักล้ม
- Deploy "เสร็จ" แต่ container ยังใช้ image เก่า

## ทางแก้

1. **ทำ exit code ให้เห็นจริง** — echo ออกมาใน output:

```bash
ssh vpsdeluna 'cd /opt/god-port-oracle && docker compose ... build ... 2>&1 | tail -15; echo BUILD_EXIT=${pipestatus[1]}'
```

(zsh ใช้ `pipestatus`, bash ใช้ `PIPESTATUS`; หรือใช้ `set -o pipefail` ต้นทาง)

2. **อ่าน output จริงก่อนเชื่อ notification** — สแกนหา `ERROR|failed to solve|exit code != 0` เสมอ
3. **อย่าให้คำสั่ง deploy จบด้วย pipe ที่กลืนทุกอย่าง** — ถ้าต้อง tail เพื่อลด token ให้ tail หลังจากเก็บ exit code แล้ว

## หมายเหตุ

ใน incident นี้ UtimesNanoAt failure เองแก้ด้วย `docker rmi` image ค้าง + `docker builder prune` (คืน 2.4GB) แล้ว rebuild ผ่าน — อาการนั้นคือ containerd snapshotter state เสีย ไม่ใช่โค้ดเรา

**How to apply**: ทุกคำสั่ง deploy/verify ที่รัน background ผ่าน ssh ต้องมี exit echo หรือ pipefail + ต้องอ่าน output ก่อนสรุปผล