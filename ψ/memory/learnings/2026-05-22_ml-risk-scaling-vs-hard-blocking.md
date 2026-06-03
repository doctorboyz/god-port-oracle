# ML Risk-Scaling vs Hard Blocking

**Date**: 2026-05-22
**Source**: /rrr retrospective

## Pattern

เมื่อ ML model มี signal ที่ weak (แยก winner/loser ไม่เก่ง, accuracy ~55-65%, probability distribution เป็น bimodal) — calibration (isotonic/sigmoid) ไม่ช่วย และ hard blocking (skip trade ถ้า P(LOSS) > threshold) จะบล็อคเกือบทุกเทรด

**ทางออก**: risk-scaling — แทนที่จะ binary block/allow, ใช้ probability เพื่อลด position size

```
P(LOSS) < 0.50 → multiplier = 1.0 (full size)
P(LOSS) 0.50-0.85 → multiplier = linear(0.0 to 1.0)
P(LOSS) > 0.85 → multiplier = 0.0 (skip)
```

## Why It Works

- XGBoost probability distribution มัก bimodal (ใกล้ 0 หรือ 1 มาก) — calibration ไม่ช่วยเพราะข้อมูลไม่พอ (ต้องการหลายพัน samples)
- Risk-scaling ใช้ probability ranking ได้แม้ absolute probability จะ poorly calibrated
- ถ้า model แย่จริง → เทรดยังได้ size ปกติ (multiplier จะไม่เปลี่ยน outcome มาก)
- ถ้า model มี signal บ้าง → risk-adjusted returns จะดีกว่า uninformed trading

## When to Use

- Model accuracy ใกล้เคียงหรือดีกว่า baseline เล็กน้อย
- Sample size < 2000 (calibration ไม่เวิร์ค)
- ไม่อยากพลาดโอกาสเทรดดีๆ ด้วย hard filter

## When NOT to Use

- Model accuracy แย่กว่า random — risk-scaling ก็ไม่ช่วย
- มี samples > 5000 — ลอง calibration ก่อน
- ต้องการ predictable position sizing (risk-scaling ทำให้ lot size แกว่ง)
