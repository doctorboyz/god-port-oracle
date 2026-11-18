---
date: 2026-11-18
task: Wire paper-fetcher → cTrader Open API (FEED_SOURCE=ctrader) — ยุค IC Markets
origin: คุณหมอสั่งผ่าน Hermes 18/11 (invest scope, Telegram)
status: done — รอ token จาก Spotware (app Submitted) แล้วเปิดฟาร์มตาม runbook
---

# Retro: paper-fetcher → cTrader wiring

## อะไรเปลี่ยน

- `scripts/paper_feed_fetcher.py`: source ที่สาม `ctrader` — fetch M5/H1/H4
  ผ่าน `CTraderBridge` ที่มีอยู่ (chunk ≤1 สัปดาห์/request แบ่งเองแล้ว) ไม่
  เขียน protocol ซ้ำ · stateless per-fetch: `asyncio.run()` ต่อ fetch +
  fresh bridge ต่อ attempt (ไม่มี shared-terminal disease รอบนี้)
- **Read-only บังคับด้วยโครงสร้าง**: fetcher ไม่ถือ CTraderBridge ตรงๆ —
  ถือ `_ReadOnlyCTraderView` (dataclass façade) ที่ expose แค่
  connect/disconnect/get_candles/last_connect_error — ไม่มี attribute path
  ไปหา send_order/close_position เลย + tests ล็อก construction site
  เดียว + wire allowlist
- **Fail-loud ทุกทาง**: token ตาย (CH_ACCESS_TOKEN_EXPIRED/INVALID/
  APP_AUTH_FAILURE/CLIENT_AUTH_FAILURE) → log ERROR ชี้ชัด + `_auth_dead`
  freeze (zero wire traffic จน restart) · **ห้าม yfinance fallback —
  มี test พิสูจน์เป็นกฎ** · credential ขาด = ERROR ตั้งแต่ก่อนต่อ network
- **Era discipline**: marker 2 ชั้น — per-TF `XAUUSD_<TF>.csv.era` +
  dir-level `feed-era.json` (`{era:"ic-ctrader", since, venue:"IC Markets"}`)
  · seed ข้าม era = แทนที่ประวัติเดิม + ประทับใหม่ · delta ข้าม era =
  refuse พร้อมชื่อ era ใน log (ไม่ผสม Exness กับ IC เงียบๆ) · marker
  per-TF หาย = กู้จาก feed-era.json ได้
- `ctrader/bridge.py` additive เดียว: `last_connect_error` (reset ต่อ
  connect ใหม่, ตั้งใน except) — เพื่อให้ fetcher fail ด้วยเหตุผลจริง
  ไม่ retry token ตายแบบตาบอด
- compose paper-farm: บล็อก env ของ **paper-fetcher จุดเดียว** —
  FEED_SOURCE=ctrader + CTRADER_* 7 ตัวผ่าน `${...}` (runtime values)
  · สาม engine ไม่ถูกแตะแม้บรรทัด (diff ยืนยัน hunk เดียว)
- tests ใหม่ `tests/test_paper_feed_ctrader_causal.py` **32 ตัว**:
  unit (scaling/token-freeze/credential ขาด/view blocks orders/flaky
  poll recovery/cache) + era discipline 9 ตัว + failure modes + backfill
  + **mock end-to-end ครบ** (bars → CSV → marker → delta merge →
  forming bar หาย → clock advance) + read-only contract 5 ชั้น (text
  scan/payload absence/construction site/AST surface/AST delegation/
  module regex) + compose contract (both-ends grep — compose เขียน key
  แล้ว fetcher ต้องอ่านจริง)

## ทำยังไง — สอง session แบ่งงานบน worktree เดียว

- งานนี้มีสอง agent บน same machine same worktree: peer (god-port-oracle-45)
  เป็นเจ้าของ impl fetcher, เราเป็นเจ้าของ tests + compose + README + bridge
  additive + full-suite run · Edit ชนกันครั้งแรก (file-modified-since-read)
  → เลือก **coordinate ผ่าน SendMessage แทน overwrite** — ตกลง split ก่อน
  แตะต่อ แล้วทำงานคนละไฟล์ไม่ชนกันอีกเลย
- เราจับ defect จริง 2 ตัวของ impl ตัวแรกก่อน merge: (1) CTRADER_* env
  เขียนใน compose แต่ reader เดียวอยู่ใน `ctrader/config.py` ซึ่ง **อยู่
  นอก corpus ของ env-coverage guard** (PRODUCTION_DIRS = metty/broky/
  shared/scripts) = guard ต้องแดงจริง เรา verify แดงแล้ว peer แก้ด้วย
  `_CTRADER_ENV_KEYS` literal ใน fetcher (2) era marker ไม่รู้จัก = fail-open
  → ตกลงกันเป็น fail-loud (venue-mix guard)
- **asyncio.Queue ผูก loop แรกที่ถูกใช้** — FakeTransport ตัวเดียวรับ
  `asyncio.run()` หลายรอบไม่ได้ ("bound to a different event loop") ·
  อ่าน `TcpNewlineTransport` จริงก่อนสรุป: production สร้าง transport
  ใหม่ต่อ fetch (transport=None) = production ไม่มี bug นี้ · แก้ฝั่ง
  harness ด้วย `SessionFakeTransport` (queue ใหม่ทุก connect = model
  session จริง) — บทเรียน: e2e แดงอาจเป็นของ harness ไม่ใช่ impl
  อ่าน production path ก่อนตัดสิน
- core_integrity manifest แดง: คราวนี้ไม่ใช่ bridge.py แต่ **compose**
  (IRON-scope เพราะ build farm image) — ตาม procedure เก่า: ตรวจ diff
  ด้วยตา (hunk เดียว paper-fetcher env) → `--update` → manifest เปลี่ยน
  hash compose บรรทัดเดียวพอดี
- เจตนาเดิมของเราคือ thread-persistent feed; peer เสนอ stateless
  (per-fetch asyncio.run + fresh bridge) — เลือกแบบ peer เพราะไม่มี
  shared-terminal disease และ test ง่ายกว่า โดยรักษา invariant ทุกตัว
 ของ spec ไว้ครบ

## หลักฐานจากการรันจริง

- ไฟล์ใหม่ 32 ตัว: **32 passed** (collect-only ยืนยัน 32)
- ชุดเป้าหมายรวม (ไฟล์ใหม่ + test_paper_feed_fetcher.py + 3 ไฟล์
  ctrader เดิม + test_env_coverage_guard.py): **157 passed / 0 failed**
- env-coverage guard: แดงก่อนแก้ (reader นอก corpus) → เขียวหลัง peer
  ใส่ literal names — พิสูจน์ guard ทำงานจริงไม่ใช่ของประดับ
- core_integrity manifest: `--update` หลังตรวจตา → diff manifest =
  hash compose บรรทัดเดียว
- full suite ทั้ง repo: **1788 passed 2 skipped 0 failed 979.75s
  EXIT=0** — คณิตเป๊ะ: baseline 1756 + ใหม่ 32 · ยืนยันข้ามวิธีอีก
  รอบ: รัน chunk 6 ชุดตามตัวอักษร (643+44+146+310+255+390) ได้
  1788/2 เท่ารันเดียวต่อเนื่อง

## บทเรียน

1. **สอง agent หนึ่ง worktree: ประกาศ ownership ก่อนแตะไฟล์ร่วม** —
   Edit ชนครั้งแรกเพราะไม่มีใครรู้ว่าอีกฝ่ายกำลังเขียนไฟล์เดียวกัน ·
   SendMessage split งานแล้วจบงานได้ไม่มีความชนกันอีก — วันหน้าเจอ
   multi-session บน worktree เดียว ให้ ping กันด้วย ListAgents ก่อน
   แตะไฟล์หลักของงาน
2. **env guard มองเฉพาะ corpus ของมัน** — key ที่ reader อยู่นอก
   PRODUCTION_DIRS = no-op guard · ทางออกคือให้ production reader
   เองอ้างชื่อ key literal (ไม่ใช่ย้าย corpus)
3. **mock harness ต้อง model สิ่งที่ production สร้างใหม่ต่อ session** —
   transport/queue จริงสร้างต่อ connect ถ้า test แช้ object ข้าม
   asyncio.run มันจะตายด้วยเหตุที่ production ไม่มี แล้วเราจะไปแก้
   impl ที่ถูกต้องแล้ว
4. **fail-loud ต้องมีเหตุผลจริงในมือ** — `last_connect_error` additive
   บน bridge ทำให้ fetcher แยก "token ตาย = หยุดถาวรรอคน" ออกจาก
   "network เพี้ยน = retry" — ถ้าไม่มีมัน retry loop จะเบิ้ล request
   ใส่ token ตายเปล่าๆ
5. **`ps` ใน sandbox นี้มองไม่เห็น process ของ background task ของ
   harness** — log ไม่โต + ps ว่าง ≠ process ตาย · รอบนี้เกือบสรุป
   "ถูก SIGKILL" แล้วรันซ้ำซ้อน ทั้งที่ suite จริงวิ่งจบเอง 16:19 นาที
   พอดี · วันหน้า: รอ notification ของ background task ก่อนวินิจฉัย
   แล้วถ้าต้องยืนยันจริง ใช้การรัน chunk แยกเป็นหลักฐานข้ามวิธี

## อะไรค้าง

- **token ยังไม่มี** — app สถานะ Submitted รอ Spotware · เมื่อถึง:
  เปิดฟาร์มตาม runbook ใน `ctrader/README.md` (เติม host .env บน Mac
  mini → HOST_TYPE=demo → build paper-fetcher ถ้า image เก่า →
  docker start 4 containers → ตรวจ log + feed-era.json)
- **compose เป็นไฟล์ generated** — บล็อก CTRADER_* เป็น hand-edit:
  **อย่ารัน `paper_farm_apply.py` จนกว่าจะ port บล็อกเข้า
  `_fetcher_service()` + farm_variants.yml เสร็จ** ไม่งั้น regenerate
  แล้วบล็อกหายเงียบๆ (งานถัดไป นอก scope วันนี้)
- **farm_watchdog `_feed_calendar()`**: era=="bridge"→Exness ปฏิทิน,
  อื่นๆ→CME — era "ctrader" จะตก branch CME และตีราคา stale ผิด
  วันที่ bar IC จริงมาถึง · ต้องใส่ IC entry เมื่อมีหลักฐาน bar จริง
  ให้ตีราคา (open item ของ peer — ไม่ใช่ blocker)
- token อายุ ~30 วัน ยังไม่มี refresh flow (ทำมือได้ตาม README)
