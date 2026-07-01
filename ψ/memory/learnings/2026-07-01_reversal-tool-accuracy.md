# Reversal-Detection Tool Accuracy — Premium + Exness

> Date: 2026-07-01
> Source: scripts/reversal_accuracy_eval.py
> Datasets: premium M5 200k bars (2023-06 to 2026-04) + Exness M5 20k bars (Mar-Jul 2026)
> Related: [[2026-07-01_trailing-decision-worst-case]], [[2026-06-08_trading-philosophy-trend-following]]

## คำถาม

ที่เราทำไว้ อะไรแม่น อะไรไม่แม่น แค่ไหน — survey reversal-detection tools ใน `broky/indicators/` และวัด precision/recall บน premium + Exness complete backfill.

## เครื่องมือที่มี (8 ตัว)

| Tool | Logic | Trigger |
|------|-------|---------|
| RSI 30/70 | Wilder 14 | cross 30 (OS→UP) / cross 70 (OB→DOWN) |
| Stoch %K 20/80 | 14/3/3 | cross 20 (UP) / cross 80 (DOWN) |
| Stoch %K×%D | 14/3/3 | K crosses D from below 20 / above 80 |
| Bollinger %B 0.15/0.85 | 20, 2σ | %B ≤ 0.15 (UP) / ≥ 0.85 (DOWN) |
| CCI ±100 | 20 | cross -100 (UP) / cross +100 (DOWN) |
| MFI 20/80 | 14 vol-weighted | cross 20 (UP) / cross 80 (DOWN) |
| DeMarker 0.3/0.7 | 14 | cross 0.3 (UP) / cross 0.7 (DOWN) |
| Williams %R -80/-20 | 14 | cross -80 (UP) / cross -20 (DOWN) |

## วิธีวัด

- **Ground truth** = pivot points (N=3 bars แต่ละข้าง, price moves ≥X% หลัง pivot confirmation)
- **Precision** = สัญญาณออกแล้ว price ไปตามทิศที่ทาย ≥X% ภายใน W bars / สัญญาณทั้งหมด
- **Recall** = pivot ที่มีสัญญาณทิศตรงกันใน 6 bars ก่อน pivot / pivot ทั้งหมด
- **Net favorable** = avg move ตามทิศ − avg move สวนทิศ (ตัวเลขจริงที่ขาดทุน/กำไร)

รัน 3 ระดับ: X=0.10% (noise pivot), X=0.30% (small reversal), X=0.50% (real reversal).

## ผล — Combined Premium + Exness (X=0.30%, W=36 bars = 3h)

| Tool | Sigs | Precision | Recall | F1 | Net fav | Verdict |
|------|------|-----------|--------|-----|---------|---------|
| Williams %R -80/-20 | 30,926 | 58.5% | 50.4% | 54.2% | -0.01% | พอใช้ |
| CCI ±100 | 21,123 | 59.8% | 41.8% | 49.2% | -0.02% | พอใช้ |
| Bollinger %B 0.15/0.85 | 23,140 | 58.8% | 41.9% | 48.9% | -0.02% | พอใช้ |
| Stoch %K 20/80 | 16,707 | 59.9% | 33.1% | 42.6% | -0.00% | พอใช้ |
| DeMarker 0.3/0.7 | 14,414 | 61.2% | 27.1% | 37.6% | -0.03% | พอใช้ |
| Stoch %K×%D | 14,561 | 64.7% | 22.2% | 33.0% | -0.00% | พอใช้ |
| RSI 30/70 | 5,851 | 60.3% | 13.0% | 21.4% | -0.05% | พอใช้ |
| MFI 20/80 | 2,009 | 64.7% | 4.6% | 8.7% | -0.02% | พอใช้ |

## ข้อค้นพบหลัก (CRITICAL)

1. **ไม่มี indicator ตัวไหน "แม่น" จริง** — ทุกตัว precision 55-65% ดูดี แต่ **net favorable move เป็นลบ/ศูนย์** (-0.00% ถึง -0.05%). แปลว่าเมื่อสัญญาณออก price ไปตามทิศที่ทายบ่อย แต่ move ที่ได้ **เล็กกว่าหรือเท่ากับ** move ที่เสีย. ใช้ตัวเดียวเทรด = เจ๊ะ.

2. **Precision ลดลงเมื่อ reversal ใหญ่ขึ้น** — X=0.10% precision 73-93% (จับ minor pullback ได้), X=0.30% ลดเหลือ 58-65%, X=0.50% ลดเหลือ 54-60%. คือ indicator จับ "noise pivot" ได้ดี แต่จับ "real reversal" แทบไม่ได้เลย — ส่วนใหญ่ที่ทายถูกเป็น pullback เล็กๆ ที่ไม่ใช่จุดกลับตัวจริง.

3. **Net favorable ลดลงเมื่อ reversal ใหญ่ขึ้น** — ยิ่ง target reversal ใหญ่ ยิ่งเสียเปรียบ. ที่ X=0.50% net = -0.01% ถึง -0.08%. คือสัญญาณ OB/OS ทั้งหมด **ไม่ใช่ edge** ในตัวมันเอง — เป็นแค่ noise classifier.

4. **Williams %R จับ pivot ดีสุด (recall 50%)** แต่ precision ต่ำสุด (54-58%). ตรงกับทฤษฎี: Williams %R = (highest_high - close) / range — sensitive สุด เพราะใช้แค่ close กับ extreme ของ window → ตอบสนองเร็ว แต่ false positive เยอะ.

5. **Stoch %K×%D cross แม่นสุด (precision 65%)** แต่ recall ต่ำ (22%). cross ที่มี confirmation แบบนี้ตัด noise ออก แต่พลาดหลายจุด.

6. **RSI กับ MFI ไม่ค่อยออกสัญญาณ** (recall 4-13%) — ใช้ Wilder smoothing ทำให้เข้าสู่ extreme zone น้อย → เหมาะเป็น confirmation ไม่ใช่ trigger.

7. **Premium vs Exness ให้ผลตรงกัน** — Williams %R best F1 ทั้งคู่, RSI/MFI worst F1 ทั้งคู่. ไม่ใช่ overfit ตัวใดตัวหนึ่ง.

## ข้อสรุปสำหรับการเทรด

**อะไรแม่น อะไรไม่แม่น แค่ไหน:**
- ❌ **ไม่มีตัวไหนแม่นพอใช้คนเดียว** — ทุกตัว net favorable ≤ 0
- ⚠️ **Stoch %K×%D cross** แม่นสุดในกลุ่ม (P=65%) แต่ recall ต่ำ — ใช้เป็น confirmation ตัวหนึ่งได้
- ⚠️ **Williams %R** จับ pivot ได้ครึ่งหนึ่ง (R=50%) — ใช้เป็น "alert" ว่าอาจกลับตัว แต่ห้ามเทรดตามคนเดียว
- ❌ **RSI, MFI** ออกสัญญาณน้อยเกินไป — ไม่เหมาะเป็น trigger หลัก
- ❌ **Bollinger %B, CCI, DeMarker** กลางๆ ทุก metric — ไม่มีจุดเด่น

## สาเหตุที่ indicator ไม่แม่น

1. **Threshold แบบคลาสสิก (RSI 30/70, Stoch 20/80) คือ noise filter ไม่ใช่ reversal detector** — ออกแบบมาเป็น overbought/oversold ของ range ไม่ใช่จุดกลับตัวของ trend
2. **ไม่มี trend context** — ใน strong uptrend, RSI > 70 อาจอยู่ได้นาน (momentum แรง), indicator ออก OB ซ้ำๆ ทั้งที่ price ยังขึ้น → false signal เยอะ
3. **ไม่มี price action confirmation** — สัญญาณออกที่ indicator crossing ไม่ใช่ที่ price กลับตัวจริง. จริงๆ ต้องรอ lower low / higher high หลังสัญญาณถึงจะยืนยัน

## แนวทางต่อ (ยังไม่ทำ — รอ user ตัดสินใจ)

เพื่อให้ "จุดกลับตัวที่แม่น" จริง ต้อง combine:
1. **Trend filter** — รับสัญญาณ reversal เฉพาะใน trending regime (ADX ≥ 25) ที่มี counter-trend move จริง; ใน ranging ปล่อยผ่าน
2. **Confluence (2+ indicators firing within K bars)** — Stoch %K×%D + Williams %R + Bollinger %B extreme พร้อมกัน → precision น่าจะขึ้น ~70%+
3. **Price action confirmation** — รอ 1-2 bars หลังสัญญาณ ถ้ามี lower low (สำหรับ DOWN signal) ถึงยืนยัน entry
4. **Volume confirmation** — OBV divergence หรือ volume spike ที่จุดกลับตัว

หรือเปลี่ยน paradigm: ใช้ **swing-pivot detection** (fractal high/low ที่ break) แทน OB/OS threshold — แต่นั่นคือ layer ใหม่ที่ยังไม่มีใน codebase

## Files
- `scripts/reversal_accuracy_eval.py` — evaluation framework (pivot ground truth + per-indicator precision/recall/net-favorable)
- ผล run เต็ม: 3 ระดับ X=0.10% / 0.30% / 0.50% เก็บใน transcript