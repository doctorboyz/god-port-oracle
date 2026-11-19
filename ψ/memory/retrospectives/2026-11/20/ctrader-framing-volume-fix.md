---
date: 2026-11-20
task: cTrader connection-day bugs — FrameDecoder framing + volume lotSize-in-cents
session: Claude (maw queue job, dispatched 20/11)
---

# Retro: cTrader framing + volume fix (bug จาก wire จริงวันแรก)

## บริบท

19/11 เป็นวันที่ `ctrader/` แตะ network จริงครั้งแรก (Spotware อนุมัติ
app + แลก token สำเร็จ) raw probe พิสูจน์ auth ผ่านจริงทั้งสอง host แต่
`CTraderBridge` ใช้งานไม่ได้เลย — และ deep probe จับ bug ที่สองใน
order path รวมเป็น brief เดียวสองงาน

## สิ่งที่ทำ

1. **Framing**: docs ทางการระบุ serialization JSON แต่ไม่ระบุ framing —
   สมมติฐานเดิม "newline-terminated" (ยืมจาก WebSocket mode) ผิดบน TCP
   เปล่า: server เขียน JSON objects เรียงติดกันไม่มี delimiter
   (3 เฟรม 2 host นับ byte ครบพอดีที่ `}` ไม่มี `\n`) → `FrameDecoder`
   รอ newline ที่ไม่มีวันมา → `_await_response` timeout ทุก message →
   `connect()` ล้มทุกครั้ง · แก้ด้วย incremental
   `json.JSONDecoder().raw_decode` (stdlib — ตามกติกา repo) API
   `feed()/pending()` เดิม 100% → transport/bridge ไม่แตะแม้บรรทัด
2. **Volume**: `.proto` ใน repo เองเขียนชัด `lotSize = 30; // in cents`
   แต่โค้ดคูณ `VOLUME_CENTS_PER_UNIT (100)` ซ้ำอีกชั้น → 1 lot จริง
   (lotSize=10000 จาก wire) = 1,000,000 cents = **100 lots** oversize
   100 เท่า (คลาส USC trap) · default เก่า `DEFAULT_LOT_SIZE=100` ถูก
   **โดยบังเอิญ** (100×100=10000) จน mock-test เขียวหลอกๆ — แก้เป็น
   `volume_cents = lots × lotSize` + default 10000 ตาม wire

## หลักฐาน (จาก artifact จริง)

- RED proof งาน 1: stash คืน decoder เก่า → framing causal แดง 14
  (รวม (a)(b)(c) = replay byte จริงจาก probe 19/11) control legacy
  3 ตัวเขียวทั้งสองฝั่ง → pop เขียว 17/17
- RED proof งาน 2: stash คืนสูตรเก่า → volume causal แดง 4 พอดีตัว
  scale contract (round-trip invariant 2 ตัวเขียวทั้งสองฝั่ง — ตัวถูก
  ตัวแรกที่ต้องระวัง) → pop เขียว 6/6
- ชุด ctrader รวม 6 ไฟล์: 158 passed 0 failed · ruff สะอาด
- Full suite: ดู handoff — baseline 1788 (0827310) + ใหม่ 23 = คณิต
  ต้องตรงเป๊ะ

## บทเรียน

1. **บทเรียนใหญ่ประจำสาย: "ตรวจสิ่งที่ mock ตอบไม่ได้บน demo" — README
   งาน 5 เตือนไว้เองตั้งแต่แรก และวันนี้มาถึงจริง**: สมมติฐาน framing
   ที่ไม่มี document รองรับผ่าน mock มา 2 สัปดาห์ แต่ตายทั้งระบบบน
   wire จริงวันแรก · ครั้งหน้าเวลา implement protocol ที่ docs ไม่
   ระบุจุดไหน ให้ถือว่าจุดนั้น = ต้อง probe จริงก่อนเชื่อ
2. **Default ที่ถูกโดยบังเอิญ อันตรายกว่า default ที่ผิดชัดๆ** — มัน
   ทำให้ test เขียวหลอกๆ และพรางสูตรผิดทั้งแถว (fixture lot_size=100
   ใน bridge tests ก็เป็นคนร้ายร่วม) · ทางแก้คือ pin ค่าจริงจาก wire
   ลง default + test one-way (ไม่ใช่แค่ round-trip ที่ invariant กับ
   การคูณ-หารซ้ำสองฝั่ง)
3. **คำอธิบายเดียวที่ทำให้ decoder แยก incomplete/garbage ได้**:
   parser ชนท้าย buffer (pos ≥ len) / unterminated string / literal
   กำลังโต = รอ · parser เจอ byte ที่ผิดกลาง text = raise ·
   ตรวจด้วย error shape จริงจาก `raw_decode` ก่อนเขียน heuristic
   (probe ด้วย python บรรทัดเดียวก่อนเขียนโค้ด ป้องกันเดาผิด)
4. **Iron lock ช่วยกันบั๊กที่เหลือ**: bridge fallback lotSize=100.0
   สามจุดเป็นสเกลผิดในสูตรใหม่ แต่ lock กีดกันงานนี้ → จดเป็นค้างเปิด
   ใน handoff พร้อมเลขบรรทัด แทนที่จะลัดคิวแตะ — scope discipline ทำงาน
   จริงเมื่อ queue มีเจ้าของเดียวต่อ job

## ค้างเปิด

- Hermes acceptance บน wire จริงผ่าน bridge (list_accounts /
  get_account_info / get_candles / lotSize 10000 ก่อน order test)
- bridge lotSize fallback สามจุด → fail-loud หรือ 10000 (งานถัดไป)
- ProtoOATrader demo balance=None (จดไว้ใน handoff ข้อ 2)
- ค้างเดิม 18/11: paper_farm_apply CTRADER_* block · watchdog
  calendar era ctrader · token refresh flow
