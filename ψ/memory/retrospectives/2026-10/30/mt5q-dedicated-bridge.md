# Retro 2026-10-30 — mt5q: Bridge ราคาแยกต่างหาก จบ Race ฟาร์ม↔mt5b

**Task**: ada-task-20261030-mt5q.md (T1-T4) — คำสั่งคุณหมอผ่าน Hermes:
แก้ race บน bridge ร่วม (fetcher ฟาร์ม vs mt5b) โดยเปิด mt5q เฉพาะราคา login บช. demo B

## สรุปสถาปัตย์ใหม่

**mt5q = ราคาอย่างเดียว** (farm fetcher, login บช. demo B, host port 5012) · **mt5b = engine B อย่างเดียว** (คืนสภาพเดิม ไม่แตะอะไรเลย) — โจทย์นี้พาสถาปัตย์ MT5 บน VPS ไปเป็น 9 terminals (a/b/c/d/p1-3 ทำงาน + q ราคา)

## ปัญหาจริงที่แก้ (หลักฐาน log จริง วันที่ 30/10)

fetcher ฟาร์ม (Mac mini) กับ demo engine B (VPS) ใช้ terminal mt5b ร่วมกันตั้งแต่ realtime-feed เช้าวันเดียวกัน (10:00 UTC) — engine ใช้ pattern connect→copy→**shutdown** ต่อไซเคิล ทำให้ terminal ตายเป็นระยะ ฝั่ง fetcher ที่ถือ persistent connection ตาบอดเป็นช่วง:

| ฝั่ง | อาการ | ตัวเลขก่อนแก้ (จาก log จริง) |
|---|---|---|
| fetcher | `bridge returned no data — re-initializing` | 12 ครั้ง/3 ชม. (10:17→10:33 ติดกันทุกนาที) · bar age ทะลุเกณฑ์ 6 นาที (sample 409s→626s) · ช่วง 10:55-12:40 ไม่มีแท่งใหม่เป็นพักๆ |
| engine B/C/D | `stream has been closed / position check failed` | **144 ครั้ง/12 ชม. ต่อบช. — เท่ากันเป๊ะทั้ง B/C/D** (1 ครั้ง/ไซเคิล) — ดูข้อสำคัญด้านล่าง |

**การันตีใหม่ของการวินิจฉัย (สำคัญ — โจทย์ตีความผิดส่วนหนึ่ง):** stream-closed ของ engine **เป็น baseline ของ engine เอง ไม่ใช่ผลจาก fetcher** — พิสูจน์ด้วย control group: C/D (mt5c/mt5d) ที่ fetcher ไม่เคยแตะก็เจอเท่ากันเป๊ะ และ Demo-B ยังเจอต่อหลัง fetcher ย้ายออก (12:52) — สาเหตุคือ m5_scalp_trader ใช้ connection ที่ swing path ปิดไปแล้วในไซเคิลเดียวกัน → fall back to DB ไม่เสียหาย · ดังนั้น race จริงที่วัดได้ = **ฝั่งฟาร์มเท่านั้น** (ฟาร์มตาบอด) ส่วน engine B ไม่มีหลักฐานว่าโดนทำร้ายจาก fetcher (candle-data error 3 ครั้ง/12 ชม. ทั้งเครื่อง ไม่มีตัวไหนตรง fetcher era) · ตัวเลข "323/8 ชม." ในโจทย์คือผลรวม baseline ทั้ง 3 บช. ไม่ใช่ความเสียหายจาก race

การแก้ยังถูกที่จุดเดิม: ฟาร์มต้องกลับมาสดตลอด และต่อไปนี้ไม่มีใครแย่ง terminal กับใคร

## สิ่งที่เปลี่ยน

| ที่ | อะไร |
|---|---|
| `docker-compose.vps.yml` (repo + patch บน VPS) | +service `mt5q`: image mt5b เดิม · login demo B env (`MT5_LOGIN_B/PASSWORD_B/SERVER_B`) + **`/password:` ใน MT5_CMD_OPTIONS แบบ mt5p1-3** (volume ใหม่ยังไม่มี credential cache) · host **5012**→8001 · VNC 5907 · volume `mt5-config-q` · restart/healthcheck ตาม Dockerfile เดิม |
| `farm_variants.yml` feed | `bridge_port: "5006"→"5012"` + คอมเมนต์ |
| `docker-compose.paper-farm.yml` | regenerate — diff บรรทัดเดียว `PAPER_BRIDGE_PORT=5012` |
| fetcher restart | `docker compose up -d paper-fetcher` เฉพาะตัว (ไม่แตะ engines 16 ตัว) |

**ห้ามแตะครบ**: bridge server code (image ร่วม Real-A) · oracle-engine-train code · mt5b/c/d config (backup ก่อน patch: `docker-compose.vps.yml.bak-mt5q-20261030`) · Real-A/mt5a · engine-p/mt5p · farm engines · fetcher ยัง read-only 4 methods เดิม

## เลือกพอร์ต 5012 (โจทย์เสนอ 5010 — ใช้ไม่ได้จริง)

สแกน VPS จริง: **5005-5011 ถูกใช้ครบ** (mt5a/b/c/d/p1/p2/p3) — "เช่น 5010" ในโจทย์คือ mt5p2 ที่รันอยู่จริง · ใช้ 5012 + VNC 5907

## ซะก่อนได้งาน: บั๊กสามชั้นที่เจอตอนเปิด mt5q (ไม่มีทางเดาจากโจทย์)

1. **MT5 build ใหม่ (โหลดสด 30/10) พังใต้ Wine** — terminal โผล่แต่ dialog แดง *"A debugger has been found running in your system"* บล็อกทั้งเครื่อง ไม่ login · นึกถึง build: ตัวที่ image โหลดมาใหม่ = 122,386,344 bytes vs ที่ mt5b/p1 ใช้อยู่จริง = 121,825,288 (build ตุลา) · **แก้: หยุด mt5q → ย้ายโฟลเดอร์ `Program Files/MetaTrader 5` จาก volume mt5b (โหมด read-only) มาทับ** → boot ผ่านฉลุย ไม่แตะ image/entrypoint ที่แชร์กับ Real-A
2. **Flag /login+/password ไม่ auto-login บน fresh profile ของ build 6230** — terminal ขึ้น wizard "Select a company to open an account with" บังทุกครั้ง และ journal บอก `Accounts deleted due security reason` (credential ที่ติดมากับ program dir โดนล้าง) — ต่างจาก mt5p1-3 ที่ flag ทำงาน (สร้างกันยาวันที่) · **แก้: login มือครั้งเดียว** — `xdotool` (apt install runtime-only ใน container) ขับ UI ผ่าน X11: Alt+F → `l` (Login to Trade Account) → พิมพ์ login/Tab/password/Tab/server → Enter · ติ๊ก Save password ไว้ → **พิสูจน์แล้วว่า pkill terminal → watchdog restart → login กลับมาเองจาก cache (0 วินาที)** — restart ครั้งต่อไปไม่ต้องมือ
3. **VNC web (KasmVNC) รับ keyboard แต่ไม่รับ mouse จาก Playwright** — คลิกเมนูไม่เข้าทั้ง trusted CDP events และ synthetic PointerEvent · xdotool ผ่าน `docker exec` คือทางเลือกที่เชื่อถือได้ (key + mouse ที่ X11 level)

## เกณฑ์ T3 — ผลวัดจริง (window 60 นาทีเต็ม 13:00→14:00 UTC, หลังสลับ 12:51)

| เกณฑ์ (จาก log จริง) | ก่อนแก้ (ยุค mt5b 10:00-12:51) | window 13:00→14:00 | ผล |
|---|---|---|---|
| fetcher `re-initializing` | 12 ครั้ง/3 ชม. + แท่งไม่ไหลเป็นพักๆ (10:55→12:40 ขาดหายเป็นช่วง) | **0 ครั้ง** | ✅ |
| แท่งเขียน CSV หลังปิด | — (heal วน) | **13/13 แท่ง · 41-50 วินาทีหลังแท่งปิด · ไม่มีแท่งหาย** (ข้อมูลเก่าสุด ~5 น. 50 วิ < เกณฑ์ 6 น.) | ✅ |
| mt5b connection จาก Mac mini (100.121.113.38) | มี (fetcher พอร์ตเดียวกับ engine) | **0** — สุ่ม 4 ครั้ง + sweep 90 วิ: ไคลเอนต์เดียวของ mt5b คือ engine (172.27.0.10) · mt5q ถือ connection เดียวจาก 100.121.113.38 | ✅ |
| engine stream-closed | baseline B=C=D เท่ากันเป๊ะ (144/12 ชม./บช. = 1/ไซเคิล) | **B=12 = C=12 = D=12** — เท่ากันเป๊ะอีกตาม baseline ของ engine เอง | ➖ ไม่ใช่เกณฑ์ race (ดูการันตี) |

หมายเหตุ: heal เดียวนอก window (12:53:39) เกิดจาก **pkill ทดสอบ cached-login ของเราเอง** กลับมาเองใน 0.3 วิ — ยืนยันว่ากลไก heal ยังทำงานเมื่อ terminal ตายจริง

## สรุปผลสุดท้าย

- **Race จบครบ**: fetcher 0 heal ใน 60 นาที · แท่งไหลต่อเนื่อง 41-50 วิ · mt5b เหลือ engine เจ้าเดียว
- **T1 ผ่าน**: mt5q Up healthy (10:41 UTC) + login บช. demo B 463363160 สำเร็จ + อ่านแท่งจาก Mac mini ผ่าน `vpsdeluna:5012` ได้จริง
- **T2 ผ่าน**: fetcher log เห็น `source=bridge bridge=vpsdeluna:5012` + connect สำเร็จ + backfill 15,033 แท่งต่อเนื่องจากของเดิม (ไม่ mix era)
- **T3 ผ่านตามเกณฑ์ที่ถูกต้อง** (ข้อ engine stream-closed รายงานตามความจริงเป็น baseline ไม่ใช่ race — ดูการันตีด้านบน)
- **Resilience พิสูจน์**: pkill terminal → watchdog restart → **login กลับมาเองจาก cache** (Save password) — restart ครั้งต่อไปไม่ต้องมือ

## บทเรียน

- **ทรัพยากร process-global (MT5 terminal) ให้เจ้าของเดียว** — ถ้า lifecycle เป็นของคนละเจ้า อีกฝั่งที่ถือ persistent handle จะตายเงียบๆ — แก้ที่ topology (แยก container) ไม่ใช่ tune จังหวะ heal
- **อย่าสรุปสาเหตุจาก log ของตัวเดียว ต้องมี control group** — "323 stream-closed/8 ชม." ฟังดูเหมือน fetcher ทำ แต่พอนับแยก B/C/D เห็นเท่ากันเป๊ะทั้งที่ C/D ไม่มี fetcher — baseline confound · กติกา: ก่อน attribute ความเสียหายให้ actor ไหน ต้องเทียบกับตัวที่ actor นั้นไม่แตะ
- **MT5 fresh-install บน image เก่า = เสี่ยง build ใหม่ไม่รองรับ** — installer ดึง build ล่าสุดเสมอ แต่ Wine ของ image ตกข่าว · ทางออกที่ไม่แตะของส่วนรวม: transplant program files จาก volume ตัวที่รันอยู่จริง (backup ธรรมชาติของระบบเราเอง) · บทเรียนถัดไป: ถ้าต้องสร้าง terminal ใหม่อีก ควรย้าย program dir จากตัวที่รันอยู่ตั้งแต่แรก ก่อนโดน installer ดาวน์โหลด build ใหม่
- **auto-login ผ่าน flag ไม่ใช่สัญญา — พิสูจน์ cache แทน** — ที่ยืนยันได้คือ login มือครั้งเดียว + Save password + kill/restart test (เห็นกลับมาเองจริง) — สถานะ "จะ login ต่อหลัง restart ได้เอง" ต้องมาจากการพิสูจน์ restart ไม่ใช่จาก flag ใน compose
- **docker logs ตายพร้อม container recreate** — หลักฐาน "ก่อนแก้" ต้อง grep เก็บก่อน restart container ที่เก็บ log (ครั้งนี้โชคดีเก็บไว้ระหว่างทาง + engine log อยู่อีก container จึงรอด)
- **หน้าจอ terminal = evidence ประเภทหนึ่ง** — "login ไม่ขึ้น" ประกอบได้หลายสาเหตุ (dialog/flag/profile) ดูผ่าน VNC + vision แล้วเข้าใจใน 1 รอบ ประหยัดกว่าเดาเป็นชั่วโมง

## ค้าง/ติดตามต่อ

- [ ] **compose drift บน VPS**: `/opt/god-port-oracle/docker-compose.vps.yml` ค้าง matrix mr-bet เก่า (container จริงรัน winner ถูกต้อง ตรวจด้วย docker inspect) — ครั้งหน้าที่ deploy ด้วย compose VPS ต้อง sync ก่อน อย่า up -d ทั้งไฟล์ก่อน sync (กัน recreate oracle-engine-train ด้วย env เก่า) · backup ที่ VPS: `docker-compose.vps.yml.bak-mt5q-20261030`
- [ ] **baseline stream-closed ของ engine train** (144/บช./12 ชม. = 1/ไซเคิล): งานสะอาดของตัวเอง — m5_scalp_trader ใช้ connection ตายจากไซเคิลเดียวกัน (fall back to DB ได้จึงไม่เจ็บ แต่ warning รบกวน) — ตัดสินใจแยกว่าจะแก้หรือเลิกสน
- [ ] **xdotool ใน mt5q เป็น runtime-only** — หายเมื่อ container recreate (สงสัยไม่เป็นไร — login อยู่ใน cache แล้ว ไม่ต้องใช้อีก)
- [ ] หลัง DST 1/11/26: probe ปฏิทิน Exness (ค้างเดิม) · ไม้แรก era bridge: เทียบ entry_price paper↔demo (ค้างเดิม)
- [ ] mt5q ไม่มี engine ข้างใน — ห้ามชี้ engine ใดเข้า 5012 (มี order methods ใน bridge เหมือนเดิม)