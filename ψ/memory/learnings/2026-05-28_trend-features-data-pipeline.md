# Trend Features Blind Spot: When Metadata Stripping Kills ML Training

**Date**: 2026-05-28
**Source**: 2,078 trade_outcomes with zero trend features — root cause traced to backfill metadata stripping

## Pattern

ML training data pipeline มี single point of failure ที่ดู innocuous: backfill function strip "metadata keys" (d1_trend, session, timeframe, etc.) ออกจาก features_json โดยตั้งใจ ทำให้โมเดลไม่มีทางรู้ว่าตลาดอยู่ใน regime อะไร

Pipeline flow: collector → `feature_snapshots` (มี d1_trend) → `backfill_trade_outcomes()` → strip d1_trend → `trade_outcomes.features_json` (ไม่มี d1_trend) → ML training → โมเดลไม่รู้ regime

## What We Learned

1. **"Metadata" vs "Feature" เป็นเส้นบางๆ**: สิ่งที่ดูเหมือน metadata (d1_trend = "bullish") ตอน design อาจกลายเป็น feature สำคัญตอน train regime-specific models — การ strip ควรมีแค่สิ่งที่ไม่มีทางเป็น feature จริงๆ (id, timestamp) ไม่ใช่ strip โดยใช้ assumption ว่า "ไม่ใช่ indicator"

2. **Pipeline visibility matters**: การที่ d1_trend หายไประหว่าง backfill ถูกซ่อนอยู่ใน comment `# Remove metadata keys` — ไม่มี log, ไม่มี warning, ไม่มี test ทุกอย่าง trust ด้วยสายตาและสมมติฐาน

3. **Chicken-and-egg problem**: ต้องการ train โมเดลที่รู้จัก bearish D1 แต่โมเดลปัจจุบันบล็อก trades ใน bearish regime → ไม่มีข้อมูล bearish → ไม่สามารถ train โมเดลที่ดีขึ้นได้ — ต้องมี data collection strategy ที่ยอมขาดทุนเล็กน้อยเพื่อแลกข้อมูล

## Application

- Never strip columns from training data based on assumptions about what's "not a feature"
- Add validation test: after backfill, assert expected keys exist in features_json
- When entering new market regime, temporarily lower ML thresholds to collect data
