# process env สะอาด ≠ runtime config สะอาด — .env ใน image โดน load_dotenv เติมกลับ

**วันที่**: 2026-10-02
**บริบท**: paper farm บน Mac mini — 0 trades ใน 38 ชม. ทั้งที่แก้ compose แล้ว (Hermes จับ layer 2)

## อาการ

แก้ compose เอา `RANGING_HARD_BLOCK` ออกแล้ว redeploy — `docker exec env` สะอาด แต่ engine
ยังไม่มีไม้ การันตีด้วยการ replicate ทางเดิน startup จริงใน container:

```bash
docker exec <c> python3 -c "
from dotenv import load_dotenv; load_dotenv()
import broky.signals.generator as g
print(g.RANGING_HARD_BLOCK)"   # → True ทั้งที่ compose ไม่ได้ตั้ง!
```

## ต้นตอ

- Dockerfile `COPY . .` ปั๊ม `.env` ของ repo (สมัย Real-A, 70 keys + MT5 credentials) เข้า `/app/.env`
  ของทุก image
- entrypoint (`oracle_runner`) เรียก `load_dotenv()` ก่อน import — `load_dotenv` ค่า default
  (`override=False`) **เติม key ที่ยังไม่มีใน process env** จาก `/app/.env`
- ผล: compose ไม่ตั้งอะไร = .env กำหนดเอง เงียบๆ — และ `docker exec env` ดูไม่เจอเพราะดู
  process ใหม่ที่ไม่ได้ผ่าน load_dotenv

## กฎที่ใช้ต่อ

1. **ตรวจ runtime config ด้วยทางเดินเดียวกับ app จริง** (load_dotenv → import → อ่าน flag) —
   อย่าหลงเชื่อ `docker exec env` หรือ compose file อย่างเดียว (ความผิดพลาดเดียวกับ ISSUE-089
   "verify must read live object values")
2. **Image ที่ build จาก repo ที่มี .env = .env หลุดเข้า image เสมอ** — `COPY . .` ไม่เว้น .env
   ถ้าไม่มี .dockerignore
3. **การกันแบบสองชั้น**: strip `.env` ตอน container start (`sh -c "rm -f /app/.env && exec ..."`)
   **และ** pin ค่าสำคัญเป็น explicit `=0/=1` ใน compose — process env ชนะ .env เสมอ
4. `.dockerignore` ทั้ง repo อาจกระทบ VPS ที่พึ่ง /app/.env ใน image — แก้ farm-scoped ก่อน
   แล้วค่อยตรวจ VPS แยก (อย่าแตะ VPS โดยไม่ตรวจ)
5. **Gate ที่คูณกันต้องทดสอบด้วยจุดตัดจริง**: TRENDING_HARD_BLOCK + RANGING_HARD_BLOCK
   ปิดทุก regime พร้อมกัน = ไม่มีหน้าต่างเทรดเลย — อ่านเป็นเรื่องๆ ทีละ gate ไม่เห็น
   causal test ระดับ compose คือตัวจับเรื่องนี้ได้ (`tests/test_farm_env_bcd_contract_causal.py`)