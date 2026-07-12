---
name: 2026-07-12-h4-closed-bar-only-real-a
description: Fix #2 — H4 trend คำนวณจาก incomplete resample bar ทำให้ label flip 19+ ครั้งใน 8h เปลี่ยนใช้ closed bar only
metadata:
  type: project
---

# Fix #2: H4 Trend From Closed Bar Only (2026-07-12)

## ปัญหา (Symptom)
หลัง deploy 3 fixes (f393b0c) 07-10 Real-A:
- 2 SELL trades (swing + m5_scalp) เข้าเพราะ M5 regime=trending ผ่าน ranging hard-block
- แต่ H4 กำลัง whipsaw (19+ flips ใน 8h ตาม TREND_FLIP alert)
- ทั้งคู่โดน SL (-$12.16) เป็นสาเหตุหลักของ -6% equity dip

## สาเหตุ (Root Cause)
`broky/data/resampler.py:64` ใช้ `df.resample(freq).agg()` ที่ **รวม in-progress bin** เป็น row สุดท้าย — last row ของ H4 คือ bar ที่ยังไม่ปิด (close = M5 close ล่าสุด ไม่ใช่ H4 bar ที่ปิดจริง)

`_compute_h4_trend` ทั้ง 3 จุด เทียบ `ema10.iloc[-1] > ema50.iloc[-1]` บน row สุดท้าย = incomplete bar นี้:
- ตอน H4 choppy (EMA10 ≈ EMA50) close ของ incomplete bar ที่แกว่งตาม M5 ทำให้ label flip ทุก cycle (5 min)
- multi-TF confirmation กลายเป็น noise เท่า M5 → ให้ false H4 agreement กับ M5 signal
- 07-10: H4 บอก "bearish" ชั่วครู่ (สนับสนุน SELL) แล้ว flip กลับ 30 นาทีให้หลัง — trade โดน SL

**Why:** resample โดย default ไม่ drop incomplete bin; EMA อ่าน iloc[-1] ที่เป็น in-progress close → label ไม่ stable
**How to apply:** ทุกที่ที่อ่าน trend จาก resampled higher-TF ต้อง drop incomplete bin ก่อนคำนวณ ไม่งั้น label = noise ของ lower-TF

## Causal Proof (CPT)
`tests/test_h4_trend_incomplete_bar_causal.py` — 8 tests GREEN
- Closed history 50@100 + 10@110 (EMA10≈108.7, EMA50≈103.3 → bullish บน closed)
- Last close=70: WITH incomplete → bearish (flip), WITHOUT (closed-only) → bullish (stable)
- 96-cycle square wave (70↔150): production flips ~95 ครั้ง, closed-only flips 0 ครั้ง
- A/B พิสูจน์ incomplete bar เป็น cause ของ label noise
- Env override: H4_USE_CLOSED_BAR_ONLY=1 drop last row, =0 legacy

## แก้ (Fix)
Drop incomplete H4 bar ก่อน EMA ใน 3 จุด (commit `43fcfcd`):
- `metty/execution/live_trader.py:598` `_compute_h4_trend`
- `metty/execution/m5_scalp_trader.py:458` `_compute_h4_trend`
- `metty/execution/live_collector.py:69` `_compute_h4_trend`
- Env `H4_USE_CLOSED_BAR_ONLY` (default `1`) — =0 legacy เพื่อ rollback
- `docker-compose.vps.yml` oracle-engine: `H4_USE_CLOSED_BAR_ONLY=1` explicit

## Deploy
- IRON LAW confirmed, `bash scripts/deploy-vps.sh start oracle-engine`
- ML smoke test ผ่าน, equity $351.92 คงที่ก่อน/หลัง deploy
- Verified ใน container: choppy closed history + incomplete 70 → label = bullish (legacy จะได้ bearish)
- env: `H4_USE_CLOSED_BAR_ONLY=1` ตั้งจริงใน container

## Trade-off
- H4 trend ล่าช้าสูงสุด 4h (last closed bar อาจเก่าถึง 4h)
- แต่ H4 ในระบบคือ slow confirmation ไม่ใช่ fast signal — latency ดีกว่า noise
- ถ้าตลาด trend จริงๆ H4 label เปลี่ยนทุก 4h ซึ่งถูกต้อง
- ถ้าตลาด choppy H4 label คงที่ = ไม่มี false confirmation = ไม่มี entry ตาม noise

## แล้วไง (Next)
- เก็บข้อมูลต่อ 24-48h: ดูว่า H4 TREND_FLIP alert ลดลง (จาก 19+/8h เหลือ < 3/8h)
- ดูว่า trade ใหม่มี H4 agreement ที่ stable ขึ้น → win rate ดีขึ้น
- Fix #1 + #2 ทำงานร่วมกัน: #2 ป้อง entry ใน H4 chop, #1 ปล่อย winner วิ่งถึง TP
- ห้ามแตะ B/C/D (oracle-engine-train) จนกว่า Real-A ยืนยันดีขึ้น
- หาก 48h ไม่ดีขึ้น → ลอง H4_USE_CLOSED_BAR_ONLY=0 กับ chop-stability gate (ข้อ 4)

## ข้อ 3-7 ที่ยังไม่แก้
3. ML threshold 0.55 อาจสูงเกิน
4. Ranging block 49h อาจคุมแน่นเกิน + H4-stability gate (flip count window)
5. ATR multiplier 1.5 อาจเล็กไป (SL แนบ entry)
6. PARTIAL_TP_ENABLED=0
7. ML V4 อาจ stale

Related: [[2026-07-12-real-a-post-deploy-3fixes-check]], [[2026-07-12-trailing-tp-widen-real-a]]