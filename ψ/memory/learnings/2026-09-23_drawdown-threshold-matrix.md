---
name: drawdown-threshold-matrix
description: ค่า DrawdownProtector ที่ deploy จริงต่อบช. (canonical ณ 2026-09-23) — แทนที่ system report เก่า 2026-07-01 ที่เขียน 20/30/30% cd4h ซึ่งไม่เคยตรงกับ demo
metadata:
  type: reference
---

# Drawdown Threshold Matrix (ค่าที่ deploy จริง)

**Source**: `docker-compose.vps.yml` (verify ด้วย `scripts/verify_deploy.py` — อ่านจาก DrawdownProtector object จริง)
**Verified**: 2026-09-23 · supersedes ψ/outbox/2026-07-01_system-report.md (ไฟล์ต้นทางถูกลบ/ไม่มีใน vault แล้ว)

## ค่าจริงต่อบช.

| Account | Daily | Weekly | Account-level | Cooldown | ที่มา |
|---|---|---|---|---|---|
| A (Real-A) | 5% | 10% | 20% | 4h | compose lines 152-155 |
| B (demo, mr-bet-B) | 10% | 20% | 50% | 2h | compose lines 332-335 |
| C (demo, mr-bet-C) | 10% | 20% | 50% | 2h | compose lines 336-339 |
| D (demo, mr-bet-D) | 3% | 6% | 15% | 4h | compose lines 340-343 |

## ประวัติความขัดแย้ง (ISSUE-073)

System report 2026-07-01 เขียนไว้ 20% daily / 30% weekly / 30% account, cd 4h — **ไม่เคยตรงกับที่ deploy**:
- Demo B/C เข้มกว่า doc ที่ daily/weekly (10/20 vs 20/30) แต่หลวมกว่าที่ account-level (50 vs 30) และ cooldown สั้นกว่า (2h vs 4h)
- ตัวที่เป็นปัญหาจริงในทางปฏิบัติคือ account-level 50% ของ B/C — กว้างเกินจนเกือบไม่มีทางถึง แต่ตั้งใจไว้แบบนั้นเพราะ demo ทดลอง strategy
- D แคบสุด (3/6/15%) เพราะเป็นบช.ที่คุณหมอกำหนดให้เฝ้าระวังเงื่อนไขบัญชี

**คำตัดสิน (2026-09-23, ISSUE-073 ปิด)**: ค่าใน compose = canonical เสมอ — doc นี้คือ mirror ของ compose ณ วันที่ verify ไม่ใช่แหล่งความจริง ถ้าค่าสองที่ไม่ตรงกัน ให้เชื่อ compose + verify_deploy.py

## Related

- [[2026-09-23_env-set-does-not-mean-env-used]] — วิธี verify ค่าจริงจาก object
- ISSUE-073 — ที่มาของการบันทึกนี้