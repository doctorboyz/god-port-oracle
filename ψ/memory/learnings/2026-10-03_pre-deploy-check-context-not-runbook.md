# ก่อน deploy อ่าน compose ของ service นั้น ไม่ใช่ทำตาม runbook เป๊ะ

**วันที่**: 2026-10-03 · **Issue**: — · **File**: docker-compose.vps.yml, ψ/HANDOFF.md

## เกิดอะไร

HANDOFF บันทึกจาก deploy 2026-09-23 ว่า "deploy รอบหน้าให้ up mt5b/c/d ก่อน รอ healthy แล้ว up train" (two-phase กัน terminal บูต 30 นาที) วันนี้ (2026-10-03) จะ rebuild `oracle-engine-train` เท่านั้น — เกือบทำ two-phase ตาม HANDOFF ทั้งที่ไม่จำเป็น เพราะเช็คจริงแล้ว:

- mt5b/c/d build จาก context `./docker/mt5` (Dockerfile อื่น, คนละ image กับ train)
- train build จาก context root — `up -d --build oracle-engine-train` จึงไม่ recreate mt5b/c/d เลย ไม่มีหน้าต่าง terminal บูต และ depends_on `service_healthy` ผ่านทันทีเพราะทั้งสามตัว healthy อยู่แล้ว

## บทเรียน

1. **Runbook คือบันทึกเหตุการณ์ ไม่ใช่กฎ** — ทุกครั้งก่อน `up --build` ให้เช็คจริงว่า service ที่จะ up ใช้ build context/image ไหน และ image นั้น share กับ service อื่นไหม — two-phase จำเป็นเฉพาะเมื่อจะ recreate mt5b/c/d จริง
2. **depends_on `service_healthy` เป็น gate แค่ตอน start** — service ที่ healthy อยู่แล้วผ่านทันที ไม่มีการรอ
3. **Pre-deploy checks ที่คุ้มค่าจริง**: TZ (`timedatectl` — cron 03:17 UTC ≠ 10:17 ในเครื่อง UTC), crontab เดิม (append ไม่เขียนทับ), host .env override (`grep ^REGIME_VOLATILE_SKIP` — host .env ชนะ compose default), ไม้เปิดก่อน recreate engine

## ใช้ยังไงต่อ

ก่อน `up -d --build <service>` ให้ตอบ 3 คำถาม: (1) service นี้ share image/context กับใคร (2) container ไหนจะถูก recreate จริง (3) recreate แล้ว state อะไรหาย — ตอบไม่ได้ = อ่าน compose ก่อน อย่าทำตาม HANDOFF เป๊ะ

**Related**: [[2026-10-02_compose-env-noop-class]] · [[2026-09-30_macos-crontab-hang-use-launchd]]
## ⚠️ แก้ไข (2026-10-03 ค่ำ — คอนคลูชันข้อ 2 ผิด พิสูจน์จาก production)

คืนเดียวกันหลังเขียน learning นี้: `docker compose up -d oracle-engine-train` (ไม่มี --build) **recreate mt5b/c/d จริง** — container เกิดใหม่ทั้งสามตัว 20:47 UTC ทั้งที่บทสรุปด้านบนบอกว่า "จึงไม่ recreate mt5b/c/d เลย"

สิ่งที่ต้องแก้ในความเข้าใจ:

1. **สิ่งที่ทำให้ compose recreate ไม่ใช่แค่ image** — มันคือ **config hash ของ service เปลี่ยน** (compose file, env_file interpolation, labels) — คืนนี้ `git pull` ก่อน up ดึง compose/env ที่แต่งต่างมา → hash เปลี่ยน → recreate ตาม แม้จะ up แค่ service เดียวและ dep ไม่ได้อยู่ใน argument
2. **framework 3 คำถามยังถูก** — แต่คำตอบต้องรวม "dep ถูก recreate ไหมเมื่อ config เปลี่ยน" ไม่ใช่แค่ "image share กันไหม"
3. **บทเรียนที่แท้จริงของคืนนี้: มี learning ที่ตรงเป๊ะ แต่ไม่ recall ก่อนสั่ง = ไม่มีค่า** — ก่อนทุก deploy ให้ `grep -l "compose\|recreate\|deploy" ψ/memory/learnings/*.md` แล้วอ่านทุกไฟล์ที่ match ก่อนอนุญาตตัวเองสั่ง `up` (โดนเอง 2026-10-03: learning 2026-09-23 compose-build-recreates-deps ทำนายเหตุการณ์ไว้ครบแต่ไม่ได้ถูกเปิดอ่าน)
