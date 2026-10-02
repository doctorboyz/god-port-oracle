# COPY . . ไม่มี .dockerignore = bake data/ 1.6GB + .env ลงทุก image layer

**วันที่**: 2026-10-02 · **Issue**: ISSUE-103 · **File**: Dockerfile, .dockerignore

## เกิดอะไร

Dockerfile L22 `COPY . .` โดยไม่มี .dockerignore → ทุก build bake `data/` 1.6GB (oracle.db 1.4GB), `.git/` 49M, `ψ/` 15M, `tests/` 3.2M และ **`.env` ที่มี MT5 credentials จริง** ลง image ทุกตัว วันที่ 2026-10-02 disk แตกเต็ม 100% (เหลือ 117MB) กลาง deploy ต้อง prune builder ~20GB — และถ้าไม่แก้ root cause มันจะเต็มอีก

## บทเรียน

1. **Runtime ไม่ได้อ่าน baked data/ เลย**: ทุก compose shadow `/app/data` ด้วย volume (named volume หรือ bind mount) — สิ่งที่ "ดูเหมือนจำเป็น" ใน context ไม่จำเป็นจริง ต้องเช็คจาก volume mounts ของ compose ไม่ใช่ความรู้สึก
2. **การเอา `.env` ออกจาก image เปลี่ยน runtime behavior**: `load_dotenv()` ใน container กลายเป็น no-op — ทุก key ที่เคย "หล่อ" มาจาก baked .env จะตกไป code default เงียบๆ ต้อง audit ว่า compose pin ครบไหม **ก่อน** exclude (จุดนี้เจอ gap จริง: `RANGING_HARD_BLOCK=1` ของ local compose มาจาก baked .env line 80)
3. **Lock ด้วย contract test**: tests/test_dockerignore_contract_causal.py บังคับทั้ง "ต้อง exclude พวกหนัก/ลับ" และ control "ห้าม exclude runtime paths" (scripts/ broky/ metty/ shared/) — กันคนหน้าลบออกเกิน
4. ผลจริง: image 3.98GB → **1.67GB** (-2.3GB) และ class การ leak credentials ผ่าน image (ISSUE-099) ตายถาวรกับ build ใหม่

## ใช้ยังไงต่อ

- เพิ่มโฟลเดอร์ runtime ใหญ่ๆ ใน repo = เพิ่มบรรทัด exclude ใน .dockerignore พร้อม test ทันที
- อย่า rebuild Real-A โดยไมัตั้งใจหลังแก้ context — container เดิมยังใช้ image เก่าอยู่ (แค่ build ไม่ recreate)

**Related**: [[2026-10-02_env-in-image-beats-compose-fix]] · [[2026-09-30_macos-crontab-hang-use-launchd]]