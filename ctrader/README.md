# ctrader/ — cTrader Open API adapter (Phase 3, additive)

บริดจ์ cTrader ที่มีพื้นผิวเหมือน `MT5Bridge` — เพื่อให้อนาคตสลับ broker
ได้โดยไม่แตะ engine core สถานะปัจจุบัน: **โค้ด + tests ครบ, ยังไม่เคยต่อ
network จริง, บช. real 6194964 ยังไม่ถูกแตะแม้บรรทัดเดียว**

- Interface parity: `tests/test_ctrader_contract.py` (สะท้อน signature จริง
  ของ `metty/bridge/client.py` — read-only)
- Unit/lifecycle tests: `tests/test_ctrader_bridge.py` (mock transport ล้วน)
- Wire protocol: `tests/test_ctrader_protocol.py`
- Wire contract ต้นทาง: `ctrader/proto_reference/*.proto` (spotware, MIT)

## โครงสร้าง

| ไฟล์ | หน้าที่ |
|---|---|
| `bridge.py` | `CTraderBridge` — auth 2 ชั้น, spot, order lifecycle, heartbeat, พื้นผิว parity |
| `protocol.py` | JSON envelope, newline framing, กฎ scaling (ราคา 1/100000, volume cents, moneyDigits) |
| `transport.py` | `CTraderTransport` (interface) + `TcpNewlineTransport` (asyncio TLS จริง — ยังไม่ถูกใช้นอก tests) |
| `messages.py` | payloadType IDs + enums จาก .proto ทางการ |
| `config.py` | `CTraderConfig.from_env()` — credentials ผ่าน env เท่านั้น |
| `endpoints.py` | hosts/ports (demo/live, JSON 5036 / protobuf 5035) + OAuth URIs |

## เลือก JSON mode ทำไม (สำคัญต่อการอนุมัติ)

cTrader Open API มี 2 serialization: protobuf (port 5035) และ JSON
(port 5036, newline-terminated, TCP หรือ WebSocket) — **envelope เดียวกัน
(clientMsgId / payloadType / payload)** ตาม
[docs ทางการ](https://help.ctrader.com/open-api/sending-receiving-json/)

repo นี้ `pyproject.toml` ตรึง dependency set ไว้และไม่มี protobuf/twisted —
งานนี้ห้ามแก้ไฟล์เดิม จึงเลือก JSON mode: เชื่อมจริงได้ด้วย stdlib
อย่างเดียว (`json` + `asyncio` + `ssl`) ผ่าน `TcpNewlineTransport`

ถ้าวันหนึ่งต้องการ protobuf mode (เข้า SDK ทางการ `ctrader-open-api`)
ย้ายเฉพาะ `protocol.py` (encode/decode) — bridge ไม่ต้องแก้

## กลไกที่ต้องรู้ก่อนเชื่อมจริง (ยืนยันจาก .proto ทางการแล้ว)

1. **Auth 2 ชั้น**: `ProtoOAApplicationAuthReq` (client id/secret ของ app)
   → `ProtoOAAccountAuthReq` (accessToken + ctidTraderAccountId ต่อบช.)
   ก่อนคำสั่งอื่นใด
2. **Heartbeat**: client ที่ไม่ส่งอะไรเกิน ~30 วิ โดนตัด — bridge ส่ง
   `ProtoHeartbeatEvent` เมื่อ idle ≥ 20s (ตามพฤติกรรม SDK ทางการ)
   และ echo ทันทีเมื่อ server ส่ง heartbeat มา
3. **MARKET order ใส่ SL/TP แบบ absolute ไม่ได้** — bridge แปลงเป็น
   `relativeStopLoss`/`relativeTakeProfit` (1/100000) ยึดจาก quote ปัจจุบัน
   (BUY ยึด ask / SELL ยึด bid) · แก้ SL/TP ไม้ที่เปิดแล้วใช้
   `amend_position_sltp()` (absolute ได้)
   - **การตัดสินใจที่ควรรู้ (ข้อแลกเปลี่ยน)**: server ตีความ relative
     distance จากราคา fill จริง ดังนั้นตลาดไว SL จริงจะเลื่อนตาม
     slippage (เช่น BUY ask 2350.30 fill 2350.80 → SL ที่ขอ 2345.0
     กลายเป็น ~2345.5) · ทางเลือกคือ amend-after-fill ตี absolute เป๊ะ
     แต่ถ้า amend พังจะเหลือไม้ **ไร้ SL** — เราเลือก attach ตั้งแต่
     ต้น (ไม้มี SL เสมอ) แลกกับ drift ตาม slippage · ถ้าคุณหมอต้องการ
     absolute เป๊ะ ให้สั่งเพิ่มวัน wiring
4. **Order/close ตอบกลับเป็น push event** `ProtoOAExecutionEvent`
   (ไม่ใช่ Res ธรรมดา) — bridge จับคู่ด้วย clientMsgId + FIFO ที่
   **กรองเฉพาะ event แบบ terminal ที่เป็นของ request นั้นจริงๆ**:
   ACCEPTED/PARTIAL_FILL ไม่จบการรอ (รอ fill ต่อ), SWAP/deposit/
   server-event (stop-out) ไม่มีสิทธิ์ขโมย request, event ของ position
   อื่นไม่ถูก claim (ตรวจด้วย positionId), และ event ที่ echo
   clientMsgId ของ request ที่ตายแล้ว (timeout/retry) โดนทิ้ง —
   ไม่หล่นไปขโมย waiter ของ retry
5. **Volume หน่วย cents**: lots × lotSize × 100 (ทอง 1 lot = 100 oz =
   10000) — lotSize ดึงจาก `ProtoOASymbolByIdReq` ตอน connect
6. **Trendbar window จำกัด 1 สัปดาห์/request** + historical rate limit
   5 req/s — bridge แบ่ง chunk เอง (test พิสูจน์)
7. **Rate limit ปกติ 50 req/s/connection**
8. **Equity ไม่มีใน `ProtoOATrader`** — bridge คำนวณ = balance + Σ
   unrealized PnL จาก reconcile + spot (ประมาณใน quote currency — สำหรับ
   XAUUSD ที่ deposit USD ตรงเป๊ะ; ถ้า deposit สกุลอื่น ต้องเพิ่ม conversion
   ภายหลัง)

## สิ่งที่คุณหมอต้องเตรียมเพื่อเชื่อมจริง (checklist)

- [ ] สร้าง OAuth application ที่ [openapi.ctrader.com](https://openapi.ctrader.com)
      (login ด้วย cTID) → ได้ **Client ID + Client Secret** (app ต้องผ่าน
      อนุมัติจาก Spotware ก่อน — ใช้เวลาหลักวัน)
- [ ] ตั้ง redirect URI ของ app (ใช้ได้ทั้ง localhost สำหรับ flow ครั้งเดียว)
- [ ] ขอ **access token** (scope=trading) ต่อบช.:
      เปิด `https://openapi.ctrader.com/apps/auth?client_id=<ID>&redirect_uri=<URI>&scope=trading`
      → ใส่ credential cTID → ได้ `?code=` → แลกเป็น token ที่
      `https://openapi.ctrader.com/apps/token` (grant_type=authorization_code)
      (ทำมือครั้งเดียวได้ — ตัวช่วย `list_accounts_by_token()` อยู่ใน bridge แล้ว)
- [ ] เปิดบช. **demo** ของ IC Markets cTrader (แยกจาก real 6194964)
- [ ] เก็บ credential ใน **VPS .env เท่านั้น** (env-only pattern เหมือน MT5):
      `CTRADER_CLIENT_ID`, `CTRADER_CLIENT_SECRET`, `CTRADER_ACCESS_TOKEN`,
      `CTRADER_ACCOUNT_ID`, `CTRADER_HOST_TYPE=demo`, `CTRADER_NAME`
- [ ] **ระวัง token อายุ** (~30 วัน): adapter ยังไม่มี refresh flow —
      วันที่ token หมด `connect()` จะ error CH_ACCESS_TOKEN_EXPIRED ·
      refresh มือได้ผ่าน `https://openapi.ctrader.com/apps/token`
      (grant_type=refresh_token) หรือขอ flow อัตโนมัติเป็นงานถัดไป

## แผนเชื่อมแบบ demo ก่อน real (บังคับ)

1. **Demo account ก่อนเสมอ** — `CTRADER_HOST_TYPE=demo` + ctidTraderAccountId
   ของบช. demo · รัน `connect()` + `health_check()` + `get_account_info()` +
   `get_candles()` ด้วยตัวเอง (ยังไม่ปล่อยให้ engine ใช้)
2. ทดสอบ order ด้วย **volume ต่ำสุด** (0.01 lot) บน demo — ส่ง → fill →
   `get_positions` → `close_position` → เทียบเลขกับหน้า cTrader จริง
3. ตรวจสอบสิ่งที่ mock ตอบไม่ได้ บน demo อย่างน้อย: ราคา spot 1/100000
   ถูกไหม, symbol ทองบน IC ชื่อจริงว่าอะไร (เช็คด้วย symbols list — adapter
   resolve แบบ case-insensitive อยู่แล้ว), execution event กลับมามี
   clientMsgId หรือไม่ (เรารองรับทั้งสองแบบ), เวลา latency จริง
4. เมื่อ demo ผ่านการทดสอบครบ + **คุณหมออนุมัติเป็นลายเขียน** → ค่อยเตรียม
   บช. real 6194964 (ยอดยัง $0.00 — ฝากเงินหลังอนุมัติเท่านั้น) ·
   `CTRADER_HOST_TYPE=live` บนบช. แยก
5. ขั้นถัดไปหลังอนุมัติ: งาน wiring เข้า engine (compose + engine config)
   เป็นงานใหม่อีงาน — ต้องผ่านสายตาคุณหมอแยกต่างหาก งานนี้ไม่แตะ

## ที่มา (verified 17/11/2026)

- Canonical protobuf: <https://github.com/spotware/openapi-proto-messages> (MIT)
- Python SDK ทางการ: <https://github.com/spotware/OpenApiPy>
- Docs: <https://help.ctrader.com/open-api/> (essential tutorials, messages,
  sending-receiving JSON, protocol-buffers-json)
- บันทึกบช.: `~/.hermes/invest/notes/2026-11-17_icmarkets-ctrader-account.md`
