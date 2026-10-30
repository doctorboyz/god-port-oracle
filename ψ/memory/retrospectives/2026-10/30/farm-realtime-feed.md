# Retro 2026-10-30 — Farm Feed สลับเป็น Exness Realtime (era yfinance สิ้นสุด)

**Task**: ada-task-20261030-realtime-feed.md (T1-T6) — คำสั่งตรงจากคุณหมอผ่าน Hermes:
"แก้ feed timing ใช้ realtime อันเดียว ทั้ง paper และ demo"

## ทำอะไร

paper farm (16 variants, Mac mini) เปลี่ยน data source จาก yfinance GC=F (futures หน่วง 5-15 นาที) เป็น **Exness XAUUSD realtime จาก bridge mt5b บน VPS ผ่าน Tailscale** — paper กับ demo B/C/D เห็นราคาเดียวกันเป๊ะตั้งแต่ 30/10/26 10:00 UTC เพื่อให้เทียบ per-signal paper↔demo ได้จริง (gap ที่เหลือ = execution costs ล้วน)

ผลที่ตามมา: สัญญาณสองฝั่งยิง timing เดียวกัน · สถิติ league ก่อน 30/10 = era yfinance ห้ามนำไปเทียบกับ era ใหม่

## สิ่งที่เปลี่ยน (7 ไฟล์)

| ไฟล์ | อะไร |
|---|---|
| `scripts/paper_feed_fetcher.py` | โหมด `FEED_SOURCE=bridge`: BridgeFeed (persistent connection, read-only), era marker, backfill 74 วัน, no-yfinance-fallback, heal จาก foreign shutdown |
| `shared/market_calendar.py` | รับ flag `feed`: "cme" (เดิม, default) / "exness" (ใหม่) |
| `scripts/farm_watchdog.py` | อ่าน era marker เพื่อเลือกปฏิทิน + ข้อความ stale ไม่กล่าวหา Yahoo |
| `scripts/paper_farm_apply.py` + `farm_variants.yml` | fetcher service ได้ `FEED_SOURCE=bridge` + host/port + poll 60s (source of truth อยู่ yml) |
| `pyproject.toml` | ปัก `rpyc>=5.2.0,<6.0.0` ตาม server |
| `tests/test_paper_feed_fetcher.py` | +9 tests bridge mode (17 รวม) |
| `tests/test_watchdog_market_calendar_causal.py` | +11 tests ปฏิทิน Exness + era gate (31 รวม) |

## บั๊กจริง 2 ตัวที่เจอระหว่างทำ (ทั้งคู่เจอจากของจริง ไม่ใช่เดา)

1. **rpyc 6.0.2 vs server <6.0.0** — connect จาก Mac ติด `invalid message type: 18` ทั้งๆ ที่ VPS engines ใช้ได้ปกติ: Mac venv มี rpyc 6 แต่ server (Wine) ปัก <6 ตาม entrypoint.sh · แก้: downgrade 5.3.1 + ปักใน pyproject กัน build ใหม่ดึง 6.x มาอีก
2. **Shared-terminal shutdown (เจอสด 10:02 UTC, สองชั้น)** — deploy แล้ว fetcher ตายถาวรหลังรอบแรกๆ: demo engine ใช้ pattern connect→copy→**shutdown** ต่อ cycle ซึ่งฆ่า handle MT5 ระดับ process ของ terminal ที่แชร์กัน — rpyc connection ของ fetcher ยังรอดอยู่จึงไม่มีข้อยกเว้น แค่ได้ข้อมูลว่างตลอดไป · ชั้นแรก: แก้เป็น re-initialize เมื่อ payload ว่าง · แต่ deploy แล้วยังตายอีก (10:12) — ชั้นสอง: server คืน **`"[]"`** (string ว่างแต่ *truthy*) ทำให้ `if not payload` ไม่จับ ข้ามไปโดนเงียบที่ df.empty · แก้จริง: แยกเป็น `_copy()` ตัดสินความว่าง *หลัง parse* แล้ว heal ตาม · fetcher ไม่เรียก shutdown เองเลย · พิสูจน์ RED→GREEN ด้วย TestBridgeSharedTerminalCausal (fake คืน "[]" ตรงพฤติกรรม server จริง)

## บทเรียน (เก็บเป็นกติกา)

- **อ่าน gap จาก diff() ระวังทิศ** — label ของ `series.diff()` คือแท่ง *หลัง* ช่องว่าง ผมอ่านกลับครั้งแรก สรุปปฏิทิน Exness ผิดไป 1 ชั่วโมง (จะทำ watchdog false-ALERT ทุกค่ำ) — จับได้เพราะเทียบจำนวนแท่ง/วันกับ gap อีกที · กติกา: ก่อนสรุป session hours จาก diff ต้อง reconcile กับ bar-count ต่อวัน
- **ปฏิทินสองค่าตาม feed era ต้องผูกกับ marker ไม่ใช่ guess** — CME กับ Exness ชั่วโมงเดียวกันพอดีในช่วง CDT จะพินไว้ด้วย era-flip ต้องใช้วันหลัง DST (พ.ย.) — test ที่พิน behavior ต้องเลือกเวลาที่ system สองอัน *จริงจัง* ต่างกัน
- **เทอร์มินัลแชร์กัน = lifecycle ของคนอื่นคือ input ของเรา** — persistent connection ตายเงียบๆ เมื่อ client อื่น shutdown; สัญญาณที่บอกว่า "connection ok" ไม่ใช่ "terminal alive" — ต้องมี self-heal + log ทุก path ที่คืน None

## เกณฑ์ T5 — หลักฐานจริง (จับเวลา UTC)

1. **Backfill ≥5 วันต่อเนื่อง**: M5 = 14,999 แท่ง 2026-08-16→10-30 (74.4 วัน) · gap>15น. ทั้งหมด 54 ตัว อธิบายได้ 52 (halt รายวัน 20:55→22:00 + weekend ศุกร์ 20:55→อาทิตย์ 22:00) · เหลือ 2 = Labor Day 9/7 + ช่องแท่งแรกอาทิตย์ 9/13 (broker-side) — ไม่มี gap ยักษ์จาก era mix
2. **แท่งสด**: แท่ง 10:05 (ปิด 10:10:00) เขียน 10:10:05 · poll 60s · อายุข้อมูล ~5 นาทีตาม cycle M5 (< 6 นาที) · ต้นฉบับ: bridge bar 09:40 อายุ 3.5 นาทีตอน probe
3. **Engines 16 ตัว**: farm1+farm2 Up (healthy), 48 cycles DRY-RUN ใน 15 นาที, อ่าน 14,999 แท่งได้, ราคา signal = 3991.706 = แท่ง bridge ปิดล่าสุดเป๊ะ, ไม่มี error/warning/hard-block/stale เลย
4. **ไม้ใหม่ entry ≈ bridge**: ไม่มีไม้เปิดใหม่หลัง 10:00 ในจังหวะที่ตรวจ (ตลาด ranging = HOLD ตามระบบ — ไม้ล่าสุด 06:45 เป็น era เก่า) · กลไกพิสูจน์แทน: entry_price ของเครื่อง = close ของแท่ง bridge ล่าสุด (เห็นใน log ตรง price=3991.706) — ไม้แรกที่เปิดใน era ใหม่จะยืนยันอีกชั้น ให้ Hermes ตรวจไม้แรกตอนอ่าน league ครั้งหน้า
5. **Test suite**: 1140 passed, 1 skipped (11:16 น.) + แก้ fetch เพิ่มหลังจากนั้น → รอบสุดท้ายผลอยู่ใน handoff

## Era สิ้นสุด/เริ่ม (สำหรับสถิติ)

- **Era yfinance สิ้นสุด 30/10/26 10:00:47 UTC** (fetcher เริ่ม era switch)
- **Era bridge เริ่ม 30/10/26 10:00:47 UTC** — history แทนที่ทั้งชุด 14,999/5,999/799 แท่ง (M5/H1/D1)
- การเทียบ paper↔demo ใช้ era bridge เท่านั้น

## ค้าง/ติดตามต่อ

- [ ] หลัง DST สหรัฐฯ **1/11/26**: probe ปฏิทิน Exness อีกรอบ (คาดไม่เลื่อนเพราะ server GMT+0 แต่พิสูจน์ด้วยของจริง) — ถ้าเลื่อน watchdog จะเริ่ม false-alert ช่วง halt
- [ ] ไม้แรกของ era ใหม่: เทียบ entry_price กับ bridge จริง (T5 ข้อ 4 ชั้นสอง)
- [ ] backup `data/paper-feed.bak-yfinance-20261030/` (1.5MB) เก็บไว้ในเครื่อง — ลบเมื่อ era bridge นิ่งแล้ว (หลัง validation 1 สัปดาห์)