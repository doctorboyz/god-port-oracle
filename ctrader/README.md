# ctrader/ — cTrader Open API adapter (Phase 3, additive)

บริดจ์ cTrader ที่มีพื้นผิวเหมือน `MT5Bridge` — เพื่อให้อนาคตสลับ broker
ได้โดยไม่แตะ engine core สถานะปัจจุบัน (20/11): **โค้ด + tests ครบ,
paper-feed wired (FEED_SOURCE=ctrader, 18/11), แตะ network จริงแล้ว 19/11
— OAuth app ผ่านอนุมัติ, token แลกสำเร็จ, app auth + account list ผ่านจริง
ทั้ง demo/live host (พบบช. real ctid 49596566 / demo ctid 49609239),
พบและแก้ framing bug + volume scale bug แล้ว (ดูส่วน framing กับ volume
ด้านล่าง) · งาน engine wiring (P22 บน demo IC1/IC2, 20/11) **หยุดที่
parity gap — รายงาน `ψ/outbox/ctrader_engine_wiring_blocked_2026-11-20.md`**:
LiveTrader ปฏิเสธชื่อบช. IC1/IC2 ตั้งแต่ constructor + ไม่มีจุด inject bridge
ใน oracle_runner + engine เรียก sync wrappers 7 ตัว (`*_sync`) และ
`get_deal_history` ที่ bridge ยังไม่มี — รอ Hermes เลือกทางก่อน (contract test
งาน 5 ล็อกแค่ 9 async methods จึงไม่จับกลุ่ม sync) · lotSize fallback 3 จุด
เปลี่ยนเป็น fail-loud แล้ว (20/11) · บช. real 6194964 ยังไม่ถูกแตะแม้บรรทัดเดียว**

- Interface parity: `tests/test_ctrader_contract.py` (สะท้อน signature จริง
  ของ `metty/bridge/client.py` — read-only)
- Unit/lifecycle tests: `tests/test_ctrader_bridge.py` (mock transport ล้วน)
- Wire protocol: `tests/test_ctrader_protocol.py`
- Feed wiring tests: `tests/test_paper_feed_ctrader_causal.py` (era
  discipline + read-only contract + mock end-to-end)
- Wire contract ต้นทาง: `ctrader/proto_reference/*.proto` (spotware, MIT)

## โครงสร้าง

| ไฟล์ | หน้าที่ |
|---|---|
| `bridge.py` | `CTraderBridge` — auth 2 ชั้น, spot, order lifecycle, heartbeat, พื้นผิว parity |
| `protocol.py` | JSON envelope, object-concatenation framing, กฎ scaling (ราคา 1/100000, volume cents, moneyDigits) |
| `transport.py` | `CTraderTransport` (interface) + `TcpNewlineTransport` (asyncio TLS จริง — ยังไม่ถูกใช้นอก tests) |
| `messages.py` | payloadType IDs + enums จาก .proto ทางการ |
| `config.py` | `CTraderConfig.from_env()` — credentials ผ่าน env เท่านั้น |
| `endpoints.py` | hosts/ports (demo/live, JSON 5036 / protobuf 5035) + OAuth URIs |

## เลือก JSON mode ทำไม (สำคัญต่อการอนุมัติ)

cTrader Open API มี 2 serialization: protobuf (port 5035) และ JSON
(port 5036, TCP หรือ WebSocket) — **envelope เดียวกัน
(clientMsgId / payloadType / payload)** ตาม
[docs ทางการ](https://help.ctrader.com/open-api/sending-receiving-json/)

## Framing จริงบน wire (สำคัญ — บทเรียน connection day 19/11)

[docs ทางการ](https://help.ctrader.com/open-api/sending-receiving-json/)
ระบุแค่ serialization JSON **ไม่ได้ระบุ framing เลย** สมมติฐานเดิม
"newline-terminated frames" (จาก WebSocket mode ที่ WS มีเฟรมของตัวเอง)
**ผิดบน TCP เปล่า**: probe จริง 19/11 (raw socket ทั้ง demo/live
`ctraderapi.com:5036`) พิสูจน์ว่า server เขียน **JSON objects เรียงติดกัน
ไม่มีตัวคั่น ไม่มี newline ปิดท้าย ไม่มี heartbeat ระหว่างนั้น** — ทุกเฟรม
คือ object เปล่าๆ เช่น 2101 ตอบกลับมา 44 bytes ครบพอดีที่ `}` แล้วเงียบ

`FrameDecoder` (protocol.py) จึงสกัดเฟรมแบบ incremental ด้วย
`json.JSONDecoder().raw_decode` — object ยังไม่ครบ = รอ feed ถัดไป
(ไม่ raise), ครบ = สกัค byte-exact, whitespace/newline ระหว่าง objects
ข้ามได้ (เฟรมฝั่งเราที่ newline-terminated ยังใช้ได้), garbage ที่ไม่มีทาง
กลายเป็น JSON = `ProtocolError` (fail loud ไม่ hang) — tests
`tests/test_ctrader_framing_causal.py` replay byte จริงจาก wire

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
5. **Volume หน่วย cents — `volume_cents = lots × lotSize` เท่านั้น**:
   `lotSize` จาก `ProtoOASymbolByIdReq` มาเป็น "cents" อยู่แล้ว (ตาม
   .proto: `lotSize = 30; // Lot size of the Symbol (in cents)`) —
   XAUUSD จริงจาก wire = **10000** (1 lot = 100 oz = 10000 cents,
   0.01 lot = 100 cents) · **ห้ามคูณ 100 ซ้ำอีกชั้น** (bug เดิมทำ 1 lot
   กลายเป็น 1,000,000 cents = oversize 100 เท่า — คลาสเดียวกับ USC trap;
   default เก่า 100 ถูกโดยบังเอิญจน mock-test เขียวหลอก) — tests
   `tests/test_ctrader_volume_scale_causal.py` · **20/11: fallback 100.0
   เหลือศูนย์จุด** — ทั้ง 3 จุดที่เคยเดา (`_load_lot_size` สองทาง +
   `_lot_size` บน order path) กลายเป็น `CTraderApiError` fail-loud
   (`LOT_SIZE_MISSING` / `LOT_SIZE_NOT_FOUND` / `LOT_SIZE_NOT_LOADED`)
   — lotSize หาย = connect abort ที่ boot / order ปฏิเสธก่อนออกจากมือ
   ไม่มีทางเดา scale เงียบๆ — tests
   `tests/test_ctrader_lot_size_fail_loud_causal.py`
6. **Trendbar window จำกัด 1 สัปดาห์/request** + historical rate limit
   5 req/s — bridge แบ่ง chunk เอง (test พิสูจน์)
7. **Rate limit ปกติ 50 req/s/connection**
8. **Equity ไม่มีใน `ProtoOATrader`** — bridge คำนวณ = balance + Σ
   unrealized PnL จาก reconcile + spot (ประมาณใน quote currency — สำหรับ
   XAUUSD ที่ deposit USD ตรงเป๊ะ; ถ้า deposit สกุลอื่น ต้องเพิ่ม conversion
   ภายหลัง)

## สิ่งที่คุณหมอต้องเตรียมเพื่อเชื่อมจริง (checklist)

- [x] สร้าง OAuth application ที่ [openapi.ctrader.com](https://openapi.ctrader.com)
      (login ด้วย cTID) → ได้ **Client ID + Client Secret**
      **สถานะ 19/11: Spotware อนุมัติแล้ว ("application is active now")**
- [ ] ตั้ง redirect URI ของ app (ใช้ได้ทั้ง localhost สำหรับ flow ครั้งเดียว)
- [x] ขอ **access token** (scope=trading) ต่อบช.:
      เปิด `https://openapi.ctrader.com/apps/auth?client_id=<ID>&redirect_uri=<URI>&scope=trading`
      → ใส่ credential cTID → ได้ `?code=` → แลกเป็น token ที่
      `https://openapi.ctrader.com/apps/token` (grant_type=authorization_code)
      **สถานะ 19/11: แลก token จริงสำเร็จ (access+refresh ใน VPS .env)** —
      account list ผ่าน raw probe เจอบช.ใต้ cTID: real ctid **49596566**
      (traderLogin 6194964) + demo ctid **49609239** (traderLogin 10138643)
- [ ] เปิดบช. **demo** ของ IC Markets cTrader (แยกจาก real 6194964)
- [x] เก็บ credential ใน **VPS .env เท่านั้น** (env-only pattern เหมือน MT5):
      `CTRADER_CLIENT_ID`, `CTRADER_CLIENT_SECRET`, `CTRADER_ACCESS_TOKEN`,
      `CTRADER_ACCOUNT_ID`, `CTRADER_HOST_TYPE=demo`, `CTRADER_NAME` —
      ฟาร์ม Mac mini อ่านจาก host .env ที่ repo root ของ Mac mini (compose
      `${...}` interpolation), VPS .env เก็บ Client ID/Secret แล้ว
- [ ] **ระวัง token อายุ** (~30 วัน): adapter ยังไม่มี refresh flow —
      วันที่ token หมด `connect()` จะ error CH_ACCESS_TOKEN_EXPIRED ·
      refresh มือได้ผ่าน `https://openapi.ctrader.com/apps/token`
      (grant_type=refresh_token) หรือขอ flow อัตโนมัติเป็นงานถัดไป

## เปิดฟาร์มกระดาษเมื่อ token มาถึง (runbook, 18/11)

ฟาร์มจอดอยู่บน Mac mini (`docker start` คืนได้ทั้ง 4 containers:
paper-fetcher + oracle-engine-farm1/farm2/farmbet) — feed ฝั่ง cTrader
wired แล้ว (`FEED_SOURCE=ctrader` ใน compose, mock-test ครบ), เหลือแค่
credential จริง:

1. เติม `CTRADER_*` (CLIENT_ID / CLIENT_SECRET / ACCESS_TOKEN /
   ACCOUNT_ID) ใน **host .env ที่ repo root ของ Mac mini** — บล็อก env
   ของ service paper-fetcher ใน docker-compose.paper-farm.yml ดึงค่าผ่าน
   `${...}` ทั้งหมด (runtime values ไม่ใช่ strategy knobs)
2. **`CTRADER_HOST_TYPE=demo` ก่อนเสมอ** — default ใน compose เป็น demo
   อยู่แล้ว อย่า override เป็น live จนกว่า demo ผ่าน + คุณหมออนุมัติเป็น
   ลายเขียน
3. ถ้า image เก่า (จอดมาก่อนงานนี้): `docker compose -f
   docker-compose.paper-farm.yml build paper-fetcher` ก่อน เพื่อเอาโค้ด
   18/11 (package `ctrader/` + fetcher รุ่น ctrader) เข้า image · แล้ว
   `docker start paper-fetcher oracle-engine-farm1 oracle-engine-farm2
   oracle-engine-farmbet`
4. ตรวจจาก artifact จริง:
   - `docker logs paper-fetcher` ต้องเห็น "era switch … → ctrader" และ
     "wrote N bars" ต่อ cycle · token ผิด/หมดอายุ = log ERROR ชี้ชัด
     (เช่น CH_ACCESS_TOKEN_EXPIRED) และ **ไม่มี yfinance fallback เด็ดขาด**
   - `data/paper-feed/feed-era.json` ต้องเป็น
     `{era: "ic-ctrader", since: …, venue: "IC Markets"}` · ถ้ามี marker
     era อื่นค้างอยู่ fetcher จะ refuse ทั้ง seed ทั้ง merge
     (venue-mix guard) — จะสลับ era จริงๆ ให้ลบ marker แล้ว seed
   - engines เทรดไม่ได้เองเมื่อ feed ตาย (`MAX_CANDLE_AGE_SECONDS=1800`
     stale guard) — นั่นคือกลไกป้องกัน ไม่ใช่ bug
5. **ข้อระวังเดียวของงานนี้**: `docker-compose.paper-farm.yml` เป็นไฟล์
   **generated** จาก `scripts/paper_farm_apply.py` — บล็อก CTRADER_* ของ
   paper-fetcher เป็น hand-edit ตาม scope งาน wiring 18/11 · **อย่ารัน
   `paper_farm_apply.py` จนกว่าจะเพิ่ม ctrader branch ใน
   `_fetcher_service()` เสร็จ** ไม่งั้น regenerate แล้ว CTRADER_* block
   หายเงียบๆ (งานถัดไป — อยู่นอก scope วันนี้)

## Engine wiring stage 2 — P22 บน cTrader demo (20/11)

**สถานะ: code เสร็จ + test ครบ + DRY_RUN — ยังไม่ deploy (Hermes เป็นคน
deploy ตาม runbook ข้างล่าง)**

สอง engine วิ่ง P22 champion ผ่าน CTraderBridge แล้ว (ไม่ใช่ MT5):

- `oracle-engine-ic1` — ctid 49633503, baseline **$2,200 USD** face value
- `oracle-engine-ic2` — ctid 49633450, baseline **$500 USD** face value
- ทั้งคู่ `DRY_RUN=1` hard-code ใน `docker-compose.ctrader-demo.yml`
  (simulated trades ก่อน — Hermes เป็นคนเสนอ flip เมื่อเห็นไม้แรกตรงสูตร)

กลไกเลือก bridge (จุดเดียวใน runner): `ACCOUNT_TYPE_ICn=ctrader` →
`_build_ctrader_bridge_factory()` สร้าง `CTraderEngineAdapter` (cache ต่อ
process) → ส่งเข้า `LiveTrader(bridge_factory=...)` — constructor เอา factory
ไปแทน `metty.bridge.client.MT5Bridge` ที่ 9 จุด self-serve ทั้งหมด ไม่แก้
call-site แม้แต่จุดเดียว · บช. อื่น (A/P1-P3) ไม่ส่ง factory = เดินทาง MT5
เหมือนเดิมเป๊ะ (test ล็อกไว้) · account อื่นปลอมเข้ามา = `ACCOUNT_MISMATCH`
refuse + หนึ่ง engine ต่อ process

**Persistent connection (กฎ hot-path)**: sync wrappers ทั้ง 6 ใช้
connection เดียวบน persistent loop (daemon thread) — handshake เดียว
ต่อหลาย cycle (พิสูจน์ใน test: 2 cycles เต็ม + async order flow =
`APPLICATION_AUTH_REQ` นับ 1) · `adapter.disconnect()` เป็น no-op โดย
เจตนา (engine เรียกตาม idiom MT5 แต่ cTrader auth แพง — ห้ามฉีก)

**Sizing บน IC1/IC2** (สูตร `lot = equity×0.005÷(SL×100)` + floor
0.01-granularity):

| บช. | equity | SL | raw | ส่งจริง |
|-----|--------|----|-----|---------|
| IC1 | $2,200 | $6 | 0.0183 | **0.01** (floor) |
| IC1 | $2,200 | $4 | 0.0275 | **0.02** (เหนือ floor — บช.ควบคุม) |
| IC2 | $500 | $6 | 0.0042 | **0.01** (floor ≈ 1.2%/ไม้ — flag ให้คุณหมอทราบ) |

**Runbook deploy IC1 (บังคับตามลำดับ)**:

1. `docker compose -f docker-compose.ctrader-demo.yml build` **ก่อนเสมอ**
   (up โดยไม่ build = image เก่า = equity_ts ไม่เดิน = มองเหมือน outage)
2. ขึ้น VPS env ก่อน: `CTRADER_CLIENT_ID` / `CTRADER_CLIENT_SECRET` /
   `CTRADER_ACCESS_TOKEN` (host `.env` — repo ไม่เก็บ) · ไม่ตั้งค่าเหล่านี้ =
   container ตายตั้งแต่ boot พร้อมเหตุผล (fail-loud by design)
3. `docker compose -f docker-compose.ctrader-demo.yml up -d oracle-engine-ic1`
   (IC2 รอ IC1 ผ่าน 1 cycle ก่อน)
4. ตรวจจาก artifact จริง:
   - log ต้องเห็น symbol resolve (XAUUSD) + account auth (ctid 49633503)
   - `equity_ts` เดินทุก cycle (300s) และ `current_equity` มาจาก bridge
     จริง (~$2,200) — **ห้ามเป็นค่า paper fallback** (test ล็อก path นี้)
   - reason census สะอาด (skip เพราะเหตุผลจริง เช่น ไม่มีสัญญาณ/สเกล —
     ไม่ใช่ bridge fail ทั้ง cycle)
   - ไม้แรก (simulated): lot ตรงสูตรในตารางข้างบน → เสนอ flip DRY_RUN
5. ข้อระวัง: `_check_existing_position` เรียก rpyc ตรง (จุดที่ 11 นอก
   scope) — บช. IC จะ warn ต่อ cycle แล้วตกไป DB (ปลอดภัย แต่ log มี noise
   — งานถัดไปถ้าจะเก็บ)

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
