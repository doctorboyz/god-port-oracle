---
date: 2026-10-24
type: retro
scope: farm axis2 — กลุ่ม trend-following farm2 (P31/P32/P33) + P23 + per-group mode override + เดินสาย monitoring
commits: (ดู git log — feat: farm axis2)
---

# Axis 2: เปิดแกน trend-following ของ portfolio (farm2) + P23

## เหตุผล (โจทย์ ada-task-20261024-portfolio-axis2)

farm1 ทั้งก้อน MR-only (`TRENDING_HARD_BLOCK=1` process-global) → ตลาด
trending = ทั้ง portfolio เงียบพร้อมกัน (corr ต่ำสุด 0.61 ยังไม่ผ่านเกณฑ์
"ทำกำไรหลายสถานการณ์" ของ PLAN-3PHASES) — จึงเปิดแกนที่สองที่วัด
trend-following ได้จริง + รวม knob ชนะสองสมมติฐาน exit เป็น P23

ปัญหาเชิงโครงสร้าง: `TRENDING_HARD_BLOCK` อ่านตอน import (process-global)
→ กลุ่มใหม่ต้องเป็น container แยก และ `FARM_MODE_ENV` เดิมเป็น global
เดียวทุกกลุ่ม — ต้องเพิ่มกลไก override ระดับกลุ่มก่อน ไม่งั้น farm2
ถูก deploy แต่ทุก cycle trending โดน HOLD = "deployed-but-dead"
วัดผลไม่ได้เลย (คลาสบั๊กเดียวกับ ISSUE-100: config ตั้งแล้วไม่มีใครอ่าน)

## ทำอะไร

1. **T1 per-group mode override** — `paper_farm_apply.py` เพิ่ม
   `_group_mode_env()`: groups.<n>.mode_env ใน yml เขียนทับ
   FARM_MODE_ENV รายคีย์ (replace ในตำแหน่งเดิม ไม่ duplicate env line —
   winner ของ duplicate ไม่ defined ใน compose) คีย์ใหม่ append
2. **T2 P23 (farm1)** — P21+trail: RR 1.20 / BOLL 0.85 / hours ว่าง /
   TS24 / TRAILING_TP 1 (รวม knob ชนะสองสมมติฐาน exit รอบ 16/10:
   P21 +64.83$ PF 1.97, P22 +49.88$ PF 2.29 — ถ้าเสริมกัน P23 ควรดีกว่าทั้งคู่)
3. **T3 farm2 (trend axis)** — ตาม Marks: P31 RR2.5/TS48 baseline,
   P32 RR3.0/TS96 let-run, P33 P31+trail lock · DB แยก farm2.db ·
   mode_env TRENDING_HARD_BLOCK=0 · ชื่อ P3x ไม่ชน farm1 (league merge ตามชื่อ)
4. **T5 monitoring** — league_table: pre-seed ทุก account จากตาราง
   accounts (variant ที่ยัง 0 ไม้ต้องมองเห็น ไม่งั้น "deployed and quiet"
   กับ "not deployed" แยกไม่ออก) · watchdog: watchlist derive จาก
   container_name ใน compose (กลุ่มใหม่ถูกเฝ้าอัตโนมัติ ไม่มี list ที่สอง
   ให้ลืม — read ไม่ได้ fallback farm1) · snapshot ครอบอยู่แล้ว (glob
   data/farm/*/*.db) · ทั้งหมด TDD: causal test RED→GREEN

## หลักฐาน (artifact จริง)

- causal test 3 ไฟล์ใหม่ RED ก่อนแก้ (override ถูกเมิน / 0-trade variant
  ล่องหน / watchdog ตาบอด farm2) → GREEN 54/54 ไฟล์เกี่ยวข้อง
- full suite **1121 passed, 1 skipped** (baseline 1100 → +21 พอดี = test
  ใหม่ ไม่มี failure ใหม่)
- `docker inspect`: farm1 `TRENDING_HARD_BLOCK=1` + ACCOUNTS P11..P23 /
  farm2 `TRENDING_HARD_BLOCK=0` + `ACCOUNTS=P31,P32,P33` + DB_PATH แยก
- DB farm2: P31/P32/P33 balance 100 leverage 2000 · farm1: P23 balance 100
- league table regenerate: **16 variants รวมสองกลุ่ม** (P23+P31-P33
  แถว 0-trade โผล่ครบ)
- compose diff additive: บรรทัดที่ถูกแทนมีเดียว = ACCOUNTS farm1 (เติม P23)
  — env P11-P22 ไม่ถูกแตะ (มี test ล็อกค่าไว้ด้วย)
- RAM จริง (docker stats): farm1 103.5MiB / farm2 79.9MiB /
  fetcher 112.4MiB — สบายบน 8GB

## ระวัง / ข้อจำกัด

- deploy ตอนเสาร์ (ตลาดปิด) — log ยังไม่เห็นสัญญาณ trend จริง หลักฐาน
  mode ตอนนี้คือ env + causal test; หลักฐาน runtime (farm1 ยังยิง
  `trending_hard_block` HOLD / farm2 ไม่ยิง) จะเกิดเมื่อตลาดเปิดอาทิตย์
- farm2 ไม่ตั้ง MR_BOLL_THRESHOLD → ใช้ default 0.70 ของ live_trader:
  ใน regime ranging (ADX<20) ไม้ MR ยังเข้าได้ในแกน trend — อ่านผล
  ต้องแยกตาม regime ไม่ใช่แค่ชื่อ variant
- watchdog เพิ่งเปลี่ยน — รอบถัดไป (launchd 15 นาที) คือ verify จริง
  ครั้งแรกที่เห็น farm2 ใน watchlist
- corr ระหว่างแกนจะยังไม่เก็บได้จนกว่า farm2 มีไม้พอ (≥15-20 ไม้/variant)

## แล้วไง

รอ farm2 วิ่ง 1-2 สัปดาห์: เทียบ P31/P32/P33 ตามสมมติฐาน exit เดียวกับ
รอบ P21/P22 แต่บน RR สูง แล้วดู corr พอร์ตรวมสองแกน — ถ้า trending week
farm1 เงียบแต่ farm2 ทำกำไร = ผ่านเกณฑ์ "หลายสถานการณ์" ข้อแรก
propose ต่อตาม protocol ผ่าน Hermes