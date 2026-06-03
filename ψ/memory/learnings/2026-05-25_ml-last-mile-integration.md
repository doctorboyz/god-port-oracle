# ML Last-Mile Integration: Signal First, Filter Second

**Date**: 2026-05-25
**Source**: /rrr retrospective

## Pattern

เมื่อสร้าง ML pipeline สำหรับเทรด — ปัญหาที่เจอบ่อยที่สุดไม่ใช่โมเดลไม่แม่น แต่คือ "ไม่มีสัญญาณให้โมเดลวิเคราะห์" 

เราใช้เวลา 2 sessions ปรับแต่ง XGBoost, calibration, risk-scaling, direction-specific features — แต่ลืมถามว่า "trader ตัวไหนจะใช้ ML บ้าง" สุดท้าย:

- M5 scalp: มี ML แต่โดน session/signal filter บล็อค
- M1 scalp: มี ML แต่ปิดไปแล้ว (bridge issue)
- Swing trader: **ตัวที่เทรดจริงทุกวัน** — ไม่มี ML

**ทางแก้**: ก่อน optimize model → เช็คว่า pipeline end-to-end ทำงานไหม — signal generation → ML evaluation → trade execution

## Why It Happens

- โฟกัสที่ "hard part" (model training) มากเกินไป
- ลืมว่า ML เป็น filter — ต้องมีอะไรให้กรองก่อน
- Architecture diagram ไม่ได้ show data flow จริง (signal → ML → trade)

## How to Apply

- ก่อน train model รอบใหม่: ถาม "trader ไหนจะใช้ ML" → เช็คว่า trader นั้นสร้างสัญญาณอยู่จริง → แล้วค่อย optimize
- ML integration checklist: ✅ ML init ✅ signal generation ✅ feature computation ✅ risk-scaling ✅ trade execution
- "Last mile first" — ต่อปลายท่อก่อนขุดต้นน้ำ
