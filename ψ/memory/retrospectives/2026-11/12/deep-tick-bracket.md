# Retro 2026-11-12 — Deep Tick + Bracket Grid (bar mode)

**Task:** ada-task-20261111-deep-tick-bracket (คุณหมอผ่าน Hermes)
**Scope:** T1 tick archive · T2 bracket machinery · T3 48-ledger grid · T4 close-out
**Constraint:** additive ล้วน, bridge read-only เท่านั้น, ห้าม lookahead

## ทำอะไร

1. **T1 probe + downloader** — probe พบ tick ลึกสุด **2026-01-01 23:05 UTC**
   (~10.4 เดือน, keys ครบ bid/ask/last/time_msc) · `scripts/tick_download.py`
   chunked/resumable (per-day parquet + progress.json) · rpyc netref ใช้
   str()+ast.literal_eval รอบเดียวตาม pattern ที่แก้ไว้ 11/11
2. **T2 bracket machinery** — โมดูลใหม่ `broky/betbracket/` (ไม่แต้ม broky/bet):
   events 6 ชนิด + resolution 2 โหมด (tick first-passage / bar two-edge
   pes-opt) · differential tests พิสูจน์ equivalence กับ farm
   BetResolver ทั้ง step_tick และ resolve_bet_on_bars
3. **T3 grid** — คุณหมอสั่งเปลี่ยนทางกลางงาน: "อย่ารอ tick — รันบน M5 full
   เลย" → ดึง M5 75,000 แท่ง (2025-10-21 → 2026-11-11) ด้วย
   `scripts/m5_pull_full.py` · `scripts/bet_bracket_grid.py` รัน 48 ledgers
   ใน 30 วินาที · รายงาน ψ/outbox/bet_bracket_deep_2026-11-11.{md,json}
4. **T4** — spot-check อิสระ 3 ledgers (bar-walking loop คนละ implementation)
   ตรงเป๊ะทั้ง WR และ quarterly · commit additive · handoff

## บั๊กจริงที่เจอและแก้

1. **CALL_CAP=5M ทำให้ MT5 ส่ง tick เป็นสิบล้านตัวต่อ call เดียว** —
   copy_ticks_from(date_from, count) เดินหน้าจาก date_from ถึง count ตัว:
   count=5M ที่วันแรกของปี = ~66M ticks → rpyc AsyncResultTimeout ตายตั้งแต่
   day 1 (แท่ง 11/10 รอดเพราะ str ~150MB ยังทัน) → แก้: ลด cap 150k/call +
   continuation loop + retry + per-day skip (วันพังไม่ฆ่าทั้ง run)
2. **เกณฑ์ warmup หาย** — ema_cross ยิงที่ bar 1-3 (EMA50 ยัง seed-biased)
   → guard ข้าม 60 แท่งแรกตาม MIN_HISTORY_BARS ของฟาร์ม
3. **rpyc timeout 60s ของ TickFeed ไม่พอสำหรับ archive chunk** — สร้าง
   ArchiveBridge เฉพาะของ downloader (300s) ไม่แตะ class ของฟาร์ม

## ข้อค้นพบหลัก (bar mode, 75k แท่ง 1 ปี)

- **BUY ledgers แพ้ BE ทุกตัว** — ไม่มีแม้แต่ตัวเดียวที่ E>0
- **สูตรที่ชนะ = ขา SELL ล้วน**: StochRev-SELL-TP10SL20 (WR 69.2% vs BE
  66.7%, E +$2.76/bet, ชนะ 4/4 ไตรมาส) · EMACross-SELL-TP10SL20 (+$1.64,
  4/4) · StochRev-SELL ทุก geometry · MACD/Boll/RSI-SELL บาง geometry
- ระวังการตีความ: ข้อมูลทั้งปีนี้เป็นยุคเดียว (gold ราคาสูงขึ้นตลอด) —
  ขา SELL ชนะ *ตอน event* ไม่ได้แปลว่าโครงสร้างตลาด bearish แต่แปลว่า
  จุด event ของ indicator กลุ่มนี้เป็นจุดที่ราคา "ย่อ" ต่อเนื่องบ่อยกว่า
  (zigzag) — both_win ~1/3 ของทุก event ยืนยันเรื่องนี้
- ป้าย "คงตัว" ตามเกณฑ์ WR>BE ≥3/4 ไตรมาส มี 20 ตัว แต่ **คงตัว+E>0 จริง
  มี 10 ตัว ทั้งหมดขา SELL**

## ค้างรับ (upgrade ล่าง)

- **tick archive** กำลังดาวน์โหลด detached (nohup) ~7-8 ชม. → ครบแล้วรัน
  tick-mode grid (resolve_bracket พร้อมใช้ + ทดสอบแล้ว) เทียบผลกับ bar mode
  โดยเฉพาะ ambiguous bars (~0.5-1% ของ events) ที่ bar mode ตัดสินไม่ได้จริง
- spot-check ต่อเนื่อง: เทียบ tick-aggregated M5 กับ bridge bars ยืนยัน
  delta ≤ $0.20 (หนึ่ง spread) ทุก OHLC แล้วบน 2026-11-10

## บทเรียน

- copy_ticks_from เป็น count-forward API — "จำกัดหน้าต่างด้วย from_epoch"
  ไม่จำกัดจำนวน ต้องตั้ง count ให้พอดีต่อ call เสมอ
- โจทย์ใหญ่ = commit เป็นทอด: T1+T2 (machinery+tests) commit เดียว, T3
  (grid+report) commit ถัดไป — Hermes --resume ต่อได้ที่จุดใดก็ได้
- differential testing (engine ใหม่ vs engine อ้างอิงของฟาร์ม บน random
  walk 40-70 seeds) จับ semantics drift ได้ก่อนรันงานจริง — ใช้ซ้ำทุกครั้ง
  ที่เขียน resolution engine เร็วกว่าเดิม
