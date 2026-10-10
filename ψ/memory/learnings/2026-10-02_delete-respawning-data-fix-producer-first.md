# Deleting Data That Respawns — Fix the Producer Before the Cleanup

**Date**: 2026-10-02
**Source**: Group 1 farm monitoring session (ISSUE-102, retro 22.45)

## Situation

หมอสั่งลบ demo accounts A/B/C ออกจาก farm DB ตอนเช้า — ลบแล้ว ตรวจแล้ว ผ่าน. ตอนบ่ายตรวจใหม่ **id 14–16 กลับมาอีก** พร้อม created_at ตรงเวลา redeploy พอดี. สาเหตุ: `oracle_runner.main()` seed A/B/C แบบ unconditional ทุกครั้งที่ boot — การลบ rows เป็นการสู้กับ producer ที่ไม่เคยหยุดผลิต

## The Lesson

**การลบ data ที่มีตัวสร้างที่ยังรันอยู่ = Sisyphean cleanup — ต้องหาและปิด producer ก่อนเสมอ แล้วค่อยเก็บกวาดครั้งเดียวจบ.**

Checklist เมื่อ data "กลับมาหลังลบ":

1. อย่าลบซ้ำทันที — ใช้ created_at / timestamp ของ rows ใหม่ชี้ตำแหน่งเวลาที่มันเกิด
2. งัด event ที่ตรงกับ timestamp นั้น (deploy? restart? cron?)
3. ไล่หา code path ที่ INSERT แบบนั้น (grep insert + table name)
4. Gate มันด้วย env knob ที่ default = legacy behavior (ผู้ใช้อื่นไม่กระทบ) — `SEED_DEFAULT_ACCOUNTS=1` default, farm ตั้ง =0
5. Deploy → พิสูจน์ข้าม restart → **แล้วจึง**ลบ rows ครั้งเดียว
6. ก่อนลบ: เช็ค references (FK, snapshot rows, backup ล่าสุดมีอยู่จริง)

## Bonus: ให้ Monitor ใหม่ทำงานทันที

Snapshot script ตัวใหม่ (เขียนเพื่อ ISSUE-096) เป็นคนเผยให้เห็น zombie ตั้งแต่ run แรก — monitor ใหม่ควรถูกยิงใส่ production จริงทันทีที่เสร็จ ไม่ใช่ "เดี๋ยวค่อยดูพรุ่งนี้" เพราะการเห็นสิ่งที่ไม่ได้ตั้งใจหาคือ value จริงของมัน. และ fire แรกใน context จริง (launchd) จะเผย environment quirk ที่ pytest ไม่มีวันเห็น — PATH ที่ไม่มี /usr/local/bin ทำให้ docker หายใน agent แรก

**How to apply:** ทุกครั้งที่จะลบ data ประจำที่ "โดนสร้างมา" — grep หา INSERT ต่อ table นั้นก่อนลบเสมอ; และทุก monitor/cron ใหม่ ต้อง test-fire ใน scheduler จริง (launchctl kickstart) ไม่ใช่แค่ shell