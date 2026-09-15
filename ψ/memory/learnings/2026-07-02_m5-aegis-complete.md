# M5 AEGIS Complete — Porting Safety Stack to m5_scalp_trader

> วันที่: 2 กรกฎาคม 2026
> เกี่ยวข้อง: [[2026-07-02_aegis-audit]], [[2026-07-01_system-report]], [[2026-06-20_v6-ml-model-training]]
> ประเภท: learning (architecture + bug fix)
> scope: oracle-engine-train (B/C/D demo) — Real-A ไม่ถูกแตะ

## สรุป

audit 2026-07-02 พบว่า `m5_scalp_trader` ขาด safety stack 4 อย่างที่ `live_trader` (swing) มี:
1. **reversal gate เป็น dead code** — gate (consumer) ต่อแล้ว แต่ generator (producer) ไม่ emit `trend_alignment`/`has_reversal` → gate อ่าน None เสมอ → ไม่เคยบล็อก counter-trend
2. **ขาด trailing TP** — ปล่อยกำไรหายเมื่อราคากลับตัวก่อน TP
3. **ขาด TradeBlocker** — ไม่มี hard_max_lots, margin_safety, sl sanity, anti-churn
4. **ไม่เรียก `record_trade_open()`** — anti-churn counter ไม่ fresh

Tasks #80-86 แก้ครบ: generator emit indicators + port 3 กลไก + 3 causal test suites + deploy + บันทึกนี้

## นิยาม "counter-trend" vs "reversal trade" (ตาม CLAUDE.md)

| คำ | นิยาม | กระทำ |
|-----|--------|------|
| Trend-following | เข้าตามทิศ trend หลัก | ✅ หลัก |
| Reversal trade | เข้าสวนเมื่อมี reversal signal ชัด (HH/LL + OB/OS + divergence) | ⚠️ ยอมได้ |
| Counter-trend | เข้าสวน trend ย่อยโดยไม่มี reversal signal | ❌ ห้าม |
| Ranging | ADX < 25 หรือ trend ไม่ชัด | 🛑 พัก |

## กลไก 3 ขั้นของ reversal gate

### ขั้น 1: `detect_swing_structure(high, low, lookback=20)`
ตรวจ swing-high / swing-low ล่าสุด ใน last 20 bars (M5 ≈ 100 นาที) → ส่งคืน dict กับ `made_lower_low` / `made_higher_high`
- SELL ใน bullish D1 ต้องมี `made_lower_low` (uptrend structure breaking)
- BUY ใน bearish D1 ต้องมี `made_higher_high` (downtrend structure breaking)

### ขั้น 2: `compute_reversal_signal(direction, d1_trend, h4_trend, rsi, stoch_k, boll_pct_b, mfi, macd_hist, plus_di, minus_di, boll_bw, swing)`
รวมหลักฐาน reversal 5 ตัว: OB/OS (RSI/Stoch/Boll/MFI) + MACD divergence + ADX directional → คืน `(has_reversal, strength)`

### ขั้น 3: `compute_trend_alignment_value(direction, d1_trend, h4_trend, has_reversal)`
คืนค่า trend_alignment:
- `1` = aligned (BUY in bullish / SELL in bearish)
- `0` = neutral (HOLD หรือ trend unknown)
- `-1` = counter-trend ไม่มี reversal → **gate บล็อก**
- `2` = counter-trend มี reversal → ยอม (legitimate reversal trade)

## สาเหตุที่ m5 gate เคยเป็น dead code

```
Producer (m5_scalp_generator)  →  Signal.indicators  →  Consumer (gate)
        ❌ ไม่ emit keys              ❌ อ่าน None = ผ่าน         ✓ ต่อแล้ว
```

Task #77 ต่อ consumer (gate) แต่ลืมแตะ producer (generator) → gate อ่าน `signal.indicators.get("trend_alignment")` ได้ None → เงื่อนไข `trend_alignment == -1` เป็น False เสมอ → ไม่เคยบล็อก

## บทเรียนสำคัญ

**ตอน wire gate ต้อง verify ทั้ง producer และ consumer — gate test ที่ mock signal เข้า gate ไม่ catch bug นี้**

- Test ที่ mock `Signal(indicators={"trend_alignment": -1, ...})` จะผ่าน แม้ producer จริงไม่ emit key นั้น
- ต้องมี causal test ที่เรียก producer จริง แล้วเอา output ยัดเข้า consumer — prove ทั้งสองต่อเชื่อมแล้ว

อ้างอิง: `tests/test_m5scalp_reversal_gate.py::TestGeneratorEmitsReversalIndicators` (3 tests)
- `test_bullish_signal_has_trend_alignment_and_has_reversal_keys` — keys ต้องอยู่
- `test_counter_trend_signal_emits_negative_alignment` — SELL vs bullish D1 → -1
- `test_generator_to_gate_integration_blocks_counter_trend` — end-to-end producer→consumer

## การ port จาก swing ไป m5

| กลไก | swing (live_trader) | m5 (m5_scalp_trader) | ทำอย่างไร |
|------|---------------------|---------------------|-----------|
| Trailing TP | arm 0.20% MFE, trail 0.10% | arm 0.20%, trail 0.10% (เหมือนกัน) | port config + arm/fire logic ใน `_monitor_positions` |
| Time stop | 288 bars M5 = 24h | 0 → fallback max_holding_bars=12 (1h); env override 288 ได้ | `time_stop_bars` field, m5 scalp ปกติเก็บสั้น 1h พอ |
| TradeBlocker | ใช้ real MT5 `free_margin` (ISSUE-060) | port `_get_free_margin` + wire gate หลัง min-lot reject | ใช้ `_get_account_config()` ที่มีอยู่ → `MT5Bridge.fetch_account_info_sync().free_margin` |
| record_trade_open | 3 sites (dry-run, live, scale-in) | 3 sites (เหมือนกัน) | no-arg call ใน try/except (defensive) |

## Verification end-to-end บน VPS (2026-07-02 23:57 UTC)

หลัง deploy `oracle-engine-train` (commit `c476333`):

- **Container**: `Up 15 seconds (healthy)` ✓
- **MT5 bridges**: B/C/D ทั้ง 3 เชื่อมได้
  - B: equity $201.28, free_margin $201.28
  - C: equity $850.34, free_margin $850.34
  - D: equity $321.90, free_margin $321.90
- **Code โหลดถูก** (verify via inspect):
  - `trailing_tp_enabled=True` ✓
  - `_trade_blocker` exists ✓
  - `_get_free_margin` callable ✓
  - gate wired in `_run_once_connected` (`self._trade_blocker.check`) ✓
  - 3 `record_trade_open()` call sites ✓
- **trailing_tp ไฟร์จริงบน m5**: 4 ครั้งใน 7 วัน (B=1, C=1, D=2) — ก่อนหน้านี้ m5 ไม่มี
- **counter_trend / trade_blocker rejections**: 0 ครั้ง — เพราะ Asian session บล็อกสัญญาณก่อนถึง gate (308 ครั้ง). จะไฟร์เมื่อ London/Overlap/NY session มี counter-trend signal หรือ misconfigured lot/risk

## ข้อควรระวัง

1. **`time_stop_bars=0` default** — preserve m5 scalp behavior เดิม (1h max hold) ไม่เปลี่ยนพฤติกรรม แค่ทำให้ configurable. ถ้าอยาก 24h ตั้ง `TRADE_BLOCKER_TIME_STOP_BARS=288` (แต่ m5 scalp เก็บสั้น 24h ไม่เหมาะ)
2. **TradeBlocker ใช้ real MT5 `free_margin`** — ถ้า bridge ล่ม จะ fallback ไป local estimate `max(equity - margin_required, 0)` (อาจ undercount ถ้ามี open positions อื่น)
3. **reversal gate bypasses ใน `learning_mode`** — เพื่อ ML data collection. บน B/C/D demo `LEARNING_MODE` เปิดอยู่ → gate จะไม่บล็อก counter-trend. ปิด `LEARNING_MODE=0` เพื่อ enforce gate เต็มที่
4. **Real-A ไม่ถูกแตะ** — deploy เฉพาะ `oracle-engine-train` (B/C/D demo). กฎเหล็ก: ไม่ deploy อะไรไปบัญชีจริงโดยไม่ได้รับอนุมัติ

## ไฟล์ที่แก้ (Tasks #80-83)

| ไฟล์ | การแก้ | Task |
|------|-------|------|
| `broky/signals/m5_scalp_generator.py` | emit trend_alignment/has_reversal/reversal_strength/made_lower_low/made_higher_high | #80 |
| `metty/execution/m5_scalp_trader.py` | port trailing TP + time_stop_bars | #81 |
| `metty/execution/m5_scalp_trader.py` | port TradeBlocker + _get_free_margin + wire gate | #82 |
| `metty/execution/m5_scalp_trader.py` | record_trade_open() at 3 sites | #83 |
| `tests/test_m5scalp_reversal_gate.py` | +3 causal tests (producer+consumer wiring) | #84 |
| `tests/test_m5_scalp_trailing_tp.py` | (ใหม่) 8 trailing TP tests | #84 |
| `tests/test_m5_scalp_trade_blocker.py` | (ใหม่) 9 TradeBlocker tests | #84 |
| `scripts/pre-deploy-check.sh` | ลบ scalp_trader reference (retired Task #79) | #85 |

Commit: `c476333 feat: complete m5 AEGIS safety stack — reversal gate, trailing TP, TradeBlocker`
Tests: 29 passed, 2 skipped, full regression 636 passed.