# HANDOFF — Portfolio-cent P1-P3 paper-parallel

> **ฉบับปัจจุบัน (แทนที่เก่า)** · เขียน 2026-09-18 โดย God Port (Metty role) · ต่องานได้ทันทีจากเอกสารนี้เล่มเดียว
> เอกสารอ้างอิง: proposal `ψ/outbox/proposal_portfolio-cent-20260915T172221Z.md` (Hermes APPROVED 2026-09-16, คำตัดสิน Q1-Q5) · status `ψ/outbox/status_portfolio-deploy-phase1_20260916.md` · runbook `ψ/plans/portfolio-p1p3-paper-deploy.md` · ISSUE-087

---

## ⚠️ สถานะปัจจุบัน (2026-09-18): **หยุดทั้งหมดตามคำสั่งคุณหมอ (ตัวเลือก a)**

คุณหมอสั่งหยุดงาน P1-P3 ทั้งหมด ผ่าน Hermes — **engine-p1..3 + mt5p1..3 ถูก `docker stop` ไปแล้วทั้ง 6 ตัว** ตั้งแต่ 2026-09-18 ~14:50 UTC ไม่มีอะไรรันอยู่เบื้องหลัง กลับมาทำต่อ = start containers ตาม §3

**เหตุผลที่หยุด**: แก้ login ไม่สำเร็จเพราะ MT5 terminal build ใหม่ (6198) ตายบน wine (ดู §2) + คุณหมอไม่ต้องการให้ไปต่อในตอนนี้ ทั้งนี้แม้จะไม่หยุดก็ไม่มีความเสี่ยงเงินจริง (DRY_RUN_PORTFOLIO=1 + bridge ต่อไม่ได้ = ส่ง order ไม่ได้อยู่แล้ว)

**Real-A ปลอดภัย**: `oracle-engine` + `mt5a` ยัง "Up 2 months" เป๊ะ แต่พบ `oracle-engine-train` **"Up About an hour"** ซึ่งไม่ตรง baseline เดิม (ควรจะ Up 3 weeks+) — session นี้ไม่ได้แตะ container นี้ แค่ engine-p1 (ที่หยุดเอง) — **คุณหมอควรตรวจว่า train restart ได้เช่นไร** (อาจ crash + auto-restart ด้วย `restart: unless-stopped`)

## 1. งานที่เสร็จแล้ว (สรุปใหม่)

| ส่วน | สถานะ | หลักฐาน |
|------|--------|---------|
| Schema + variant matrix + gates + portfolio manager + compose services | ✅ | commit `a91bd4c` (ดูฉบับก่อน) |
| **Credential บช.จริง 3 ใบใน .env แล้ว** (Exness-MT5Real25 — ไม่ใช่ demo!) | ✅ 2026-09-18 | logins `184149109` (P1) / `184149113` (P2) / `184149114` (P3) อยู่ใน `/opt/god-port-oracle/.env` เท่านั้น — **ห้ามใส่ repo** |
| **Fix engine crash-loop "Unknown account: Pn"** | ✅ deployed + pushed | commit `67ad176` — SignalGroup enum + manager routing + scalp ห้าม fallback บช. A (causal tests `tests/test_portfolio_signal_group.py` RED→GREEN 5 tests) |
| mt5p1-3 containers (โปรแกรมรันได้, bridge socket :8001 ฟังครบ) | ✅ แต่ **หยุดอยู่** | ต้อง start ใหม่เมื่อทำต่อ |
| engine-p1-3 containers | ✅ แต่ **หยุดอยู่** | p1 หยุดก่อนแล้ว (เพื่อ debug bridge), p2/p3 หยุดพร้อมกัน 2026-09-18 |
| DRY_RUN ทุกใบ | ✅ `DRY_RUN_PORTFOLIO=1` default | compose ใช้ `DRY_RUN=${DRY_RUN_PORTFOLIO:-1}` — บล็อกส่ง order จริงเสมอจนกว่าคุณหมอจะเปลี่ยน |

**คำสั่ง verify เมื่อกลับมาทำต่อ**: `docker ps -a --format "{{.Names}} {{.Status}}" | grep -E "mt5p|engine-p"` → ต้องเห็น Exited ทั้ง 6 ตัว

## 2. ตัว blocker ตัวสุดท้าย — MT5 build 6198 ตายบน wine (วิจัยเสร็จแล้ว ยังไม่ได้แก้)

**อาการ**: bridge/`mt5.initialize()` คืน `False, (-10005, 'IPC timeout')` ตลอด — terminal GUI รันปกติแต่ Python API ต่อเข้าไม่ได้เลย แม้ standalone wine python ก็ timeout และ CLI `/login: /password: /server:` ผ่านทาง `MT5_CMD_OPTIONS` แล้วก็ **ไม่ auto-login** บน build นี้

**สาเหตุ (พิสูจน์แล้ว)**: ไม่ใช่ package (5.0.36 = 5.0.37 ล่าสุด pypi ก็ timeout เหมือนกัน) ไม่ใช่ login state แต่เป็น **terminal build**: `mt5a` (Real-A, build **5830**) ใช้ API ได้ปกติมา 2 เดือน ส่วน volume ของ mt5p โดน **LiveUpdate เป็น build 6198** ตั้งแต่ 15 ก.ย. และ build 6198 บน Wine 10 ไม่ตอบ IPC ของ MetaTrader5 package (terminal log mt5p1: LiveUpdate 6182→6198, ไม่มี Network line เลยตั้งแต่ติดตั้ง ไม่เคย login สำเร็จ)

**สิ่งที่พิสูจน์เพิ่มระหว่างหา**: (a) xdotool ส่ง keyboard ได้จริง (ปิด login dialog ด้วย Escape ได้ แต่เท่านั้น — ไม่จำเป็นต้องใช้ GUI แล้ว), (b) bridge มี `exposed_initialize/login/account_info` พร้อมใช้ถ้า initialize ผ่าน, (c) ห้ามพยายาม call initialize ผ่าน bridge ตอน terminal แข็ง — **GIL จะ block bridge ทั้ง ThreadedServer** (`result expired`)

**แผนแก้ต่อไปนี้ (ยังไม่ได้ลงมือ)**:
1. `docker cp` terminal **build 5830 จาก mt5a** ออกมา (`/config/.wine/drive_c/Program Files/MetaTrader 5` ~417MB) → แทนที่ใน mt5p1..3 (การกระทำ mt5a = read-only ปลอดภัย 100%)
2. ลบ data dir `D0E8209F77C8CF37AD8BF550E51FF075` ของ p ทิ้งให้เริ่มสะอาด (ยังไม่มี account cached จะเสียแค่เวลา init)
3. **บล็อก LiveUpdate** ทุก container mt5p (`chmod 555` ที่ LiveUpdate folder หรือลบ dir + read-only) — ไม่บล็อกแล้วมันจะ update กลับเป็น 6198 ในไม่กี่นาที
4. Restart terminal (`docker restart mt5p1`) → เช็ค log มี `Network ... authorized on Exness-MT5Real25` หรือเรียก bridge login ผ่าน `exposed_login` (บน build 5830 initialize น่าจะผ่านเหมือน mt5a)
5. สำเร็จแล้วค่อยเริ่ม engine-p1..3 + crontab (§3)

**ขั้นทดสอบก่อน production**: ทำกับ mt5p1 ใบเดียวก่อนจน bridge `account_info()` ได้ `login=` แล้วค่อย duplicate ไป p2/p3

## 3. Runbook กลับมาทำต่อ (หลังแก้ §2 หรือถ้าคุณหมอเปลี่ยนใจสั่ง start ก่อน)

```bash
ssh vpsdeluna          # Host = 100.68.106.101, user root, key id_ed25519_server
```

```bash
# 1. Start containers ทั้ง 6 ตัว
ssh vpsdeluna 'cd /opt/god-port-oracle && docker compose -f docker-compose.vps.yml start \
  mt5p1 mt5p2 mt5p3 oracle-engine-p1 oracle-engine-p2 oracle-engine-p3'
# ใช้ start (ไม่ใช่ up --build) เพื่อไม่ recreate image ที่ยังไม่จำเป็น

# 2. Verify bridges (raw socket ไม่ใช่ HTTP — curl /health ไม่ตอบ = ปกติ)
ssh vpsdeluna 'docker ps --format "{{.Names}} {{.Status}}" | grep -E "mt5p|engine-p"'

# 3. Verify Real-A ไม่โดนแตะ — เทียบ baseline
ssh vpsdeluna 'docker ps --format "{{.Names}} {{.Status}}" | grep -E "oracle-engine |oracle-engine-train|mt5a"'
# ต้องเห็น: oracle-engine "Up 2 months", mt5a "Up 2 months"
# ⚠️ oracle-engine-train ตอน 2026-09-18 เป็น "Up About an hour" — ตรวจด้วยว่า restart จากไหน

# 4. เช็ค login + seeding
ssh vpsdeluna 'docker exec oracle-engine-p1 python /tmp/bridge_check.py 1'  # ต้องเห็น login=
ssh vpsdeluna 'sqlite3 /opt/god-port/data/p1/oracle_p1.db \
  "SELECT name, portfolio_status, variant_id, baseline_balance FROM accounts;"'

# 5. ตั้ง host crontab (ยังไม่ได้ตั้ง — Claude CronCreate ไม่ survive session)
ssh vpsdeluna 'crontab -e'
# */30 * * * * cd /opt/god-port-oracle && source .env && PORTFOLIO_ACCOUNTS=P1,P2,P3 \
#   ACCOUNTS=P1,P2,P3 MT5_BRIDGE_P1_HOST=127.0.0.1 MT5_BRIDGE_P1_PORT=5009 \
#   MT5_BRIDGE_P2_HOST=127.0.0.1 MT5_BRIDGE_P2_PORT=5010 \
#   MT5_BRIDGE_P3_HOST=127.0.0.1 MT5_BRIDGE_P3_PORT=5011 \
#   python3 scripts/portfolio_manager.py --db-dir /opt/god-port/data --psi-dir ψ \
#   >> /var/log/portfolio-manager.log 2>&1

# 6. เขียนรายงานผล ψ/outbox/result_portfolio-*.md ให้ Hermes (ตาม PM addition 3) — ยังไม่ได้เขียน
```

## 4. ⚠️ ความเสี่ยงใหม่ที่ต้องเล่าให้คุณหมอรู้ — **LiveUpdate 6182 บน mt5a (Real-A)**

สิ่งที่พบระหว่างงาน: terminal ของ `mt5a` (build 5830) มี log **`LiveUpdate: new version build 6182 ... downloaded successfully`** เมื่อ 2026-09-18 11:21 UTC

**แปลว่าอะไร**: build 6182 อยู่ในระบบแล้ว (ถูกโหลดไว้แล้ว) — ถ้า container mt5a restart ตอนไหน terminal จะ **apply 6182 อัตโนมัติ** และ build 6182+ บน wine มีความเสี่ยงโดนปัญหา IPC เดียวกับ 6198 ที่ทำ mt5p ตาย → Real-A engine อาจเทรดไม่ได้ (แพงกว่าเดิม — บช.จริง 100k บาท)

**ทางแก้ที่แนะนำ**: บล็อก LiveUpdate ของ mt5a ด้วยการ make LiveUpdate dir read-only — **ต้อง restart container mt5a เพื่อให้มีผล จึงต้องขออนุมัติคุณหมอก่อน** เพราะ restart mt5a = กระทบ Real-A ชั่วคราว (engine จะ retry เองได้) แต่ต้องเลือกจังหวะที่ไม่มี position เปิดอยู่ — ปัจจุบัน B: XAUUSD BUY 0.01 (open 13:39 UTC, -4.53), D: BUY 0.01 (open 13:38, -3.21) ตามที่คุณหมอเช็ค

**ระหว่างนี้**: อย่า restart mt5a โดยไม่ตั้งใจ และเช็ค log ของ mt5a ก่อนทุกครั้งที่จะ deploy

## 5. คำถามค้างที่ต้องตอบเมื่อกลับมาทำต่อ

1. **Standard หรือ Cent**: บช. 3 ใบเป็น real (Exness-MT5Real25) แต่ยังไม่รู้ว่า Standard หรือ Cent (ยังไม่เคยดึง `account_info` สำเร็จเพราะ §2) — ต้องดู currency (USCent = cent) เพื่อตั้ง `ACCOUNT_TYPE_P1..P3=real|cent_real` ใน .env ให้ถูก (ตอนนี้ default `demo` ทำให้ display name เป็น "Demo-P1" ทั้งที่เป็นบช.จริง)
2. **DRY_RUN_PORTFOLIO=0**: รอคุณหมอตัดสินใจหลัง verify ว่า engine รันสมบูรณ์ + รู้ประเภทบช.แน่ชัด

## 6. ตัวเลขพารามิเตอร์ variant (จาก proposal §4) — ตั้งค่าไว้ใน compose/generator แล้ว

| Variant | ATR mult | RR | Min conf | Entry window (BKK) | Entry hours (UTC ใน env) | Risk |
|---|---|---|---|---|---|---|
| **P1** base | 2.0 | 3.0 | 0.50 | 01-03, 13, 15 | `6,8,18,19,20` | 0.5% |
| **P2** tight-SL | 1.8 | 2.5 | 0.50 | 07-08, 13, 15 | `0,1,6,8` | 0.5% |
| **P3** more-entry | 2.2 | 3.0 | 0.45 | 01-03, 07-08 | `0,1,18,19,20` | 1.0% |
| P4 NY-focus (ยังไม่เปิด) | 2.5 | 2.5 | 0.45 | 07-08, 13, 15 | `0,1,6,8` | 1.0% |
| P5 wide-SL (ยังไม่เปิด) | 2.8 | 3.0 | 0.45 | 01-03, 15 | `8,18,19,20` | 0.75% |

**พารามิเตอร์ร่วมทุกใบ**: max_positions=1 · BLOCKED_HOURS `3,4,5,9,14` UTC · trailing 0.40/0.20 · ML ensemble OR @0.50 · DRAWDOWN_ACCOUNT_LIMIT 0.20 · ไม่มี martingale/grid · trade age >1h · CB แพ้ 3 ติด → cooldown 24h · DD ≥20% จาก peak → freeze

**เงื่อนไขขยาย P4/P5 (Q1)**: หลัง P1-P3 มี 48 ชม. healthy → propose เปิด P4/P5

## 7. คำเตือนสำคัญ (อ่านก่อนทำต่อ)

1. **ห้าม recreate `oracle-engine` (Real-A) หรือ service เดิมเด็ดขาด** (Q5) — deploy เฉพาะ services ใหม่เสมอ แล้ว verify start-time ด้วย `docker ps` ทุกครั้ง (baseline: `/tmp/pre-deploy-ps.txt`)
2. **ISSUE-003 (deploy fix afb6d5d ขึ้น Real-A) ค้างเป็นการตัดสินใจแยกของคุณหมอ — ห้ามทำเงียบๆ**
3. **IRB/IRON LAW**: ML ensemble ห้ามเปิดบน Account A (Real-A)
4. **ห้ามใส่ credential ใน repo** — MT5_LOGIN_P*/MT5_PASSWORD_P* อยู่ใน `.env` บน VPS เท่านั้น และห้าม echo ออก terminal ให้โผล่ transcript — ใช้ wrapper `source /opt/god-port-oracle/.env` + ส่งเป็น env/args เท่านั้น
5. Bridge protocol เป็น **raw socket ไม่ใช่ HTTP** — ตรวจด้วย socket connect เหมือน healthcheck
6. **อย่า call initialize ผ่าน bridge ตอน terminal ในสถานะแปลก** — GIL block ทั้ง bridge (ได้ `result expired` จนต้อง stop engine ปลด) ทดสอบ initialize ด้วย standalone wine python + `timeout` guard ก่อนเสมอ
7. mt5p containers ครั้งแรกที่ขึ้นใหม่ wine init ~10 นาที ก่อน healthy = ปกติ
8. Cron ของ Claude (CronCreate) **session-only ไม่ survive** — portfolio manager ต้องใช้ host crontab เท่านั้น (บทเรียน 2026-08-24)
9. บช.ทั้ง 3 ใบเป็น **บช.เงินจริง** (Exness-MT5Real25) ไม่ใช่ demo ตาม plan เดิม — ทุก step ต้องคิดก่อนแตะเสมอ และ DRY_RUN ต้องเป็น 1 จนกว่าคุณหมอจะอนุมัติเอง

## 8. งานค้างอื่นที่แยกจาก portfolio (อย่าปน)

- **LiveUpdate 6182 บน mt5a** (§4) — งานใหม่ ต้องขออนุมัติ restart mt5a ก่อนแก้
- **oracle-engine-train restart ปริศนา** (§ ⚠️ ด้านบน) — ตรวจสาเหตุ
- **ISSUE-003**: AEGIS fix deploy ขึ้น Real-A — รอคุณหมอตัดสินใจ
- **ISSUE-059**: trade-counting work — work stream แยก
- **P4/P5 expansion**: ตามเงื่อนไข 48h ใน §6
- **Stale DB row 3411173137**: reconciliation ค้าง (บช.เก่า)
- **Cent account verify**: ตอนนี้ใช้บช.จริงแล้ว — ต้อง verify Standard vs Cent ผ่าน `account_info.currency` (§5)