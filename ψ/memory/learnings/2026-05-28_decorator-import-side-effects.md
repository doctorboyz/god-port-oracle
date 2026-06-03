# Decorator Import-Time Side Effects: The Landmine You Don't See Until Import

**Date**: 2026-05-28
**Source**: Deploy failed — `ValueError: Strategy 'swing' already registered` from `@strategy` decorator on utility function

## Pattern

`@strategy(name="swing", ...)` เป็น decorator ที่มี side effect ตอน **import** — มัน register strategy กับ global registry ทันทีที่ Python โหลด module ไม่ใช่ตอน function ถูกเรียก

เมื่อ refactor โค้ดโดยแทรก `compute_trend_alignment()` (utility function) ไว้ใต้ `@strategy(...)` decorator โดยไม่ได้ตั้งใจ → import module → decorator รัน → register "swing" → แล้ว `generate_signal()` ก็มี `@strategy(name="swing", ...)` เหมือนกัน → `ValueError: Strategy 'swing' already registered` → import chain พังทั้งระบบ

## What We Learned

1. **Decorator with import-time side effects demands caution**: `@strategy` ไม่ใช่แค่ metadata — มัน mutate global state ตอน import การเขียน function ใหม่ใต้ decorator โดยไม่ตั้งใจ ไม่ใช่ logic bug แต่เป็น fatal import error ที่ทำให้ module โหลดไม่ได้เลย

2. **Utility functions don't need decorators**: `compute_trend_alignment()` เป็น pure function คำนวณค่า multiplier — ไม่ควรมี decorator ที่ register มันเป็น signal generator การที่มันไปอยู่ใต้ `@strategy` เป็น accident จาก code insertion

3. **Import chain test ควรเป็น mandatory**: ถ้ามี test ที่ import module หลักทุกตัว (`python -c "from broky.signals.generator import generate_signal"`) เราจะรู้ทันทีว่า decorator duplicate — ไม่ต้องรอ deploy แล้วเห็น error บน VPS

## Application

- Before refactoring code near decorators, check what the decorator does at import time
- Utility functions should live in separate modules or be clearly segregated from decorated functions
- Add `import_test.py` ที่ import ทุก module ที่ใช้ใน production — รันก่อน deploy ทุกครั้ง
