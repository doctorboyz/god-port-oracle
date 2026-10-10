# เกทที่พึ่ง bridge ต้องประกาศ fail-direction ชัด — และถูกทดสอบทั้งสองทาง

**วันที่**: 2026-10-02
**บริบท**: paper farm brokerless — สัญญาณจริง 7 ไม้ของ P20 ตายที่ `spread unavailable` ทั้งหมด

## อาการ

Farm ไม่มี MT5 (brokerless) → `_get_current_spread()` คืน None เสมอ → `run_once`
skip ทุกไม้ที่รอดเกทอื่นมาด้วย `spread unavailable (MT5 disconnected?)` — 0 ไม้ตลอดไป
ทั้งที่ plan ตอนสร้าง farm เขียนไว้ว่า "Spread gate จะ inert (None → ผ่าน)" — ความเข้าใจ
ผิดตั้งแต่ยังไม่ deploy และไม่มี test จับเพราะไม่ได้ทดสอบข้อสมมตินั้นเลย

## ต้นตอ

- เกท spread เป็นแบบ **fail-closed** (None = skip) — ถูกต้องสำหรับ live เงินจริง (spread
  จริงมีผลต่อ SL/TP/cost) แต่ฆ่าสภาพแวดล้อมที่ bridge ไม่มีอยู่เลย
- ระบบมีสองสภาพแวดล้อมที่ต้องการทิศทาง fail ต่างกัน แต่โค้ดฝังทิศเดียว

## กฎที่ใช้ต่อ

1. **เกทที่พึ่งข้อมูลจากภายนอก (bridge/feed/API) ต้องประกาศ fail-direction ใน docstring
   และมี env knob กำหนดทิศทางสำหรับสภาพแวดล้อมอื่น** — อย่าฝัง hardcoded เพราะ
   "correct for live" ไม่ได้แปลว่า "correct everywhere"
2. **ค่า mock ไม่ใช่แค่ "ให้ผ่าน" — ต้องไล่ดูว่าค่านั้นไหลไปถึงจุดไหนบ้าง**: spread ค่าเดียว
   ถูกใช้ 4 จุด (max-spread gate, ML features, cost coverage, fill anchor + spread_at_entry)
   เลือก 0 = ทำให้หลายเกทตาบอดเงียบๆ; เลือกค่าจริง (20 pts = $0.20) = ทุกเกทยังทำงาน
   อย่างมีความหมาย
3. **Lock ค่าตั้งใน contract test ด้วยช่วงที่มีเหตุผล** (`0 < v ≤ SWING_MAX_SPREAD`) ไม่ใช่
   แค่ "มีตั้ง" — กันคนลำดับถัดไปตั้งค่าที่เพิ่งกลายเป็น bug ใหม่
4. **ในระบบ brokerless ให้ไล่รายการเกทที่พึ่ง bridge ทั้งหมดตอน design** แล้วถามทีละตัว:
   bridge ตายแล้วเกทนี้พับไปทางไหน — คำตอบ "skip ทุกอย่าง" รวมกันคือ 0 ไม้ตลอดไป
   (ชั้นที่ 3 ต่อจาก .env leak: RANGING_HARD_BLOCK → SESSION_CONFIDENCE_MULT → spread)

## การันตีความเข้าใจผิดแบบนี้

เขียน plan ไว้ว่า X จะ inert → เขียน causal test ข้อสมมตินั้น **ก่อน deploy** ("None → ผ่าน"
คือข้อสมมติทดสอบได้ใน 10 บรรทัด แต่ไม่มีใครเขียน เพราะคำว่า "ยอมรับได้" ฟังดูเหมือนตัดสินใจแล้ว)