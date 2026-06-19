---
name: dead-code-removal-over-deprecation
description: ถ้า code ไม่มี caller อยู่แล้ว, ลบเลยดีกว่าปล่อยไว้. Dead code ที่ยัง import ได้ทำให้ confuse และ waste time debugging.
metadata:
  type: lesson
  date: 2026-06-19
  tags: [refactoring, dead-code, technical-debt]
---

# Dead Code Removal > Deprecation

**Lesson**: ถ้า code ไม่มี caller อยู่แล้ว, ลบเลยดีกว่าปล่อยไว้. PersistentMT5Bridge 145 บรรทัดไม่มีใครเรียก แต่ยังอยู่ใน pre-deploy-check.sh import list → confusion และ waste time.

**Why**: Dead code ที่ยัง import ได้ทำให้:
1. คนใหม่ (หรือตัวเอง) เข้าใจผิดว่ามันยังใช้
2. Import test ใน deploy script ตรวจจับ class นั้นได้ → คิดว่ามันสำคัญ
3. ถ้าลบ class ทีหลัง ต้องตามแก้ import tests ด้วย (ซึ่งเกิดขึ้นจริงใน session นี้)

**How to apply**:
- ก่อนลบ code: `grep -r "ClassName" .` เพื่อหาทุก reference (รวม scripts, tests, configs)
- ลบทุกที่พร้อมกัน — อย่าทิ้ง reference ค้างไว้
- ถ้ายังไม่แน่ใจว่ามี caller: comment out แล้วรัน full test suite. ถ้าผ่าน → ลบได้
- ถ้าต้องการ deprecation period: ใส่ `@deprecated` decorator พร้อมวันที่จะลบ, แต่ถ้าไม่มี external API → ลบเลย

**Related**: [[mt5-source-of-truth-ghost-positions]], [[multi-account-dynamic-scaling]]