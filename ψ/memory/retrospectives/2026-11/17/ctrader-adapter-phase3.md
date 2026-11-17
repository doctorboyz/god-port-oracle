---
date: 2026-11-17
task: Phase 3 — cTrader Open API adapter (additive ล้วน ยังไม่เชื่อมบช.จริง)
origin: คุณหมอสั่งผ่าน Hermes 17/11 (งาน 5 ในคิว repo)
status: done
---

# Retro: cTrader adapter (งาน 5)

## อะไรเปลี่ยน

Package ใหม่ `ctrader/` (6 โมดูล + README + .proto reference) + tests ใหม่ 3 ไฟล์
103 tests — ไฟล์ใหม่ล้วน ไม่แตะไฟล์เดิมแม้บรรทัดเดียว

ผ่าน cross-context review 2 รอบ (reviewer ไร้ context คนแก้):
- รอบ 1 FAIL: CRITICAL (ORDER_ACCEPTED จบการรอ → ไม้ filled รายงาน fail
  ทั้งที่ position เปิด) + HIGH (SWAP/server-event ขโมย pending future)
  + MEDIUM 4 + LOW 5 — reproduce เองด้วย causal test ทั้งคู่ก่อนแก้
  (RED → fix → GREEN) แล้วส่งตรวจรอบ 2
- รอบ 2 **PASS** (0 CRITICAL / 0 HIGH) เหลือ MEDIUM 2 ตัวรากเดียวกัน:
  event ที่ echo clientMsgId ของ request ที่ตายแล้ว (timeout→retry)
  หล่นไป FIFO ขโมย waiter ของ retry — แก้ตามที่ reviewer ระบุ
  (gate เดียวใน `_claim_wait`: echo id ไม่มี waiter = ทิ้ง) ด้วย causal
  test 2 ตัว RED ก่อนแก้ (retry ได้ ticket ไม้ที่ตายแล้ว / ghost error
  ทำ close รายงาน fail ทั้งที่ fill ตามมา) → GREEN
- residual LOW ที่ยอมรับ (documented): anonymous terminal event ไร้
  positionId ยัง match any waiter (liveness trade-off), ORDER_CANCELLED
  ไม่ terminal (timeout-bounded fail-safe), transport drain nit

## ทำยังไง — ศึกษาต้นทางก่อนเขียนโค้ดจริง

- โจทย์บังคับ "ห้ามเชื่อข้อมูลลอยๆ" — ยืนยันทุกอย่างจาก 3 ต้นทางจริง:
  help.ctrader.com (overview / messages / model-messages / common-messages /
  sending-receiving-json), SDK ทางการ spotware/OpenApiPy (ดาวน์โหลด source
  auth.py/client.py/endpoints.py/tcpProtocol.py + ConsoleSample มาอ่านจริง)
  และสุดท้าย **openapi-proto-messages** (.proto ฉบับทางการ MIT) — คัดลอกมา
  เก็บใน `ctrader/proto_reference/` เป็น source of truth ถาวรใน repo
- ได้ข้อเท็จจริงสำคัญที่ไม่มีในโจทย์: **JSON mode มีจริงที่ port 5036**
  (newline-terminated, envelope เดียวกับ protobuf) — เป็นทางออกของข้อจำกัด
  ห้ามแก้ pyproject.toml (ไม่มี protobuf/twisted ใน dep set) = เชื่อมจริง
  ได้ด้วย stdlib ล้วน
- กับดักที่จับได้จาก .proto ก่อนเขียนโค้ด: MARKET order ใส่ SL/TP absolute
  ไม่ได้ (ต้องใช้ relative แบบ 1/100000), order/close ตอบกลับเป็น push
  ProtoOAExecutionEvent ไม่ใช่ Res ธรรมดา, trendbar หน้าต่างจำกัด 1
  สัปดาห์/request, equity ไม่มีใน ProtoOATrader (ต้องคำนวณจาก reconcile+spot)

## หลักฐานจากการรันจริง

- cTrader tests: 103 passed (bridge lifecycle + contract + protocol) รันด้วย
  `.venv/bin/python -m pytest` — ตัวเลขเต็มอยู่ใน handoff
- contract test สะท้อน signature จริงของ MT5Bridge (inspect ทั้งชื่อ param,
  kind, default, iscoroutinefunction) — ถ้า MT5Bridge เปลี่ยน จะแดงทันที
- full suite: ดูตัวเลขจริงใน handoff (รันที่ commit state)
- ruff check ไฟล์ใหม่ทั้งหมด: 0 issues

## บทเรียน

1. **ห้ามแก้ pyproject กลายเป็นข้อจำกัดออกแบบ** — ผลออกมาดีกว่าเดิม:
   JSON mode ทำให้ adapter ไม่เพิ่ม dependency เลย และ protocol.py แยกชัด
   วันหน้าสลับเป็น protobuf ได้โดยแก้ไฟล์เดียว
2. **mock test กับ wire contract จริงไม่ใช่ของเล่น** — ทุก payloadType ID
   ใน FakeTransport มาจาก .proto ไม่ใช่ตัวเลขที่เราคิดเอง มี test ล็อกเลข
   ID ทั้งชุดไว้ (`test_payload_types_match_canonical_proto`)
3. **พึ่ง docs เว็บอย่างเดียวไม่พอ** — หน้าเว็บหาย details หลายจุด
   (proto field จริง, ตัวเลข enum); ต้องลงไปถึง .proto จึงได้ครบ — และ
   ควรเก็บ .proto ไว้ใน repo เสมอเพื่องานต่อ
4. ตัวเลข test flake ไม่เกิด (mock ทั้งหมด ไม่มี timing นอกจาก timeout
   ที่ตั้งชัด) — โครงแบบนี้เหมาะกับ adapter ที่ยังไม่เคยเชื่อมของจริง
5. **รอบ review แรกจับ CRITICAL ที่เรามองไม่เห็นเอง** — คนเขียน
   state machine มัก test เฉพาะ happy path ที่ตนออกแบบไว้ (ACCEPTED→
   FILLED, SWAP กลาง close) — reviewer ที่ไม่มี context คนแก้หา
   sequence จาก protocol จริงแทน · กฎเดิมยืนยันซ้ำ: reproduce เอง
   ก่อนเชื่อ (ทั้ง 2 รอบทำแบบเดียวกัน: RED → fix → GREEN)

## อะไรค้าง

- ยังไม่เคยต่อ network จริง — สิ่งที่ mock ตอบไม่ได้ (ชื่อ symbol ทองจริง
  บน IC, พฤติกรรม clientMsgId ของ execution event จริง, latency) ต้อง
  verify บน demo ตาม checklist ใน `ctrader/README.md`
- การ wiring เข้า engine/compose = งานใหม่ ต้องขออนุมัติแยก (บช. real
  6194964 ยังห้ามแตะ)
- cTrader tokens มีวันหมดอายุ — วันเชื่อมจริงอาจต้องทำ refresh flow
  (ProtoOARefreshTokenReq / apps/token grant_type=refresh_token) ซึ่งยัง
  ไม่ได้ implement (ทำมือครั้งเดียวพอสำหรับ demo day)
