# Compose ตั้ง env แต่ code hardcode — env knob ต้องมี contract test พิสูจน์ว่า env ถึงตัวแปร

**วันที่**: 2026-10-02 · **Issue**: ISSUE-100 · **File**: broky/signals/generator.py

## เกิดอะไร

`docker-compose.vps.yml` ตั้ง `REGIME_VOLATILE_SKIP=1` บน oracle-engine-train พร้อม comment "เปิด" มาตั้งแต่ 2026-09-21 แต่ `generator.py:90` เขียน hardcode `REGIME_VOLATILE_SKIP = False` — ไม่มี `os.environ.get` เลย ตัวแปรจึงเป็น **no-op เงียบๆ บน production นานกว่าสัปดาห์** (volatile regime ที่ backtest บอกว่าขาดทุน -$94, WR 30.2% ไม่เคยถูก skip จริงบน VPS)

## บทเรียน

1. **ทุก env knob ต้องเป็น pattern เดียวกัน**: `os.environ.get("NAME", "default") in ("1", "true", "True")` อ่านครั้งเดียวตอน import — ตาม `RANGING_HARD_BLOCK`/`TRENDING_HARD_BLOCK` ที่ผ่านสงครามมาแล้ว อย่าเขียน boolean hardcode แล้ว comment ไว้ว่า "จะทำ env-driven"
2. **Contract test ต้องพิสูจน์ "env ถึง module constant" จริง**: import ใน subprocess ใหม่พร้อม env ตั้งไว้ แล้ว assert ค่า flag — แต่ **ห้ามใช้ importlib.reload** (re-trigger StrategyRegistry, ดู tests/test_regime_consistency.py:237) ใช้ `subprocess.run([sys.executable, "-c", "import ...; print(flag)"])`
3. **ตอนเปลี่ยน hardcoded → env-driven ให้สังเกต default ของ compose**: `:-1` ใน compose จะ activate ทันทีที่ code เริ่มอ่าน env — ต้องตัดสินใจพร้อมกันทั้ง code + compose ว่าจะเปิดจริงไหม และเช็ค host `.env` บน VPS ด้วย (host .env ชนะ compose default ผ่าน interpolation)

## ใช้ยังไงต่อ

เพิ่ม knob ใหม่ = copy pattern จาก `RANGING_HARD_BLOCK` + เขียน causal test subprocess แบบ `tests/test_volatile_skip_env_driven_causal.py` ทันที อย่ารอให้ compose โกหกเอง

**Related**: [[2026-09-23_env-set-does-not-mean-env-used]] · [[2026-10-02_env-in-image-beats-compose-fix]] · [[env-in-image-beats-compose]]