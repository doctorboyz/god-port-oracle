---
name: one-hot-regime-encoding
description: Ordinal encoding สำหรับ categorical features ที่ไม่มี natural order (เช่น regime) ทำให้ ML model เข้าใจผิดว่ามีลำดับ. One-hot encoding แก้ปัญหานี้.
metadata:
  type: lesson
  date: 2026-06-19
  tags: [ml, encoding, regime, xgboost, feature-engineering]
---

# One-Hot Encoding > Ordinal สำหรับ Categorical ที่ไม่มี Order

**Lesson**: Regime (trending/ranging/volatile) เป็น categorical ที่ไม่มี natural order. Ordinal encoding (trending=1, ranging=0, volatile=2) บอก model ว่า volatile "มากกว่า" trending ซึ่งผิด. One-hot encoding แก้ปัญหานี้โดยทำให้แต่ละ category เป็น independent binary feature.

**Why**: XGBoost และ tree-based models จะพยายาม split ด้วย regime_encoded > 1.5 (separating volatile) แทนที่จะเรียนรู้แต่ละ regime แยกกัน. ส่งผลให้ model เข้าใจผิดว่า volatile มีความสำคัญมากกว่า trending เพราะมีค่ามากกว่า.

**How to apply**:
- สำหรับ v6+ models: ใช้ `regime_trending`, `regime_ranging`, `regime_volatile` (one-hot)
- สำหรับ v4 backward compat: เก็บ `regime_encoded` (ordinal) ไว้
- เมื่อ v4 retire: ลบ `regime_encoded` ออกจาก feature pipeline
- กฎทั่วไป: categorical ที่ไม่มี natural order → one-hot; ordinal เฉพาะเมื่อมีลำดับจริง (เช่น low < medium < high)

**Transition plan**: ตอนนี้มีทั้ง ordinal + one-hot อยู่ด้วยกันใน features.py. เมื่อ v4 model retire → ลบ `regime_encoded` และ `ENCODED_CATEGORICAL_MAP["regime"]` ออก

**Related**: [[mt5-source-of-truth-ghost-positions]], [[multi-account-dynamic-scaling]]

**Feature registry** (single source of truth):
```python
REGIME_ONEHOT_FEATURES = ["regime_trending", "regime_ranging", "regime_volatile"]
# อยู่ใน broky/ml/features.py — เพิ่ม feature ใหม่ที่นี่ แล้วมันจะ propagate ไปทุกที่
```