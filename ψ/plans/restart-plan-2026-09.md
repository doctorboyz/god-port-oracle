# Restart Plan 2026-09 — แผนใหม่บนของที่ prove แล้ว

> เขียน 2026-09-21 ตามคำสั่งคุณหมอ 2026-09-20: "clean เริ่มกันใหม่ เก็บของเดิมเฉพาะที่ prove success ได้"
> ฐาน: Hermes session 2026-09-15 (winrate แนวทาง) + sweet-spot findings + P1-P3 paper deploy

---

## หลักการของแผนนี้

**ทำอะไร:** เทรดแบบ "รอบละน้อย แม่นยิบ ตัดขาดไว" — WR สูง + เล่นเฉพาะช่วงที่ data ยืนยัน
**เพื่ออะไร:** survival + consistent profit (ปรัชญาหลักของ god-port) ไม่ใช่เล่นกลยุทธ์เยอะๆ
**แล้วไง:** พอร์ตโตช้าแต่ไม่แตก เก็บสถิติจริงต่อเนื่องเพื่อพิสูจน์ต่อ

---

## 1. สิ่งที่ PROVEN — เก็บไว้เป็นฐาน

| ของ | หลักฐาน | สถานะ |
|---|---|---|
| Real-A engine + bridge stack (build 5830) | รันจริง 2 เดือน กำไรจริง PF 1.64 | ✅ ใช้ต่อ ห้ามแตะ |
| NY session filter | PF 2.26 / WR 56.9% จาก sweet-spot | ✅ ใช้เป็น filter หลัก |
| Entry hours BKK 01-03, 07-08, 13, 15 | PF 2.07-3.83 | ✅ ใช้เป็น entry window |
| SL cap 10-15$ | SL ≥15$ ติดลบ | ✅ กำหนดใน config |
| Trade age >1h | <1h = -$3,404 | ✅ ใช้เป็น exit rule |
| Confidence 0.5-0.6 | PF 2.43 sweet spot | ✅ ลด MIN_CONFIDENCE จาก 0.60 → 0.55 |
| Stop rules: 3-loss → 24h, DD≥20% freeze | circuit breaker มีแล้วใน portfolio_manager | ✅ ใช้ต่อ |
| 3-layer sizing | 0.25-0.5%/trade, 2-3%/day, 8-10%/system | ✅ ใช้เป็น risk frame |
| Survivor strategy: NY swing | realized RR 1:0.58, avgWin $25.56/avgLoss $14.83 จาก real trades | ✅ แกนของแผน |

## 2. สิ่งที่ DISPROVEN — ห้ามกลับไปหา

| ของ | ทำไมตาย |
|---|---|
| ML v12 (raw-price features) | leakage — acc 94-99% ปลอม |
| High-WR grinder (TP 0.3 ATR / SL 2.5 ATR) | WR 89.6% แต่ PF 0.99 — spread กินหมด (breakeven WR ต้อง 90.9%) |
| m5_scalp mode | แดงใน B/C |
| ML ensemble บน Account A | IRON LAW — ห้าม |
| โหมดเทรดช่วง BKK 10-12, 16, 21 | loss holes |

## 3. Roadmap

### Phase B (กำลังทำ) — ทุก account ใช้งานได้จริง
- build 5830 ลง mt5p1-3 + บล็อก LiveUpdate ทั้ง 7 container ✅ (liveupdate ล็อคแล้ว, mt5p1 rebuild แล้ว)
- engine-p1..3 รัน **โหมด paper** (ไม่ส่ง order — Exness ไม่มี dry run; order จริงทดสอบได้เฉพาะ demo B/C/D)
- มาตรฐานผ่าน: bridge `initialize()=True` + login ถูกบช. + รัน 48 ชม. ไม่ตาย

### Phase D-1 — ปรับ config ตาม sweet-spot (หลัง P1-P3 รันได้)
- NY session filter เป็น gate หลักทุกใบ
- entry window ตามตาราง §6 HANDOFF (P1-P3 มี variant hours ต่างกันอยู่แล้ว — ทบทวนให้ตรง golden hours)
- MIN_CONFIDENCE 0.55, SL cap, trade-age exit
- **คำถามเปิด:** M5_SCALP_ENABLED ปิดทุกใบ? (แดงใน B/C — เสนอปิด รอคุณหมอ confirm)

### Phase D-2 — ML ที่ไม่โกง (ใช้เวลา 1-2 สัปดาห์)
- ตัด raw-price features ให้หมด → ใช้ return-based / relative features
- TimeSeriesSplit + purge, CalibratedClassifierCV, triple-barrier labels (López de Prado meta-labeling)
- cost model จริงของ Exness (spread + swap)
- backfill M5 data ให้ครบ + ตรวจ regime หลัง May 2026 (gold-uptrend bias ของ sweet-spot)
- เกณฑ์ผ่าน: out-of-sample PF >1.3 และ PBO ต่ำ ก่อนแตะ production

### Phase D-3 — พิสูจน์ด้วยเงินจริงน้อยนิด
- order จริงเริ่มที่ **demo B/C/D ก่อน** (สนามทดสอบ order จริงเท่านี้)
- ผ่าน 4 สัปดาห์ → เสนอคุณหมอเปิด P1 (cent, เงินจริงน้อยนิด) ทีละใบ
- P2/P3 ตามหลัง P1 แสดงผลบวกจริง

## 4. ตัวชี้วัดความสำเร็จ (แทน proxy งมๆ)

- พอร์ตไม่แตก: MaxDD < 15% (MC @2% ให้ 13.6%)
- กำไรต่อเนื่อง: positive ทุกสัปดาห์ที่มี ≥10 trades
- เล่นน้อยแม่นยิบ: ไม่เกิน 3 trades/วัน/บช. โดยเฉลี่ย
- ตัดขาดไว: streak 3 ติด = พัก 24 ชม. อัตโนมัติทุกครั้ง

## 5. งบด้านเวลา

| สัปดาห์ที่ | งาน |
|---|---|
| 1 | Phase B จบ + P1-P3 paper 48 ชม. healthy + D-1 config |
| 2-3 | D-2 ML ใหม่ (leakage-free) + backfill data |
| 4+ | demo B/C/D รัน order จริงตาม strategy ใหม่ → เก็บสถิติ 4 สัปดาห์ → เสนอเปิด P1 |

---

*ผูกกับ: HANDOFF ψ/HANDOFF.md · sweet-spot `~/.hermes/infra/notes/godport-sweetspot-findings.md` · Hermes session 20260915_075333*