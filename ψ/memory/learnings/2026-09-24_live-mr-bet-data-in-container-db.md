---
name: live-mr-bet-data-lives-in-vps-container-db
description: สถิติ live mr-bet B/C/D ต้อง query จาก oracle_train.db ใน container บน VPS ผ่าน RPyC bridge — local DB เป็น snapshot เก่า
metadata:
  type: project
---

ตอนตรวจผลเทรด live ของ mr-bet (บช. B/C/D บน branch `mr-bet-bcd`):

1. **อย่า query local `data/oracle_vps.db`** — เป็น snapshot เก่า ข้อมูลหยุด 2026-05-26 (เช็ค `ls -la` mtime ก่อนใช้ DB ใดๆ เสมอ) ข้อมูลจริงอยู่ที่ **`/app/data/oracle_train.db` ใน container `oracle-engine-train` บน VPS** (`ssh vpsdeluna`)
2. **MT5 bridge ไม่ใช่ HTTP** — เป็น RPyC (`docker-compose.vps.yml` เขียนไว้ใน comment) ยิง curl เปล่าเปลือง ให้ใช้:

```python
# ใน container oracle-engine-train
from metty.bridge.client import MT5Bridge
from metty.core.models import AccountConfig
cfg = AccountConfig(name='B', bridge_host='mt5b', bridge_port=8001)
info = MT5Bridge(cfg).fetch_account_info_sync()  # balance/equity จริง
```

3. **สถิติ mr-bet แยกช่วงก่อน/หลัง fix RR (commit 5e91619, 2026-09-23)** — ไม้ก่อน fix ใช้ RR 2.5 ถ้าปนกันจะได้ PF หลอก (B ดูเว่อร์เพราะไม้เก่า 2 ไม้) ต้อง filter `timestamp >= '2026-09-23T19:00'`
4. เกณฑ์ตัดสิน config: **PF>1.3 หลัง cost ที่ 200+ ไม้** — ต่ำกว่านั้นห้ามสรุปอะไร

5. **ไม่มีไม้ใหม่ ≠ engine ตาย** — ตรวจ 3 ชั้นก่อนสรุป: (1) `docker logs oracle-engine-train --since 3h` เห็น collector วน + "MT5 re-login successful" = engine รันอยู่ (2) query `rejected_signals` แยกบช. — ถ้าเห็น `entry_hour_blocked` แปลว่าสัญญาณมีแต่โดน entry window ปัดตกตาม design (จันทร์ 28/9 เงียบทั้งวันเพราะ hour 11-14 UTC ไม่อยู่ใน window) (3) `docker ps` status `unhealthy` ของ oracle-engine-train ไม่ใช่สัญญาณ engine ตาย — บ่อยครั้งเป็น healthcheck surface เฉยๆ, engine ยัง cycle ปกติ. อนึ่ง — ไม้ศุกร์คืน B -20.56 / D -15.95 เกิดช่วงเดียวกัน ต้องระวัง correlated SL ช่วง news

**Why:** เสียเวลาไป 3-4 query กับข้อมูล stale + protocol เดาผิด กว่าจะเจอของจริง
**How to apply:** ทุกครั้งที่ตรวจสถานะ/ผลเทรด live — ssh VPS → docker exec → container DB + RPyC เท่านั้น