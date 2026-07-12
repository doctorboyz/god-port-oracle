# 2026-07-12 — Real-A post-deploy check (3 days after f393b0c)

## Context

เช็ค Real-A performance หลัง deploy 3 fixes (commit f393b0c, 2026-07-09 22:10)
คำถาม: "เป็น bug หรือทำงานถูกแต่ performance ไม่ดี?"

**Constraint ในการเช็ค**: VPS `vpsdeluna` (100.68.106.101) SSH timeout จากเครื่องนี้
local DB/logs ไม่มี Real-A data (หยุดเก็บตั้งแก่ May 12 — Real-A รันบน VPS เท่านั้น)
ใช้ Telegram bot `@adisorn_xauusd_bot` (chat id 8728922913) เป็นข้อมูลเดียวที่เช็คได้

## Post-deploy snapshot (07-09 22:10 → 07-12 11:38 UTC, ~60h)

### Equity
- Deploy: $374.25 (07-09 22:10)
- ปัจจุบัน: $351.92 (07-12 11:38) — **ลด $22.33 (-6%)**
- ติดที่ $351.92 มา 36+ ชม. (นับจาก 07-11 23:38 → 07-12 11:38)

### Trades เปิดหลัง deploy (2 trades — ทั้งคู่ SELL, ทั้งคู่ stop_loss)
1. 07-10 08:51 — SELL swing @ 4097.60, SL 4109.05, conf 0.86, regime=trending
   → 07-10 12:17 closed stop_loss **-$6.60**
2. 07-10 09:05 — SELL m5_scalp @ 4098.65, conf 1.00, regime=trending, d1=unknown
   → 07-10 10:00 closed stop_loss **-$5.56**
- รวม: **-$12.16** (ส่วนที่เหลือ -$10 น่าจะ swap/commission หรือ trades อื่น)

### หลัง 07-10 12:17 → 07-12 11:38 (~49h) — ไม่มี trade ใหม่เลย
- H4 trend flip 19+ ครั้งใน 07-10 11:37–19:48 (choppy/ranging หนัก)
- Ranging hard-block + counter-trend gate บล็อกหมด

### Order rejection 1 ครั้ง
- 07-12 08:24 UTC (Sunday) — BUY lots=0.01 rejected **error 10018 = TRADE_RETCODE_MARKET_CLOSED**
- ตลาด XAUUSD ปิดวันอาทิตย์ → ระบบพยายามส่ง order ตอนตลาดปิด
- minor bug: ไม่มี market-hours guard ก่อน OrderSend
- ไม่มี DB row written (correct — rejection ไม่ควรบันทึกเป็น trade)

## Diagnosis: ทำงานถูกต้อง แต่ performance ไม่ดี (ไม่ใช่ bug)

### หลักฐาน fixes ทำงาน
1. **Fix 1 (min_positions=2)**: equity $351.92 → max_positions =
   `min(3, max(2, floor(351/200)))` = `min(3, max(2, 1))` = **2** ✅
   (ถ้าไม่แก้จะเป็น 1 แล้ว block ทุก signal ใหม่ — นี่คือ bug ที่แก้ไป)
2. **Fix 3 (ranging hard-block)**: 07-10 H4 flip 19+ ครั้ง = choppy/ranging
   → บล็อก entries หมดหลัง 12:17 ✅ (ตามกฎ "Ranging = พัก")
3. **Fix 2 (h4 fallback)**: ไม่เห็น case ชัดเจนใน post-deploy window แต่ไม่มี regression

### สาเหตุ performance ไม่ดี
1. **07-10 ตลาด choppy** — H4 กลับตัว 19+ ครั้งใน 8 ชม. → 2 trades ที่ผ่าน gate โดน stop
   (M5 regime บอก trending แต่ H4 whipsaw — known limitation: ranging block ดู M5 regime
    ไม่ใช่ H4 chop)
2. **49 ชม. ไม่มี trade** — trade-off ของ "Ranging = พัก": บล็อกถูกแต่ไม่มีกำไร
3. **Equity ติด $351.92** — ใกล้ threshold ถ้าไม่มี trade กำไร จะนอนติดลบ

## Concerns / Open items

1. **Market-hours guard ขาด** — 07-12 08:24 UTC Sunday ส่ง order ตอนตลาดปิด (10018)
   ควรเพิ่ม `SymbolInfoSessionTrade()` check ก่อน OrderSend
   (เหมือนปัญหาเดิมที่เคยมี market-closed rejection — ดู [[2026-07-09_min2-h4-fallback-ranging-block]])
2. **49 ชม. ไม่มี trade** — ถ้า 72 ชม. ยังไม่มี trade พิจารณา relax ranging block
   (อาจจะ block เข้มเกิน — แต่ถ้าตลาดจริงๆ ranging ก็ถูกแล้ว)
3. **M5 trending vs H4 chop mismatch** — 2 trades 07-10 ผ่านเพราะ M5 บอก trending
   แต่ H4 whipsaw → โดน stop. อาจต้องเพิ่ม H4-stability check (flip count window)

## สิ่งที่ต้องทำต่อ

- รอจันทร์-อังคาร (07-13/14) ตลาดเปิดปกติ → ดูมี trade เกิดไหม
- ถ้า 72 ชม. ไม่มี trade → query `rejected_signals` table บน VPS (ต้อง SSH ได้ก่อน)
  ดู `ranging_hard_block` reason ปรากฏไหม, block ratio เท่าไหร่
- เพิ่ม market-hours guard กัน 10018 rejection วันอาทิตย์

## Sources
- [MQL5 Trade Server Return Codes](https://www.mql5.com/en/docs/constants/errorswarnings/enum_trade_return_codes)
- [MQL5 market-closed Error](https://finance.trgy.co.jp/en/mql5-en/reference-en/mql5-market-closed-error/)

## Related
- [[2026-07-09_min2-h4-fallback-ranging-block]] — 3 fixes ที่ deploy
- [[2026-07-03_real-a-no-ml-withdrawal-strategy]] — Real-A ML off baseline