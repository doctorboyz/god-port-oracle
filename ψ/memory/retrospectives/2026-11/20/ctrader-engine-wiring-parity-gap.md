# Retro — P22 cTrader engine wiring: หยุดที่ parity gap + ส่ง lotSize fail-loud (20/11)

**งาน**: P22 champion บน cTrader demo (IC1/IC2) ผ่าน CTraderBridge — engine wiring
**ผล**: งานหลัก (ข้อ 1-7) หยุดตามกติกา · ข้อ 8 (lotSize fail-loud) ส่งครบ · รายงาน
`ψ/outbox/ctrader_engine_wiring_blocked_2026-11-20.md`

## เกิดอะไร

รับ dispatch พร้อม prerequisite ครบสามข้อ แต่ก่อนแตะอะไร สำรวจ engine path จริง
(grep + อ่าน `live_trader.py` / `oracle_runner.py` / contract test) — พบว่า design intent
"engine ไม่ต้องแก้เพราะ parity ครบ" **ไม่จริงในสามชั้น**:

1. `LiveTrader` ปฏิเสธชื่อบช. `IC1`/`IC2` ตั้งแต่ constructor (`ACCOUNT_IDS` static
   A-D + P-account เท่านั้น) — compose ขึ้นมาก็ crash loop
2. `oracle_runner.py` **ไม่ได้สร้าง bridge ของ trading path เลย** — "จุดเลือก bridge"
   ที่ brief สั่งให้ทำไม่มีแขนไปถึง engine: `live_trader` self-serve `MT5Bridge`
   ภายใน method 9 จุด
3. ผิว parity ที่ contract test งาน 5 ล็อก = 9 async methods ครบจริง แต่ engine
   เรียก **sync wrappers 7 ตัว + `get_deal_history`** ที่ CTraderBridge ไม่มี —
   swap ได้ตัว แต่ equity stamp/feed/spread/reconciliation พังทั้งสาย

ทั้งสามชั้นอยู่ใน `metty/` = IRON — brief เขียนไว้เองว่า "ถ้าเจอ parity gap =
หยุดแจ้ง Hermes อย่าแก้เอง" → หยุด แล้วเขียนรายงานพร้อมทางเลือก A/B/C

## สิ่งที่ทำแทน (ไม่ให้ session จบเปล่า)

ข้อ 8 ของงาน (ไม่ถูกบล็อก): lotSize fallback 3 จุดใน `ctrader/bridge.py`
(`or 100` / `= 100.0` / `.get(id, 100.0)`) → `CTraderApiError` fail-loud
สามรหัส (`LOT_SIZE_MISSING` / `LOT_SIZE_NOT_FOUND` / `LOT_SIZE_NOT_LOADED`)
ตาม CPT: test ก่อน (5 ตัว) → RED พอดี 5 → fix → GREEN 5 → RED proof ยืนยันซ้ำ
ด้วย git stash (ของเก่าแดงพอดี / คืนค่าเขียวพอดี) — จุดสำคัญที่ mock จับได้:
**ก่อนแก้ order หลุดออกไปจริง** เมื่อ lotSize ไม่ถูกโหลด (DID NOT RAISE =
NEW_ORDER_REQ ออกจากมือด้วย scale ที่เดา 100× ผิด)

## บทเรียน

1. **"ไม่ต้องแก้ engine เพราะ parity" ต้องพิสูจน์ด้วย call-site inventory ไม่ใช่
   contract list** — contract test ล็อกสิ่งที่คนเขียนนึกออก ไม่ใช่สิ่งที่ engine
   เรียกจริงทั้งหมด · ครั้งหน้างาน swap interface: เริ่มด้วย
   `grep "bridge\.\w+(" metty/execution/live_trader.py` ก่อนเขียน PARITY_METHODS
2. **Dispatch ที่เชื่อ design intent มากกว่าโค้ด = เสี่ยง** — brief บอก
   "engine wiring ไม่ต้องแก้ engine" แต่ไม่มีบรรทัดไหนของ runner สร้าง bridge
   เลย · คำว่า "จุดเลือก bridge ใน oracle_runner" ฟังดูเหมือนมีจุดอยู่แล้ว —
   จริงๆ คือสิ่งที่ยังไม่มีเลย และสร้างไม่ได้โดยไม่แตะ metty
3. **ค่า fallback ที่ "ถูกโดยบังเอิญ" บนสูตรเก่า = ระเบิดเวลาบนสูตรใหม่** —
   ครั้งนี้ถอดได้ครบตามที่ธงไว้ตั้งแต่ 11/11 · รูปแบบเดิมกับ USC trap:
   เลขตรงกันโดยบังเอิญคือสิ่งที่ test ธรรมดาไม่มีทางจับ
4. **หยุดเร็วถูกกติกาดีกว่าฝืน** — ถ้าฝืนเขียน compose กับ runner เพื่อให้
   "ส่งงานได้" จะได้ container crash loop + equity stamp หลอก (DB-derived
   paper equity แสดงเป็น heartbeat ของบช. cTrader) — โกหกเงียบๆ สองชั้น

## ค้างเปิดส่ง Hermes

- เลือกทาง A (แนะนำ: LiveTrader รับ bridge factory + sync wrappers ฝั่ง ctrader)
  / B (ชื่อ P-account) / C (farm-style) — รายละเอียดในรายงาน outbox
- งาน wiring ข้อ 1-7 ทั้งหมด ยังไม่ถูกแตะแม้บรรทัด (ไม่มี compose ใหม่ / ไม่มี
  runner change / ไม่มี guard corpus)
- ธงเดิม: paper_farm_apply CTRADER_* block · farm_watchdog era · token refresh
