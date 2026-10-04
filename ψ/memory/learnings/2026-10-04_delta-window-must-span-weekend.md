---
name: delta-window-must-span-weekend
description: Paper-feed M5 delta window 1d ตายทุกสุดสัปดาห์ — Yahoo คืนค่าว่างพอ window ไม่ครอบ session ศุกร์ ต้องใช้ 5d
metadata:
  type: lesson
  date: 2026-10-04
---

# Delta window ต้องครอบ weekend closure — 1d ไม่พอ

**Symptom**: paper-fetcher container "healthy" แต่ CSV feed เสีย (stale) นาน ~40 ชม.
watchdog แจ้ง "feed stale: newest M5 bar N min old — Yahoo rate-limit or fetcher down?"
ทุก 15 นาทีตลอดสุดสัปดาห์ (57 รอบ) ไม่มี trade ใหม่ตั้งแต่ศุกร์ 2 ต.ค. 21:00 UTC

**Root cause**: `DELTA_PERIOD = {"M5": "1d", ...}` ใน scripts/paper_feed_fetcher.py
GC=F (gold futures CME) ปิดศุกร์ 21:00 UTC ถึงอาทิตย์ 22:00 UTC พอเข้าเสาร์ ~20:00 UTC
หน้าต่าง `period=1d` เลื่อนพ้น session ศุกร์ไปแล้ว Yahoo ตอบ **empty** กลับมาเลย
("possibly delisted; no price data found") — ไม่ใช่ rate-limit ไม่ใช่ fetcher ตาย
fetcher ทำถูกตามดีไซน์: "a failed fetch keeps the previous CSV" เลยเงียบๆ เก็บไฟล์เก่า

**กติกาที่ได้**: incremental poll window ของ feed ตลาดที่ปิดสุดสัปดาห์
ต้องกว้างกว่า **ช่วงปิดตลาดยาวสุด (weekend ≈ 2.3 วัน) + clock skew** เท่านั้นไม่พอ —
ต้องครอบ "เวลาตั้งแต่ session เปิดล่าสุด" เพราะ Yahoo วัดจากหัว window ไม่ใช่ท้าย
M5 ใช้ 5d ก็ครอบแล้ว (H1 ใช้ 5d อยู่แล้วรอดตัวเองมาตลอด)

**Causal test**: tests/test_paper_feed_fetcher.py
- `test_m5_delta_window_spans_weekend_closure` — RED บนค่าเดิม GREEN หลังแก้
- `test_one_day_window_blinds_farm_over_weekend` — replay เหตุการณ์ กันคนกลับไปใช้ 1d

**ช่องโหว่ที่เจอและยังไม่แก้** (แยกเป็นงานถัดไป):
1. watchdog staleness rule (30 นาที) ไม่รู้จัก market hours → false alarm ทั้งสุดสัปดาห์
   ทำให้สัญญาณจริง (จันทร์ feed ตาย) จมอยู่ใน noise
2. paper-fetcher healthcheck แค่ตอบ "OK" — container ฉายว่า healthy ทั้งที่ functionally
   dead 2 วัน; healthcheck ควรวัดอายุ bar ล่าสุดแบบรู้ market hours ด้วย