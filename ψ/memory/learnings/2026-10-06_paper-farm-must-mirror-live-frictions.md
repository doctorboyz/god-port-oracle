---
name: paper-farm-must-mirror-live-frictions
description: Paper farm ต้องจำลอง friction ของ live ให้ครบ (spread cost ตอน fill + กติกา weekend) ไม่งั้น league pnl/สถิติจะหลอก Marks
metadata:
  type: lesson
  date: 2026-10-06
  issues: [ISSUE-098, ISSUE-106]
---

# Paper farm ต้อง mirror live frictions — mid-fill ฟรี + ไม้ลอยข้าม weekend คือข้อมูลหลอก

**Symptom (2 ประการ จาก league ที่ Marks ใช้ตัดสิน)**

1. **ISSUE-098**: fill ทุกขาของ dry-run เป็น mid price — entry ที่ mid, exit ที่
   level SL/TP ตรงเป๊ะ → round trip ไม่มีค่า spread เลย ทั้งที่ live จ่าย ~1 spread
   ต่อไม้ (XAUUSD $0.20 ที่ MOCK_SPREAD_POINTS=20) → league pnl หวานกว่าจริง
   selection window อาจเลือก variant ที่ไป live แล้ว edge หาย
2. **ISSUE-106**: ไม้ศุกร์เย็นลอยข้าม weekend — ทุก exit check ต้องรอ bar M5
   เดินหน้า แต่ตลาดปิดศุกร์ 21:00 UTC ไม่มี bar ใหม่ → 9 BUYs (2 ต.ค.) ถือ gap
   ข้ามอาทิตย์ pnl gap เข้า league โดย strategy ไม่เคยถูกตัดสินด้วยพฤติกรรมนี้

**กติกาที่ได้**

- **Fill ข้าม spread**: dry-run entry fill = mid ± s/2 (BUY=ask, SELL=bid), exit
  fill = level ∓ s/2 (BUY=bid, SELL=ask) — ราคาจาก `spread_at_entry` ที่บันทึก
 ตอนเข้าไม้ = 1 spread ต่อ round trip เหมือน live โดยประมาณ
  (ส่วนต่างที่ยอมรับ: live trigger ขา SL/TP ใช้ bid/ask จริง จึง shift ครึ่ง spread
   — farm เก็บ trigger ที่ mid แล้วหักครึ่งต่อขาแทน ค่าคลาดเคลื่อน ≤ ครึ่ง spread
   ทิศ conservative)
- **Weekend pre-close (กติกาที่ประกาศแล้ว)**: dry-run ห้ามถือไม้ข้าม weekend —
  ศุกร์ ≥ 20:30 UTC (env `WEEKEND_PRECLOSE_UTC[_<ACCT>]`) ปิดทุกไม้
  `exit_reason=weekend_pre_close` + ห้ามเปิดไม้ใหม่ + ข้าม TP1 scale-in ใน window
- **Boundary ต้อง stamp ให้คนวิเคราะห์เห็น**: league table บอกชัดว่าไม้ก่อน
  2026-10-06 เป็น mid-fill ไร้ cost — อย่าเทียบ pnl ข้ามช่วงตรงๆ

**Causal tests**

- `tests/test_spread_cost_farm_causal.py` (8) — RED 6 ด้วย mid/level fill เป๊ะ
- `tests/test_weekend_preclose_causal.py` (10) — RED 10 ด้วย "กติกาไม่มีเลย"

**พิสูจน์ด้วยการรันจริงบน farm (ไม่ใช่แค่ test)**

- Entry: SELL เปิด 4154.30 ที่ bar mid 4154.40 (−$0.10 = bid) ✓
- Exit: BUY ปิด TP 4 ไม้ exit = TP − $0.10 ทุกตัว ✓ (ไม้เก่า 5 ต.ค. exit ที่ mid — boundary จริง)
- Commit: 5706117 (098), 884b579 (106)

**กฎทั่วไปที่ควรยึด**: ทุก simulation ที่ใช้ตัดสิน strategy (paper farm / dry-run /
backtest) ต้องตอบให้ได้ว่า "friction อะไรที่ live มีแต่เราไม่จำลอง" — spread,
slippage, กติกา session closure, margin — อะไรที่ไม่จำลองต้อง stamp เป็นข้อจำกัด
ในรายงาน ไม่ใช่ปล่อยให้คนวิเคราะห์เข้าใจว่าเป็นความจริง

**งานต่อที่เหลือในแผน 3 เฟส**: lot floor + variant correlation (ทำให้ risk/knob
แยกผลได้จริง) → ISSUE-104 → 107 → 105 → 092 (ดู
[[three-phase-plan]] ψ/goals/active/three-phase-plan-2026-10.md)