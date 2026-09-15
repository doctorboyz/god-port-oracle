---
title: AEGIS counter-trend gate blocked Demo-D trades for 53 days straight
date: 2026-08-25
tags: [aegis, counter-trend-gate, signal-generation, demo-d, no-trade-streak, m5-scalp, swing]
status: learned
---

# 2026-08-25 — AEGIS Counter-Trend Gate Blocks Demo-D 53 Days

## บทเรียนสำคัญ

**AEGIS counter-trend reversal gate (deployed 2026-07-02) บล็อก Demo-D ทุก signal มา 53 วันเลย**

สาขา `2026-07-01-live-trader-bugfix` แนะนำ AEGIS safety stack วันที่ 2026-07-01 ถึง 02:
- `c476333` feat: complete m5 AEGIS safety stack — reversal gate, trailing TP, TradeBlocker
- `5858d2d` feat: wire counter-trend reversal gate into scalp_trader
- `cf1f9e3` feat: wire counter-trend reversal gate into m5_scalp_trader

หลังจากนั้น Demo-D (account_id=4) หยุดเทรด — trade สุดท้าย ticket 3411173137 SELL เปิด 2026-07-02 17:40 แล้วไม่มี trade ใหม่อีกเลย (53 วัน).

**Pattern ที่เจอใน engine log** (565 HOLD ใน 48h ล่าสุด):
- M5 indicators all bearish (macd=-1.0, ema_cross=-1.0, ema_trend=-0.5, adx=-0.5) → BUY blocked
- H4 bullish overrides D1 bearish (h4_override=True) → SELL blocked by counter-trend rule
- Result: HOLD forever — engine log pattern "counter-trend blocked: H4 bullish overrides D1 bearish"

**สำคัญ**: ML ensemble @0.50 ของเรา **ไม่ใช่** สาเหตุ. เรา deploy 2026-08-23, no-trade เริ่ม 2026-07-02 (ก่อนเรา 52 วัน). ลด ML threshold ไม่ช่วย เพราะ signal ไม่ถึง ML gate — block ที่ signal generation ก่อน.

## สิ่งที่ต้องตรวจ (separate issue, ISSUE-086)

1. AEGIS gate แบบนี้ strict เกินไปสำหรับ "slow but sure" strategy หรือไม่?
2. XAUUSD มี 53-day trend misalignment จริงหรือ gate อ่านผิด?
3. ปรับ gate threshold ได้โดยไม่ break กฎ "ไม่แทงสวนเทรนด์" หรือไม่?
4. reconcile stale DB row 3411173137 — broker บอก Position not found แต่ DB ยัง mark is_open=1, engine spam warning ทุก 5 min

## How to apply

1. อย่า blame ML threshold/config ล่าสุด — ดูก่อนว่า no-trade เริ่มเมื่อไหร่. ถ้าเริ่มก่อน deploy → ไม่ใช่ fault ของ deploy.
2. ตอน debug "no trade" ให้อ่าน live_trades history + signals table ก่อน, อย่า assume
3. ตอน deploy strategy ใหม่ใน account ที่มีอยู่แล้ว → ดู last trade date ของ account นั้น ๆ ก่อน. ถ้ามี stale row → flag ทันที.
4. branch name ที่มี date ใกล้กับ behavior change คือ strong signal — `2026-07-01-live-trader-bugfix` → behavior เปลี่ยน 2026-07-02.

## ไฟล์ที่เกี่ยวข้อง

- `ψ/outbox/result_demo-d-20260825T120701Z.md` — manual loop result with full analysis
- [[2026-08-23_demo-d-slow-but-sure-deploy]] — deploy context
- [[2026-08-24_croncreate-session-only]] — auto-loop limitation
- ISSUE-086 in ψ/issues/issues.jsonl