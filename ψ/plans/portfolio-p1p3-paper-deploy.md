# Portfolio paper-parallel P1-P3 — VPS deploy plan

Hermes approved 2026-09-16. Implementation done on branch
`2026-07-01-live-trader-bugfix` (full suite 867 passed).
This doc = runbook สำหรับ deploy รอบ paper (3 บช. demo)

## กฎเหล็กของ deploy นี้ (Q5)

- `docker compose up -d` **เฉพาะ services ใหม่**: `mt5p1 mt5p2 mt5p3 oracle-engine-p1 oracle-engine-p2 oracle-engine-p3`
- **ห้าม recreate oracle-engine (Real-A) หรือ service เดิมเด็ดขาด**
- หลัง deploy ต้อง `docker ps` verify ว่า start-time ของ `oracle-engine` / `oracle-engine-train` / `mt5a-d` **ไม่เปลี่ยน**
- ISSUE-003 (deploy fix afb6d5d ขึ้น Real-A) = การตัดสินใจแยกของคุณหมอ — ห้ามทำเงียบๆ

## บช. demo 3 ใบเปิดอย่างไร (ตอบ PM addition 2)

**วิธีที่ใช้: คุณหมอเปิดเองผ่าน VNC** — MT5 python bridge API มีแต่คำสั่งจัดการ terminal
ที่ login แล้ว (orders/positions/account_info) ไม่มีฟังก์ชันเปิดบช.ใหม่
ต้องเปิดผ่านหน้าจอ MT5 เท่านั้น ขั้นตอนต่อใบ (~2 นาที):

1. เปิด VNC ของ container นั้น: `http://VPS_IP:5904` (P1), `:5905` (P2), `:5906` (P3)
   user `mt5user` / pass `mt5password`
2. ใน MT5: **File → Open an Account** → พิมพ์ `Exness` → เลือก **Exness-MT5Trial7** (demo)
3. เลือก **Open a demo account** (ไม่ต้องฝาก) → กรอกฟอร์ม (email อะไรก็ได้ที่ใช้จริง)
4. ได้ login+password → **จดไว้ทันที** → ใส่ใน `.env` (ด้านล่าง) ก่อน start engine
5. MT5 จะ login ให้เอง — เห็น balance ใน terminal เป็นจบ

หรือถ้าคุณหมอมีบช. demo อยู่แล้วใน Exness → ข้ามขั้นตอนเปิด เอาแค่ login/password ใส่ `.env`

## .env ที่ VPS ต้องเพิ่ม (ห้ามใส่ใน repo)

```env
# บช. demo 3 ใบ (จากขั้นตอน VNC ด้านบน)
MT5_LOGIN_P1=...        MT5_PASSWORD_P1=...        MT5_SERVER_P1=Exness-MT5Trial7
MT5_LOGIN_P2=...        MT5_PASSWORD_P2=...        MT5_SERVER_P2=Exness-MT5Trial7
MT5_LOGIN_P3=...        MT5_PASSWORD_P3=...        MT5_SERVER_P3=Exness-MT5Trial7

# ที่เก็บ DB ของ P-accounts (bind mount — host cron ต้องอ่านได้)
PORTFOLIO_DATA_DIR=/opt/god-port/data
```

ไม่ต้องใส่อย่างอื่น — defaults ใน compose ครบตาม variant matrix
(ATR/RR/conf/entry hours/risk ต่อใบอยู่ใน `docker-compose.vps.yml` แล้ว)

## ลำดับ deploy

```bash
# 0. บน VPS: pull branch 2026-07-01-live-trader-bugfix ที่มี commit นี้
#    (image ที่ build จะมี fix afb6d5d อยู่ด้วย — จำเป็น เพราะ engine ใหม่ใช้ AEGIS gate)

mkdir -p /opt/god-port/data/p1 /opt/god-port/data/p2 /opt/god-port/data/p3

# 1. บันทึก start-time ของ service เดิมก่อน (เพื่อ verify ทีหลัง)
docker ps --format '{{.Names}} {{.Status}}' | grep -E 'oracle-engine|mt5[a-d]' | tee /tmp/pre-deploy-ps.txt

# 2. เปิด MT5 containers ก่อน (ให้คุณหมอ VNC เข้า login บช. demo ได้)
docker compose -f docker-compose.vps.yml up -d mt5p1 mt5p2 mt5p3

# 3. ← คุณหมอเปิดบช. demo ผ่าน VNC ตามขั้นตอนด้านบน ใส่ .env เสร็จ ←

# 4. build + start engines ใหม่ (เฉพาะ 3 ตัวนี้)
docker compose -f docker-compose.vps.yml up -d --build oracle-engine-p1 oracle-engine-p2 oracle-engine-p3

# 5. VERIFY Real-A ไม่โดนแตะ
docker ps --format '{{.Names}} {{.Status}}' | grep -E 'oracle-engine|mt5[a-d]'
#    เทียบกับ /tmp/pre-deploy-ps.txt — start-time ต้องตรงกันเป๊ะ

# 6. host crontab — portfolio manager ทุก 30 นาที (freeze/DD/CB + weekly summary)
crontab -e
# */30 * * * * cd /opt/god-port && source .env && PORTFOLIO_ACCOUNTS=P1,P2,P3 \
#   ACCOUNTS=P1,P2,P3 MT5_BRIDGE_P1_HOST=127.0.0.1 MT5_BRIDGE_P1_PORT=5009 \
#   MT5_BRIDGE_P2_HOST=127.0.0.1 MT5_BRIDGE_P2_PORT=5010 \
#   MT5_BRIDGE_P3_HOST=127.0.0.1 MT5_BRIDGE_P3_PORT=5011 \
#   python3 scripts/portfolio_manager.py --db-dir /opt/god-port/data --psi-dir ψ >> /var/log/portfolio-manager.log 2>&1
```

หมายเหตุ cron: bridge ports 5009-5011 publish ไว้ที่ host แล้ว manager
เลยเข้าผ่าน 127.0.0.1 ได้ / `--psi-dir ψ` เขียน weekly summary ลง ψ/inbox
(ใน VPS checkout) + Telegram

## ตรวจสอบหลัง deploy (รายงานกลับ Hermes)

```bash
docker ps --format '{{.Names}} {{.Status}}' | grep p[123]        # 6 containers Up
docker logs oracle-engine-p1 --tail 50                          # บช. login สำเร็จ, มี candle
sqlite3 /opt/god-port/data/p1/oracle_p1.db "SELECT name, portfolio_status, variant_id FROM accounts;"
# → P1 | running | (เสร็จ seed ตอน engine start)
free -m                                                         # RAM จริง vs แผน 1.8GB
```

สัญญาณแรกควรมีภายใน golden hours ถัดไป — เช็ค rejection reasons ได้:

```bash
sqlite3 /opt/god-port/data/p1/oracle_p1.db \
  "SELECT reason, COUNT(*) FROM signal_rejections GROUP BY reason ORDER BY 2 DESC LIMIT 10;"
```

## เกณฑ์ขยาย P4/P5 (Hermes Q1)

- 48 ชม. healthy: containers Up ต่อเนื่อง, มี signal เข้าระบบ (rejection counts มีข้อมูล)
- ไม่มีบช.ไหนโดน freeze
- ครบแล้วจึง propose เปิด P4/P5 (variant defs พร้อมอยู่ใน generate_variants.py แล้ว)