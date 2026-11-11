# Retro — true-bet farm (T0-T5) · 2026-11-07 โจทย์ / 11/11 ส่งมอบ

> โจทย์: ada-task-20261107-true-bet-farm.md (คุณหมออนุมัติผ่าน Hermes 7/11)
> แก่น: "แก่นแท้ของ bet farm คือจุดเข้า + สัญญาออกตายตัว (TP/SL ตั้งแล้วเดินหนี)"

## สิ่งที่ทำ

9 cells (จุดเข้า 3 ครอบครัว × geometry 3) seed $1,000/cell รวม $9,000 เสมือน
พร้อม backtest บนประวัติ M5 ทั้งหมดใน feed store ก่อนเปิดเก็บสด — additive
ล้วน: farm1/farm2, fetcher, VPS config, บช.จริง ไม่โดนแตะแม้บรรทัดเดียว
(diff เชิงรับรอง: paper-fetcher/farm1/farm2 byte-identical, +78 บรรทัดคือ
service ใหม่ล้วน)

## สถาปัตย์ (broky/bet/ — module ใหม่ไม่ปนกับ generator เดิม)

- `entries.py` — จุดเข้า 3 ครอบครัว deterministic จากแท่งปิดเท่านั้น
- `contract.py` — สัญญา: TP/SL ตายตัวตั้งตอนเข้า ถือจนจบ, resolution ด้วย tick
  (สด) / bar-conservative (backtest, รายงาน 2 ขอบ), MFE/MAE + ลำดับเหตุการณ์
  + resume หลัง restart ได้จาก state ใน DB
- `engine.py` — 1 ไม้/cell, เข้าที่ open แท่งถัดไป (backtest) = tick ปัจจุบัน
  (live), compounding ตามแบล็อต, ใช้ primitive เดียวกันทั้งสองทาง
- `store.py` — SQLite แยกของตัวเอง (bet_cells/bet_bets) ที่ data/farm/bet/bet.db
- `cells.py` — อ่าน cell จาก farm_variants.yml จุดเดียว (live+backtest+test
  เห็น definition เดียวกัน)
- `scripts/bet_engine.py` — container สด (poll 60s) · `scripts/bet_backtest.py`

## T0 — probe tick: ได้ tick ใช้สดจริง

probe สด bridge mt5q (read-only): `copy_ticks_from(symbol, from_epoch, count,
flags)` มีและใช้ได้ — **from = epoch date** (ยืนยันด้วยการขอ 10 นาทีล่าสุด ได้
2,613 ticks ใน 0.2s) · flags=1 (COPY_TICKS_INFO, bid/ask) ใช้ได้ · flags=2 (ALL)
คืน [] กับ feed Exness นี้ · symbol จริงบน terminal คือ XAUUSD ไม่ใช่ XAUUSDm
(ต่างจากที่เคยเข้าใจ) → **live resolution = tick ตัดสิน (ไม่มี ambiguous),
fallback = bar-conservative เมื่อ tick ไม่มา (halt/fetch fail)** ส่วน backtest
74 วันใช้ bar 2 ขอบตามโจทย์ (tick ย้อนหลัง 10 เดือนมีจริงแต่หนักเกินงบ — ไม่ทำ)

## ครอบครัวจุดเข้า — เหมือน/ต่างจาก P22 ตรงไหน (ตามที่โจทย์ให้ document)

- **A (MR-Boll extreme)**: ใช้แกนเดียวกับ MR path ของฟาร์มเป๊ะ —
  ADX(14)<20 (ประตูเดียวกับ `if latest_adx < 20` ใน generator) + band
  position (close−lower)/(upper−lower) ≤ 0.15 → BUY / ≥ 0.85 → SELL
  (threshold 0.85 ตาม P22), indicator ชุดเดียวกัน (broky.indicators)
  **ตัดออก**: composite confidence (boll+MACD+volume, cap 0.65) ·
  ranging penalty ×0.70 · min_confidence 0.55 · hour gates (P22 ก็ปิดอยู่แล้ว)
  · spread gate · counter-trend gates · trailing TP / time stop (สัญญาออก
  ถูก contract แทนทั้งหมด)
- **B (Breakout 20)**: close ทะลุ high/low สูงสุดของ 20 แท่งปิดก่อนหน้า
  (แท่ง signal ไม่นับในหน้าต่างของตัวเอง) — strict greater
- **C (Momentum-pullback)**: ทิศตาม EMA(50) + แท่ง signal แตะ EMA(10)
  (low≤EMA10 ฝั่ง long) แล้วปิดกลับฝั่งเดียวกับเทรนด์

## บั๊กจริงที่เจอและจับได้สด (บทเรียนสำคัญสุดของงานนี้)

**env key มีขีดกลาง = runtime ตัดทิ้งจาก PID-1 environ**: deploy แรก
docker inspect / docker exec เห็น `BET_FAMILY_BET-B10=B` ครบ แต่ process
จริง (อ่านจาก /proc/1/environ) ไม่มี key พวกนี้เลย → boot seed เขียนทุก
cell เป็น default A/TP6:SL12 (จับได้เพราะ query DB หลัง boot แล้วเทียบกับ yml)
ตัวเดิมไม่เคยเจอเพราะชื่อ account เป็น P<digits> ไม่มีขีด · แก้โดย sanitize
แค่ KEY (BET-A6 → BET_A6) ค่า cell_id ใน DB/รายงานยังเป็น BET-A6 ตามโจทย์ ·
ล็อกด้วย causal test 2 ชั้น: key ทุกตัวต้อง match ^[A-Za-z0-9_]+$ + env-parser
ต้องเท่ากับ yml loader ครบทุก cell (รอบแรก test ตัวนี้เช็คแค่ BET-A6 ซึ่งค่า
ตรงกับ default เลยสีเขียวหลอกตา — ตัดตำนานด้วย loop ทุก cell)

**บทเรียนที่ควรยกเป็นกติกา**: ตรวจรับระบบที่กิน env ต้องอ่าน
/proc/1/environ ของ container จริง ห้ามเชื่อ docker inspect (config จริง
แต่ process ไม่ได้รับ) · และ env key ห้ามมีตัวอักษรนอก [A-Za-z0-9_]

## เหตุการณ์สอง: WAL สลายตอน recreate container (เจอสด 15:20-15:33 UTC 11/11)

ไม้เปิด 3 ตัว (C6/C10/C8 SELL @ 4013.25) commit แล้ว 15:20 อยู่ใน
bet.db-wal บน bind mount — พอ `docker compose up -d --build` recreate
container 15:33 พบ WAL หาย ไฟล์หลักมีแต่ rows เก่า (cells ที่ checkpoint
ไปก่อนหน้า) ไม้ทั้ง 3 หายจากประวัติ (พอร์ต $9,000 เสมือน ไม่กระทบยอด —
ไม้ยังเปิด ไม่เคยจอง pnl) · พิสูจน์แล้วว่าไม่ใช่ความผิดของ commit path:
repro native (commit→SIGKILL→เปิดใหม่ = รอด) และ repro ใน docker จริง
(bind mount + WAL + SIGKILL = รอด) — engine ไม่มีโค้ดลบแถวเลย ·
สิ่งที่แน่คือ WAL ที่ยังไม่ถูก fold ไม่ควรเป็นตัวแบกรับข้อมูลระหว่าง
redeploy บน virtiofs

**แก้ที่ราก**: bet_engine ติดตั้ง handler SIGTERM/SIGINT → `conn.close()`
ก่อนตาย = SQLite fold ทุก frame ลงไฟล์หลัก + ลบ sidecars → redeploy
ไม่พึ่งพาความรอดของ WAL อีก · causal test 2 ขอบ (มี handler → แถวอ่านได้จาก
ไฟล์หลักเปล่าๆ / ไม่มี handler → แถวอยู่ใน WAL เท่านั้น — คือหน้าต่างที่
กลืนไม้ทั้ง 3) · ตรวจรับสดครั้งที่สอง: checkpoint มือก่อน redeploy →
ไม้ B 3 ตัวรอด restart ครบ

**กติกาใหม่**: ก่อน recreate container ที่ถือ SQLite-WAL บน bind mount —
ถ้าโค้ดยังไม่มี graceful close ให้ `PRAGMA wal_checkpoint(TRUNCATE)`
ก่อนเสมอ · และห้ามเชื่อว่า docker stop = ข้อมูลถูก flush

## เหตุการณ์สาม: netref grind + .get โดนบล็อก — resolution สดไม่เคยทำงานเลย

deploy ครั้งแรกเปิดไม้ได้แต่ **ไม่เคย resolve ไม้สักตัว** จนมาเจอตอนตรวจ
สด 15:33-15:47 UTC 11/11: บั๊ก 2 ตัวซ้อนกัน —
(1) rpyc คืน tick เป็น dict NETREF ทุกการเข้าถึง field = round trip
~50ms ไป VPS → `list(remote)` เพียงลิสต์เดียว 2,530 ticks กิน 136
วินาที (probe ตอน T0 ที่ว่า "0.2s" วัดแค่ len ไม่เคยแตะ field)
(2) rpyc server บล็อก `.get()` บน dict netref → `tick.get("time_msc")`
crash ตั้งแต่ tick แรก — exception โดนกลืนเป็น "cycle failed" แล้ววนใหม่
ทุก cycle เป็น crash-loop เงียบ · พิสูจน์ด้วย repro ใน container จริง:
resolve เดิน 136.5s แล้วตายที่ .get

**แก้**: `str()` ทั้ง remote list = round trip เดียว (server render repr
~240KB สำหรับ 10 นาที ticks) → `ast.literal_eval` ในเครื่อง = 0.3s
รวม (เร็วขึ้น ~450 เท่า) + อ่าน field ด้วย getitem ธรรมดา · พิสูจน์สด
หลังแก้: ไม้แรกในประวัติฟาร์ม resolve ด้วย TICKS จริง (BET-B6 → TP
+$7.25 ใน 0.5s, ยอดเข้า $1,007.25, compounding ทำงาน) · ล็อกด้วย
causal test 4 ตัว (dict ธรรมดา / .get ต้องได้ / round-trip counter ≤3 /
empty list เป็น [] ไม่ใช่ None)

**บทเรียน**: rpyc ข้อมูล bulk ต้อง materialize รอบเดียว (str/repr/json)
ห้ามแตะ field ทีละชิ้นผ่าน netref — และ probe ความเร็วต้องวัดที่
"การใช้งานจริง" (อ่านทุก field) ไม่ใช่แค่ len

## ผล backtest (ตัวเลขจริง — spot-check อิสระ 72 ไม้ + ประตู ADX 10 จุด)

ทุก cell WR ต่ำกว่า breakeven เล็กน้อยและ E$/ไม้ ติดลบหลังหัก spread
($0.20/oz ทุกไม้) — ไม่มีจุดเข้าไหนใน 3 ครอบครัวนี้ + 3 geometry นี้ ที่
แสดง edge ชัดเจนบนข้อมูล 17,256 แท่ง (≈63 วันเทรด) · ใกล้สุดคือ C6
(pullback × TP6:SL12): WR pes 66.9% vs BE 66.7% แต่หลังหัก spread =
-0.22 $/ไม้ · geometry 8:8 แพ้ทุกครอบครัว (WR ~48-50% ต่ำกว่า BE 50%)
· n สูงสุด 1,344 ไม้ ต่ำสุด 363 — สถิติเพียงพอสรุปว่า "ไม่มี edge โผล่"
รายละเอียด: ψ/outbox/bet_backtest_2026-11-07.md

คำอ่านสำหรับคุณหมอ: จุดเข้าแบบง่าย 3 แบบนี้ (ไม่มี volume/HTF confirm)
วัดแล้วเท่าทุนก่อนค่าใช้จ่าย แพ้หลังค่าใช้จ่าย — ถ้าจะต่อ ทางที่น่าลองคือ
เพิ่มตัว confirm (volume/ทิศ HTF) หรือเปลี่ยน geometry ไม่ใช่เพิ่มจำนวนไม้

## สิ่งที่ค้าง / ต่อยอดได้

- live วิ่งแล้ว (เปิดไม้แรก 15:20 UTC 11/11 — C6/C10/C8 SELL ราคา
  geometry ตรงเป๊ะ) — สะสม WR จริงเทียบ backtest เอง
- ปฏิทิน watchdog ยังใช้ halt 21:00-22:00 ของยุคก่อน DST (ค้างจากงานก่อน)
- tick-resolution ใน backtest (bridge มี tick ย้อนหลัง ~10 เดือน) ไม่ทำ
  ตามงบ — ถ้าอยากได้ 2 ขอบแน่นขึ้น
