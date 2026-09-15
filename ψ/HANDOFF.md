# HANDOFF — Portfolio-cent P1-P3 paper-parallel

> **ฉบับปัจจุบัน (แทนที่เก่า)** · เขียน 2026-09-16 โดย God Port (Metty role) · ต่องานได้ทันทีจากเอกสารนี้เล่มเดียว
> เอกสารอ้างอิง: proposal `ψ/outbox/proposal_portfolio-cent-20260915T172221Z.md` (Hermes APPROVED 2026-09-16, คำตัดสิน Q1-Q5) · status `ψ/outbox/status_portfolio-deploy-phase1_20260916.md` · runbook `ψ/plans/portfolio-p1p3-paper-deploy.md` · ISSUE-087

---

## 1. สถานะงาน portfolio-cent ตอนนี้

**Implement เสร็จ 100% + deploy ครึ่งทาง (phase 1 เสร็จ)**

| ส่วน | สถานะ | หลักฐาน |
|------|--------|---------|
| Schema (variants, portfolio_events, accounts portfolio columns) | ✅ | commit `a91bd4c` |
| Variant matrix P1-P5 (`scripts/generate_variants.py`) | ✅ | รวมอยู่ใน commit เดียวกัน |
| Entry-hour window gate (4a1b) + freeze/cooldown gate (4a1c) ใน `run_once` | ✅ | causal tests `tests/test_portfolio_gates_live.py` 12 tests |
| Portfolio manager (freeze DD20% / CB 3 แพ้ 24h / high-water / weekly summary) | ✅ | `scripts/portfolio_manager.py` + 19 tests |
| docker-compose: mt5p1-3 + oracle-engine-p1-3 (คนละ container ต่อบช. ตาม Q2) | ✅ | `docker-compose.vps.yml` + 12 guard tests (Real-A protection) |
| Self-contained seeding (engine start = seed + enroll เอง ไม่ต้องมี extra deploy step) | ✅ | `_seed_portfolio_accounts` ใน `scripts/oracle_runner.py` |
| Full test suite | ✅ **873 passed, 1 skipped** | `python3 -m pytest tests/` (ต้องใช้ `python3` เท่านั้น — rtk `python` ไม่มี pytest) |
| Push branch | ✅ | `2026-07-01-live-trader-bugfix` @ `a91bd4c` บน origin |
| VPS pull | ✅ | repo VPS `/opt/god-port-oracle` อยู่ที่ `a91bd4c` (มี fix AEGIS afb6d5d ใน image — จำเป็นเพราะ engine ใหม่ใช้ AEGIS gate) |
| mt5p1/mt5p2/mt5p3 containers | ✅ **Up (healthy)** | bridge socket :8001 ฟังอยู่ครบ 3 ตัว, VNC 5904/5905/5906 ตอบ 401 (พร้อมใช้), ตอนขึ้นครั้งแรก wine init ช้า ~10 นาที = เรื่องปกติ |
| Real-A (`oracle-engine`, `oracle-engine-train`, mt5a-d) | ✅ **ไม่โดนแตะ** | baseline จดไว้ที่ VPS `/tmp/pre-deploy-ps.txt` — `oracle-engine` Up 2 months, `-train` Up 3 weeks, mt5a-d เป๊ะตลอด |
| RAM VPS | ✅ เหลือ ~4.0 GB available หลังเปิด mt5p 3 ตัว | แผนไว้ engines 3 ตัวใช้เพิ่ม ~1.8 GB → พอ |

## 2. จุดหยุดปัจจุบัน — รอคุณหมอเปิดบช. demo 3 ใบ (manual VNC)

**เหตุผลที่ต้อง manual**: MT5 python bridge API มีแต่คำสั่งจัดการ terminal ที่ login แล้ว (orders/positions/account_info) — **ไม่มีฟังก์ชันเปิดบช.ใหม่** ต้องเปิดผ่านหน้าจอ MT5 อย่างเดียว (รายงานวิธีนี้ให้ PM ไปแล้วตาม addition 2)

**ขั้นตอนให้คุณหมอ (ต่อใบ ~2 นาที)**:
1. เปิด **http://100.68.106.101:5904** (P1), **:5905** (P2), **:5906** (P3) — user `mt5user` / pass `mt5password`
2. ใน MT5: **File → Open an Account** → พิมพ์ `Exness` → เลือก **Exness-MT5Trial7** (demo)
3. เลือก **Open a demo account** → กรอกฟอร์ม (email ที่ใช้จริง)
4. ได้ login+password → จดทันที ใส่ใน **`/opt/god-port-oracle/.env`**:
   ```env
   MT5_LOGIN_P1=...  MT5_PASSWORD_P1=...  MT5_SERVER_P1=Exness-MT5Trial7
   MT5_LOGIN_P2=...  MT5_PASSWORD_P2=...  MT5_SERVER_P2=Exness-MT5Trial7
   MT5_LOGIN_P3=...  MT5_PASSWORD_P3=...  MT5_SERVER_P3=Exness-MT5Trial7
   PORTFOLIO_DATA_DIR=/opt/god-port/data
   ```
5. ถ้ามีบช. demo Exness อยู่แล้ว → ข้ามขั้นตอนเปิด เอาแค่ login/password ใส่ .env

**เช็คแล้ว 2026-09-16**: `.env` บน VPS ยังไม่มี key `MT5_LOGIN_P*` (grep -c = 0) — จุดหยุดนี้ยังค้าง

## 3. ขั้นถัดไปหลังคุณหมอใส่ credential — runbook ทำต่อได้เลย

```bash
ssh vpsdeluna          # Host vpsdeluna = 100.68.106.101, user root, key id_ed25519_server
```

```bash
# 1. Build + start engines ใหม่ 3 ตัว (จาก repo /opt/god-port-oracle)
ssh vpsdeluna 'cd /opt/god-port-oracle && \
  docker compose -f docker-compose.vps.yml up -d --build \
  oracle-engine-p1 oracle-engine-p2 oracle-engine-p3'

# 2. Verify bridges 3 ตัวต่อได้ (bridge เป็น raw socket protocol ไม่ใช่ HTTP —
#    curl /health จะไม่ตอบ ให้เทียบ socket กับ mt5a แทน)
ssh vpsdeluna 'docker ps --format "{{.Names}} {{.Status}}" | grep -E "mt5p|engine-p"'

# 3. Verify Real-A ไม่โดนแตะ — เทียบกับ baseline
ssh vpsdeluna 'docker ps --format "{{.Names}} {{.Status}}" | grep -E "oracle-engine |oracle-engine-train|mt5a"'
# ต้องเห็น: oracle-engine "Up 2 months", oracle-engine-train "Up 3 weeks", mt5a "Up 2 months"
# (= /tmp/pre-deploy-ps.txt ที่จดไว้ก่อน deploy)

# 4. ตรวจ seeding ใน DB ของแต่ละใบ (engine start แล้วจะ seed + enroll เอง)
ssh vpsdeluna 'sqlite3 /opt/god-port/data/p1/oracle_p1.db \
  "SELECT name, portfolio_status, variant_id, baseline_balance FROM accounts;"'
# → P1 | running | P1-base-ny | 100.0

# 5. ตั้ง host crontab (durable — CronCreate ของ Claude session ไม่ survive)
ssh vpsdeluna 'crontab -e'
# */30 * * * * cd /opt/god-port-oracle && source .env && PORTFOLIO_ACCOUNTS=P1,P2,P3 \
#   ACCOUNTS=P1,P2,P3 MT5_BRIDGE_P1_HOST=127.0.0.1 MT5_BRIDGE_P1_PORT=5009 \
#   MT5_BRIDGE_P2_HOST=127.0.0.1 MT5_BRIDGE_P2_PORT=5010 \
#   MT5_BRIDGE_P3_HOST=127.0.0.1 MT5_BRIDGE_P3_PORT=5011 \
#   python3 scripts/portfolio_manager.py --db-dir /opt/god-port/data --psi-dir ψ \
#   >> /var/log/portfolio-manager.log 2>&1
```
**Weekly report**: `portfolio_manager.py --weekly` ทำงานเองใน window อาทิตย์ 13:00 UTC = **20:00 BKK** (built-in — ถ้า crontab ทุก 30 นาทีครอบช่วงนั้นอยู่แล้ว ไม่ต้องเพิ่ม cron แยก)

**6. รายงานผลเต็ม** (ตาม PM addition 3): เขียน `ψ/outbox/result_portfolio-*.md` — สถานะบช. / สัญญาณแรกๆ / RAM จริง — ให้ Hermes อ่าน relay ให้คุณหมอ สัญญาณแรกควรมาใน golden hours ถัดไป เช็ค rejection reasons ได้:
```bash
ssh vpsdeluna 'sqlite3 /opt/god-port/data/p1/oracle_p1.db \
  "SELECT reason, COUNT(*) FROM signal_rejections GROUP BY reason ORDER BY 2 DESC LIMIT 10;"'
```

## 4. ตัวเลขพารามิเตอร์ variant (จาก proposal §4) — ตั้งค่าไว้ใน compose/generator แล้ว

| Variant | ATR mult | RR | Min conf | Entry window (BKK) | Entry hours (UTC ใน env) | Risk |
|---|---|---|---|---|---|---|
| **P1** base | 2.0 | 3.0 | 0.50 | 01-03, 13, 15 | `6,8,18,19,20` | 0.5% |
| **P2** tight-SL | 1.8 | 2.5 | 0.50 | 07-08, 13, 15 | `0,1,6,8` | 0.5% |
| **P3** more-entry | 2.2 | 3.0 | 0.45 | 01-03, 07-08 | `0,1,18,19,20` | 1.0% |
| P4 NY-focus (ยังไม่เปิด) | 2.5 | 2.5 | 0.45 | 07-08, 13, 15 | `0,1,6,8` | 1.0% |
| P5 wide-SL (ยังไม่เปิด) | 2.8 | 3.0 | 0.45 | 01-03, 15 | `8,18,19,20` | 0.75% |

**พารามิเตอร์ร่วมทุกใบ**: max_positions=1 · BLOCKED_HOURS `3,4,5,9,14` UTC (= BKK 10-12, 16, 21 negative-EV) · trailing 0.40/0.20 · ML ensemble **OR @0.50** (same as Demo-D, Q3) · DRAWDOWN_ACCOUNT_LIMIT 0.20 · ไม่มี martingale/grid · trade age >1h · CB แพ้ 3 ติด → cooldown 24h · DD ≥20% จาก peak → freeze

**เงื่อนไขขยาย P4/P5 (Q1)**: หลัง P1-P3 มี 48 ชม. healthy (containers Up ต่อเนื่อง + มีสัญญาณเข้าระบบ + ไม่มีบช.ไหนโดน freeze) → propose เปิด P4/P5 (variant defs พร้อมอยู่ใน generator แล้ว — เติมแค่ services ใน compose + .env + data dirs)

## 5. คำเตือนสำคัญ (อ่านก่อนทำต่อ)

1. **ห้าม recreate `oracle-engine` (Real-A) หรือ service เดิมเด็ดขาด** (Q5) — deploy เฉพาะ services ใหม่เสมอ แล้ว verify start-time ด้วย `docker ps` ทุกครั้ง (baseline: `/tmp/pre-deploy-ps.txt`)
2. **ISSUE-003 (deploy fix afb6d5d ขึ้น Real-A) ค้างเป็นการตัดสินใจแยกของคุณหมอ — ห้ามทำเงียบๆ** ถ้าจะทำต้องขออนุมัติก่อน
3. **IRB/IRON LAW**: ML ensemble ห้ามเปิดบน Account A (Real-A) — engine คนละ container กับ P-accounts อยู่แล้ว อย่าไปแตะ
4. **Path บน VPS**: repo = `/opt/god-port-oracle` / data dirs = `/opt/god-port/data/p{1,2,3}` (แยกกันโดยตั้งใจ) — runbook เดิมเขียน `cd /opt/god-port` ผิด **แก้แล้ว 2026-09-16** ใน `ψ/plans/portfolio-p1p3-paper-deploy.md`
5. **ห้ามใส่ credential ใน repo** — MT5_LOGIN_P* ฯลฯ อยู่ใน `.env` บน VPS เท่านั้น
6. Bridge protocol เป็น **raw socket ไม่ใช่ HTTP** — อย่าตกใจที่ `curl :8001/health` ไม่ตอบ (mt5a production ก็ไม่ตอบ) ให้เช็คด้วย socket connect เหมือน healthcheck
7. mt5p containers ครั้งแรกที่ขึ้นใหม่ wine init ~10 นาที ก่อน healthy = ปกติ
8. Cron ของ Claude (CronCreate) **session-only ไม่ survive** — portfolio manager ต้องใช้ host crontab เท่านั้น (บทเรียน 2026-08-24)

## 6. งานค้างอื่นที่แยกจาก portfolio (อย่าปน)

- **ISSUE-003**: AEGIS fix deploy ขึ้น Real-A — รอคุณหมอตัดสินใจ
- **ISSUE-059**: trade-counting work (broky/risk/drawdown_protection.py, trade_blocker.py, backtest scripts) — work stream แยก
- **P4/P5 expansion**: ตามเงื่อนไข 48h ในข้อ 4
- **Stale DB row 3411173137**: reconciliation ค้าง (บช.เก่า)
- **Cent account verify** (proposal §5): paper-parallel นี้ใช้ demo standard — ก่อนขึ้นเงินจริงต้อง verify server name Exness Cent + min lot ที่คุณหมอจะเปิดจริง