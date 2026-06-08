---
name: trading-philosophy-trend-following
description: Trading philosophy from doctorboyz — trend-following only, no counter-trend, ranging = pause
metadata:
  type: project
  created: 2026-06-08
---

# Trading Philosophy: Trend-Following Only

## Context
วิเคราะห์ performance วันศุกร์ 5 มิ.ย. (WR 62%, PnL +$387) vs วันอาทิตย์ 8 มิ.ย. (WR 40%, PnL +$40) พบว่า:

- วันศุกร์: counter-trend SELL (d1=bullish) ทำกำไร +$338, WR 88% → แต่นี่คือ reversal trades ไม่ใช่ counter-trend แท้ๆ
- วันอาทิตย์: regime=ranging ทั้งวัน, BUY 22 ครั้ง WR 41% → สัญญาณเข้าผิดจังหวะ
- ความแตกต่างหลัก: วันที่ดีมี trending + ranging, วันที่แย่มีแค่ ranging

## Rules (จาก doctorboyz)

### Counter-trend = ห้าม
- Uptrend (higher high) → ห้าม SELL ย่อย แม้มีสัญญาณ overbought
- Downtrend (lower low) → ห้าม BUY ย่อย แม้มีสัญญาณ oversold
- "แทงสวน" เป็น bad habit ห้ามทำ

### Reversal trade = ยอมได้ (ไม่ใช่ counter-trend)
- Uptrend → ยอม SELL ได้ถ้ามีสัญญาณกลับตัวชัดเจน + overbought + lower low
- Downtrend → ยอม BUY ได้ถ้ามีสัญญาณกลับตัวชัดเจน + oversold + higher high
- นี่คือ reversal trade ไม่ใช่ counter-trend

### Ranging = พัก
- ไม่เข้าเทรดใน ranging (ADX < 25)
- Signal TF (M5) ต้องแสดง trend ชัดเจนก่อนออกสัญญาณ
- Higher TF (D1/H4) ใช้เป็น confirmation เท่านั้น

## ML Implications (for v6 training)
- เพิ่ม `is_counter_trend` flag เป็น feature
- เพิ่ม `trend_alignment` = 1 (aligned), 0 (neutral), -1 (counter)
- Penalize counter-trend losses (increase weight)
- Reduce confidence or skip when regime=ranging
- Label "good trades" only when trend-following or clear reversal

## Related
- [[volatile-regime-threshold-fix]] — BW threshold fix for M5
- [[feature-pipeline-validation]] — feature registry single source of truth