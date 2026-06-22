---
name: v4-stable-on-real-a
description: V4 model pinned on Account A (real) until demo proves new model better
metadata:
  type: project
---

# V4 Stable Profitable on Account A

## Decision

**Account A (real, $100)**: ใช้ V4 model + strict drawdown protection (20/30/30%, cooldown 4h) ตลอด ไม่เปลี่ยนจนกว่า demo accounts จะพิสูจน์ว่า model ใหม่ดีกว่า

**Accounts B, C, D (demo)**: ใช้ model ใหม่ได้เลย (v6 หรือที่ train ใหม่) เพื่อเก็บข้อมูล

## Rationale

- V4 `trending_SELL` sub-model: PF=3.0, test_acc=69% — ดีที่สุด
- V6 `trending_SELL`: PF=0.99, test_acc=49% — แย่กว่ามาก
- Real account ต้องใช้ stable profitable model ไม่ใช่ model ที่ยังไม่พิสูจน์
- Demo accounts เป็นที่ทดสอบ model ใหม่ได้ ไม่เสี่ยงเงินจริง

## Implementation

- `ML_MODEL_DIR_A` env var pin ไปที่ `/app/data/models/trade_outcome_v4`
- `ML_MODEL_DIR` (default) = `/app/data/models/trade_outcome_v6` สำหรับ B/C/D
- `DRAWDOWN_DAILY_LIMIT_A=0.20`, `DRAWDOWN_WEEKLY_LIMIT_A=0.30`, `DRAWDOWN_ACCOUNT_LIMIT_A=0.30`, `DRAWDOWN_COOLDOWN_HOURS_A=4`
- ทั้ง LiveTrader และ M5ScalpTrader อ่าน `ML_MODEL_DIR_{account}` ก่อน fallback ไป `ML_MODEL_DIR`

## When to Change

เปลี่ยน model บน Account A ได้เมื่อ:
1. Demo account ใช้ model ใหม่อย่างน้อย 4 สัปดาห์
2. Demo มี WR > 55% และ PF > 1.5
3. Demo drawdown ไม่เกิน 15%
4. มีข้อมูลเพียงพอ (min 50 trades)

**Why:** หมอเชื่อ V4 เพราะ trending_SELL ดีมาก (PF=3.0) และผลจริงดีบน real account

**How to apply:** ถ้า train model ใหม่ให้ deploy บน demo accounts ก่อน รอ 4 สัปดาห์ เปรียบเทียบผล ถ้าดีกว่าจึงเปลี่ยน A