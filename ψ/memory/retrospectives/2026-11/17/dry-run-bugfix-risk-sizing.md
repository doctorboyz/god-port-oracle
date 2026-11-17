# Retro — DRY_RUN bugfix 2 ตัว: risk 2% แทน 0.5% + kill_switch shape guard

**งาน**: คำสั่งคุณหมอ 17/11/2026 (ผ่าน Hermes) — bug จริงจาก DRY_RUN บน VPS หลัง P22 cent stage 1 (commit d2464ac) · ปิดงาน 17/11/2026
**Branch**: mr-bet-bcd

## อะไรเปลี่ยน (3 ไฟล์โค้ด + 3 ไฟล์ test)
1. `metty/execution/live_trader.py` — `RISK_PER_TRADE_{account}` env **always win** เหมือน RR/conf/ATR (แก้จุดเดียว ~15 บรรทัดรวม comment ไม่แตะสูตร sizing/SL/TP แม้บรรทัดเดียว)
2. `scripts/kill_switch.py` — `HandleSafeBridge.account_info` เช็ค shape ก่อน materialize (non-mapping → read-failed ของบช.นั้น + log จำแนกชัด ไม่ใช่ "failed" กลางๆ)
3. `scripts/verify_deploy.py` — `verify_account` สร้าง LiveTrader **แบบ runner จริง** (ส่ง risk_config จาก generic key เหมือน oracle_runner:253) — ปิดช่องบอดที่ทำให้ bug ผ่านไปได้
4. Tests ใหม่ 44 ตัว: `test_risk_env_override_causal.py` (37) · `test_kill_switch.py` +6 shape-safety · `test_verify_deploy_risk_causal.py` (3)

## บั๊กที่ 1 — root cause จากโค้ดจริง (ตรง hypothesis คุณหมอเป๊ะ)
- `oracle_runner.py:253` สร้าง `RiskConfig(risk_per_trade=os.environ.get("RISK_PER_TRADE", "0.02"))` จาก **generic key** แล้วส่งเข้า LiveTrader **เสมอ**
- `live_trader.py:215` เดิม: `self.risk = risk_config or RiskConfig(risk_per_trade=_risk_per_trade)` — `or` short-circuit ทิ้งค่า per-account env ที่ resolve ไว้บรรทัด 206 ทั้งฉบับ
- คลาสเดียวกับ RR bug 2026-09-23 (fix ครั้งนั้นแก้ RR/conf แต่**ไม่ได้แก้ risk_per_trade ตัวเอง**) — pattern "env must ALWAYS win" มีอยู่แล้ว 3 จุดในไฟล์เดียวกัน จุดที่ 4 ถูกปล่อย
- พิสูจน์ RED→GREEN: 29 tests fail ตอน pre-fix (ได้ 0.02 ตรง production) → 37 passed หลัง fix

## ทำไม 2 เดือนไม่มีใครเห็น (บันทึกเป็นความรู้ — ไม่ลบข้อมูลเก่า)
- **ฟาร์มรัน risk 2% มาตลอด ~2 เดือน** (P11–P24 ตั้ง 0.005 ใน compose แต่ runtime ถือ 0.02) — ไม้ฟาร์มไม่กระทบเพราะ equity $100 @ SL ~$6 → raw lots 0.0008 (0.5%) กับ 0.0033 (2%) **floor ที่ 0.01 เหมือนกันทุกไม้** — สถิติ farm ที่เก็บมาทั้งหมดยังใช้ได้เพราะ lots จริงไม่เคยต่างจากเจตนา
- cent P2/P3 (USC 2000/1000) คือบช.แรกที่ risk budget 0.5% พ้น floor → sizing ผิดโผล่ทันทีวันแรกของ DRY_RUN
- **ช่องบอดของตัวตรวจ**: `verify_deploy.py` สร้าง object แบบไม่ส่ง risk_config → เห็นสาย env (ถูก) ในขณะที่ runtime สร้างต่างจากนั้น — ตัวตรวจเขียวทั้งที่ของจริงผิด ("env ตั้งครบ" ≠ "config ถูกใช้" คือ ISSUE-089 เดิม แต่ครั้งนี้พังที่ชั้น construction ไม่ใช่ชั้น env)
- `test_p22_cent_deploy_causal.py` เดิมก็พลาดจุดเดียวกัน (สร้างแบบไม่ส่ง risk_config) — test ใหม่ `test_compose_risk_lands_on_object` บังคับ construction แบบ runner ในทุก test ที่เกี่ยว risk

## บั๊กที่ 2 — ข้อเท็จจริงที่ต่างจาก hypothesis เดิม (สำคัญ)
- hypothesis คุณหมอ: "โค้ด materialize พัง → raise กลาง cycle" · **ความจริงจากโค้ด: except จับ ValueError อยู่แล้ว** — log ที่เห็นบน VPS ("dictionary update sequence element #0 has length 5; 2 is required") คือข้อความจาก except handler เอง supervisor **ไม่เคย crash** วงจรไหนเลย
- ที่พังจริงคือ **ชั้นต้นน้ำ**: `docker/mt5/mt5_bridge_server.py:_to_dict` มี fallthrough `return str(obj)` (บรรทัด 21) เมื่อ MT5 คืน object ที่ไม่ใช่ namedtuple/dict ตอน transient → client ได้ของที่ไม่ใช่ mapping → `dict()` ระเบิดเป็น ValueError พอดี
- สิ่งที่ fix ให้จริง: **จำแนกก่อน materialize** — non-mapping = read-failed ของบช.นั้น + log บอก type ชัดเจน ("non-dict response (type=...)") แยกจาก "bridge ตาย" — ก่อนหน้านี้สองกรณีเขียน log เหมือนกันทำให้ diagnose สับสน
- test ล็อก contract 6 ตัว: error-tuple → None ไม่ raise · string fallthrough → None · log ต้องมาจาก shape guard ไม่ใช่ dict() ValueError · `_observe` จบ cycle ปกติ data_ok=False · handle ไม่โดนแตะ · **dict ปกติยังผ่าน** (guard ไม่ over-block)
- ข้อสังเกตนอกขอบเขต (ไม่แก้ — โจทย์ห้ามล้วง): `HandleSafeBridge.positions()` มี pattern dict() เดียวกัน (except จับอยู่เช่นกัน) และ `flatten()` ที่ `positions()` คืน None จะ return `[]` เงียบๆ — ถ้า shape error เกิดตอน flatten จริง kill switch จะ "คิดว่าปิดครบ" — เสนอเป็นงานถัดไปให้คุณหมอตัดสิน (เปลี่ยน None → raise/fail-loud ใน flatten path)

## สิ่งที่พบระหว่างทาง: flake เดิมใน suite (แกะและแก้ — ขยายขอบเขตโดยแจ้งชัด)
- Full suite รอบ 2 fail ที่ `test_sf_contract_geometry_from_yml` ซึ่ง**ผ่านในรอบ 1** (โค้ดเดิม ลำดับเดิม) → สงสัยพึ่งเวลาจริง → พิสูจน์ด้วย **worktree ที่ HEAD สะอาด (bb5be64 ไม่มีการแก้ของงานนี้เลย)**: fail เหมือนกันเป๊ะ → **เป็น flake เดิม ไม่เกี่ยวงานนี้**
- กลไก (อ่านโค้ดจริง): gate เป็น **frame-relative** (`align_state_to_m5` เห็น bucket ที่ปิดไปแล้วเทียบ close ของแท่งสุดท้ายในเฟรม) แต่ helper สร้าง synthetic tail ต่อจาก `_now()` — เมื่อรันช่วง ~105 นาทีก่อน H4 boundary (ทุก 4 ชม.) tail จะล้ำข้าม boundary → bucket ที่มี decline ปิดตัว → gate อ่าน DOWN แทน FLAT → ไม่เปิดไม้ → **test แดง ~44% ของวันโดยโครงสร้าง** (105/240 นาที ต่อรอบ)
- ทางแก้ (test-only ไม่แตะ core): `_anchored_base` — ถ้า tail ธรรมชาติจะล้ำ boundary ให้ต่อฐานตาม pattern ของมันเอง (flat ต่อ flat / rising ต่อ rise) ถึงก่อน bucket ถัดไป แล้วเริ่ม tail ที่ bucket ใหม่ → bucket ที่ gate เห็นทุกตัวเป็นฐานล้วน · stale guard ไม่กระทบ (วัดที่แท่งสุดท้ายของเฟรม ซึ่งเฟรมเดิมก็ยื่นไปอนาคตอยู่แล้ว)
- Causal test ล็อกถาวร: `TestTailAnchorBucketAware` 4 ตัว — monkeypatch `_now` ให้ตกทั้งตำแหน่งปลอดภัย (นาทีที่ 10 ของ bucket) และตำแหน่งพัง (นาทีที่ 200) → RED ตรงจุดก่อน fix (gate อ่าน DOWN ตรงเป๊ะ) → GREEN ครบ 24/24 หลัง fix
- **เหตุผลที่ขยายขอบเขต**: เกณฑ์เสร็จของงานนี้บังคับ "full suite ผ่านทั้งชุด" แต่ flake นี้ทำให้ suite แดง ~44% ของเวลา — ทิ้งไว้ = ทุก session ถัดไปต้องเจอและต้องสืบใหม่ทั้งที่รู้สาเหตุแล้ว · การแก้อยู่ในไฟล์ test เดิมของมันเอง ไม่แตะ production code แม้บรรทัดเดียว

## หลักฐานจากการรันจริง
- `test_risk_env_override_causal.py`: RED 29 failed / 8 passed ก่อน fix → **37 passed** หลัง fix
- `test_kill_switch.py` + infra + reporting: **65 passed** (รวม shape-safety 6 ตัวใหม่ ที่ guard-test RED ก่อนแก้ — captured log ยืนยัน message production เป๊ะ)
- `test_verify_deploy_risk_causal.py`: contract (1) RED ก่อนแก้ verify_deploy → **3 passed** หลังแก้ — contract (3) พิสูจน์ว่า env หาย → verify จับ 2% ได้ (สิ่งที่หลุดไป production เมื่อวาน)
- Full suite รอบ 1: **1545 passed, 2 skipped, 1 failed in 693.61s (0:11:33)** — fail เดียวคือ `test_iron_scope_matches_manifest` โดยเจตนา: manifest sha256 ของ session kill-switch ล็อก `metty/**` และงานนี้แตะ `live_trader.py` ตามที่คุณหมออนุญาตเป็นการเฉพาะ → แก้ตามทางออกที่ test เองกำหนดไว้ (`--update`) โดย diff ของ manifest ออกมา **บรรทัดเดียว** = hash live_trader.py เท่านั้น (ตรวจด้วยตาก่อนยอมรับ) → `test_core_integrity` **2 passed**
- Full suite รอบ 2 (หลังอัปเดต manifest): **1545 passed, 2 skipped, 1 failed in 692.24s** — core_integrity เขียวแล้ว แต่เกิด fail ใหม่ที่ `test_sf_contract_geometry_from_yml` ซึ่งผ่านในรอบ 1 → พิสูจน์เป็น flake เดิม (ดูหัวข้อแยกด้านบน) → แก้ด้วย `_anchored_base` + causal test 4 ตัว (RED→GREEN) → ไฟล์เต็ม 24 passed
- Full suite รอบ 3 (หลังแก้ flake): **1550 passed, 2 skipped, 0 failed in 692.42s (0:11:32)** — ตรงตามคณิตพอดี: baseline 1500 + ใหม่ 46 (risk 37 + verify 3 + kill-switch 6) + flake-lock 4 = 1550 · EXIT=0
- `git status`: แตะเฉพาะ `metty/execution/live_trader.py` · `scripts/kill_switch.py` · `scripts/verify_deploy.py` · tests 3 ไฟล์ + manifest 1 ไฟล์ — **ไม่แตะ** docker-compose ทุกไฟล์ / farm_variants.yml / docker/mt5/ / broky/ / oracle_runner.py แม้บรรทัดเดียว

## บทเรียน
1. **"ครั้งนี้อนุญาตให้แก้ core" ต้องใช้กับจุดเดียวที่หลักฐานชี้** — แก้ 15 บรรทัดในจุด env-wins ไม่แตะสูตร sizing เลย: หลักฐาน production ชี้ที่ object ถือค่าผิด ไม่ใช่สูตรผิด (probe ใน container พิสูจน์สูตรถูกแล้ว)
2. **Test ต้องสร้าง object แบบเดียวกับ runtime จริง** — test/verify ที่ "สะดวก" ด้วยการตัด constructor parameter ออก คือ test ของอีก program หนึ่ง — กติกาใหม่: ทุก test ที่วัด config ต้องผ่าน `_runner_style_risk_config()` (หรือเทียบเท่า) ไม่ใช่ `RiskConfig()` เปล่าๆ
3. **Bug class "env-wins" มี 4 จุดในไฟล์เดียว** — ครั้งต่อไปเจอ fix pattern แบบนี้ ให้ grep หา pattern น้องที่เหลือทันที ไม่ใช่แก้ทีละตัวเมื่อ production มาเจอทีละอัน (RR 09/23 · ATR 07/13 · risk_per_trade 11/17 — ใครเป็นตัวต่อไป? ส่วนที่เหลือใน __init__ ที่ยังอยู่ใต้ `or`/`if not risk_config` ควร audit รอบหน้า)
4. **"Log เขียวไม่ได้แปลว่าโค้ดทำงานถูก" ในทิศกลับกัน** — log ที่เห็น ("failed: dictionary update...") ดูเหมือนโค้ดพัง แต่คือ except handler ทำงานถูกอยู่แล้ว — อ่านว่า log มาจากบรรทัดไหนก่อนสรุปว่าพัง (บทเรียนเดียวกับ "อ่านกลไกของบทเรียนก่อนเชื่อข้อสรุป" จาก retro kill-switch)
5. **Manifest lock จับการแตะ core ได้จริง — และนี่คือเหตุผลที่ต้องมีมัน** — fail เดียวใน suite รอบ 1 คือแผนที่ manifest ไม่ใช่ bug: กลไกบังคับให้การแตะ `metty/**` เป็น "conscious act" ตาม design ของ session kill-switch · ทางออก (`--update`) ต้องทำหลังตรวจ diff ด้วยตาว่าเปลี่ยนเฉพาะไฟล์ที่ตั้งใจ — ถ้า diff ออกมาหลายบรรทัด = มีอย่างอื่นหลุดมาด้วย ห้ามปล่อยผ่าน

## ค้าง / next
- **Redeploy engine-p1/2/3 บน VPS** = approval แยก (คุณหมอสั่งเอง) — runbook ใน handoff รวมวิธีตรวจไม้ถัดไป (P1/P2/P3 ต้องเห็น 0.01–0.02/0.01/0.01)
- หลัง redeploy ให้รัน `verify_deploy.py --preset p22-cent` ใน container ทั้งสามอีกครั้ง — ตอนนี้ script ตรวจ risk แบบ runner-shape แล้ว เขียวคือเชื่อได้จริง
- **flatten/None ช่องโหว่** ใน `positions()`/`flatten()` (ข้อสังเกตด้านบน) — เสนอให้คุณหมอตัดสินใจเปิดงานถัดไป
- Audit `if not risk_config:` / `or risk_config` ที่เหลือใน live_trader.py และ m5_scalp_trader.py — pattern บั๊กเดียวกันอาจยังมีหลงเหลือ
