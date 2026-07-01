#!/usr/bin/env python3
"""Block verification — proves each blocking signal actually blocks.

Tests every risk block in isolation, no MT5/DB/network. Pure unit tests.
Each test prints PASS/FAIL with evidence (the exact reason returned).

Blocks tested:
  EXISTING (verifies they still block):
    1. risk_per_trade_size — sizing edge cases (no trade when invalid)
    2. _calculate_max_positions — dynamic cap from equity
    3. DrawdownProtector — daily/weekly/account drawdown limits
    4. CircuitBreaker — consecutive losses, daily loss, flash crash, cooldown
    5. risk_per_trade_size — hard cap on lots (max_lots=10)

  NEW (TradeBlocker — gap fillers):
    6. position_limit — open_positions >= max_positions
    7. risk_pct_sanity — risk_per_trade > 5% blocked
    8. sl_too_tight — SL < 0.05% blocked (lots would explode)
    9. sl_too_wide — SL > 5% blocked (likely config bug)
   10. hard_max_lots — lots > 0.50 blocked
   11. margin_safety — margin > 80% of free margin blocked
   12. daily_trade_count — ≥ 20 trades/day blocked (ISSUE-02)
   13. weekly_trade_count — ≥ 80 trades/week blocked

Usage:
  python3 scripts/verify_blocks.py
  python3 scripts/verify_blocks.py --verbose
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, "/Users/doctorboyz/Code/github.com/doctorboyz/god-port-oracle")

from broky.risk.sizing import risk_per_trade_size
from broky.risk.drawdown_protection import DrawdownProtector
from broky.risk.circuit_breaker import CircuitBreaker
from broky.risk.trade_blocker import TradeBlocker, BlockInput


# ---------- Test harness ----------

_results: list[tuple[str, bool, str]] = []

def expect_block(test_name: str, got_blocked: bool, reason: str, expected_blocked: bool = True):
    ok = (got_blocked == expected_blocked)
    _results.append((test_name, ok, reason))
    mark = "✅ PASS" if ok else "❌ FAIL"
    print(f"  {mark} {test_name}: blocked={got_blocked} reason='{reason}'")

def expect_value(test_name: str, got, expected, label: str):
    ok = (got == expected)
    _results.append((test_name, ok, f"{label} got={got} expected={expected}"))
    mark = "✅ PASS" if ok else "❌ FAIL"
    print(f"  {mark} {test_name}: {label} got={got} expected={expected}")


# ---------- 1. Sizing ----------

def test_sizing():
    print("\n=== 1. risk_per_trade_size (sizing sanity) ===")
    # Normal: $200 × 2% = $4 risk. SL distance $12 (4000-3988).
    # lots = $4 / ($12 × 100) = 0.0033 → floored to 0.01 (min)
    lots = risk_per_trade_size(200, 0.02, 4000, 3988, 100)
    print(f"  normal: equity=$200 risk=2% SL=$12 → lots={lots} (math: $4/$1200=0.0033 → floored 0.01)")
    expect_value("sizing_normal", round(lots, 2), 0.01, "lots")

    # Bigger equity: $2000 × 2% = $40 risk ÷ ($12 × 100) = 0.033 → floored 0.03
    lots = risk_per_trade_size(2000, 0.02, 4000, 3988, 100)
    print(f"  bigger: equity=$2000 risk=2% SL=$12 → lots={lots}")
    expect_value("sizing_bigger_equity", round(lots, 2), 0.03, "lots")

    # equity=0 → min_lots (no trade effectively, 0.01 lot is the floor but should be flagged later)
    lots = risk_per_trade_size(0, 0.02, 4000, 3988, 100)
    print(f"  equity=0 → lots={lots}")
    expect_value("sizing_zero_equity", lots, 0.01, "lots")

    # SL distance = 0 → min_lots (avoids div-by-zero explosion)
    lots = risk_per_trade_size(200, 0.02, 4000, 4000, 100)
    print(f"  sl_distance=0 → lots={lots}")
    expect_value("sizing_zero_sl_distance", lots, 0.01, "lots")

    # HUGE risk_pct (50%) — sizing itself clamps to max_lots=10
    lots = risk_per_trade_size(200, 0.50, 4000, 3988, 100)
    print(f"  risk=50% (insane) → lots={lots} (capped at max_lots=10 by sizing fn)")
    expect_value("sizing_insane_risk_capped", lots <= 10.0, True, "lots<=10")

    # Tiny SL (0.01% — 0.40 points) → lots would explode but capped at max_lots=10
    lots = risk_per_trade_size(200, 0.02, 4000, 3999.6, 100)
    print(f"  tiny SL (0.01%) → lots={lots} (capped — but TradeBlocker should catch this)")
    expect_value("sizing_tiny_sl_capped", lots <= 10.0, True, "lots<=10")


# ---------- 2. Dynamic max positions ----------

def _calc_max_positions(equity: float, cap: int = 5, per_pos: float = 200) -> int:
    """Replica of LiveTrader._calculate_max_positions for testing."""
    if equity <= 0:
        return 1
    calc = int(equity // per_pos)
    return max(1, min(cap, calc))

def test_max_positions():
    print("\n=== 2. _calculate_max_positions (dynamic cap) ===")
    cases = [
        ("equity_0", 0, 1),
        ("equity_50", 50, 1),
        ("equity_199", 199, 1),
        ("equity_200", 200, 1),  # exactly 200 → 1 (floor(200/200)=1)
        ("equity_400", 400, 2),
        ("equity_1000", 1000, 5),  # capped at 5
        ("equity_10000", 10000, 5),
    ]
    for name, eq, expected in cases:
        got = _calc_max_positions(eq, cap=5, per_pos=200)
        expect_value(f"maxpos_{name}", got, expected, "max_positions")

    # The BLOCK: open_positions >= dynamic_max → block
    print("  --- block check: open >= max → block ---")
    blocker = TradeBlocker()
    # equity=$400 → dynamic_max=2, open=2 → block
    inp = BlockInput(open_positions=2, max_positions=2)
    v = blocker.check(inp)
    expect_block("maxpos_block_at_2_of_2", v.blocked, v.reason)

    # open=1, max=2 → pass
    inp = BlockInput(open_positions=1, max_positions=2)
    v = blocker.check(inp)
    expect_block("maxpos_pass_at_1_of_2", v.blocked, v.reason, expected_blocked=False)


# ---------- 3. DrawdownProtector ----------

def test_drawdown():
    print("\n=== 3. DrawdownProtector (daily/weekly/account) ===")
    # Account drawdown — equity <= initial*(1-0.30) → permanent block
    dp = DrawdownProtector(initial_equity=100.0, daily_limit_pct=0.20,
                            weekly_limit_pct=0.30, account_limit_pct=0.30)
    can, reason = dp.check(equity=69.0)  # $100 - 30% = $70 → equity $69 < $70 block
    expect_block("drawdown_account_30pct", not can, reason)

    # Equity just above threshold → pass
    dp = DrawdownProtector(initial_equity=100.0, daily_limit_pct=0.20,
                            weekly_limit_pct=0.30, account_limit_pct=0.30)
    can, reason = dp.check(equity=71.0)
    expect_block("drawdown_account_pass", not can, reason, expected_blocked=False)

    # Daily drawdown — record -21% in one day → block
    dp = DrawdownProtector(initial_equity=100.0, daily_limit_pct=0.20,
                            weekly_limit_pct=0.30, account_limit_pct=0.30)
    dp.check(equity=100.0)  # init daily
    dp.record_pnl(pnl=-21.0, equity=79.0)  # -21% loss
    can, reason = dp.check(equity=79.0)
    expect_block("drawdown_daily_20pct", not can, reason)

    # Daily just under threshold → pass
    dp = DrawdownProtector(initial_equity=100.0, daily_limit_pct=0.20,
                            weekly_limit_pct=0.30, account_limit_pct=0.30)
    dp.check(equity=100.0)
    dp.record_pnl(pnl=-19.0, equity=81.0)  # -19%
    can, reason = dp.check(equity=81.0)
    expect_block("drawdown_daily_pass", not can, reason, expected_blocked=False)


# ---------- 4. CircuitBreaker ----------

def test_circuit_breaker():
    print("\n=== 4. CircuitBreaker (consecutive/daily/flash) ===")
    now = datetime.now(timezone.utc)

    # 5 consecutive losses → block (small pnl to not trigger daily limit first)
    cb = CircuitBreaker(daily_loss_limit_pct=0.05, consecutive_loss_limit=5,
                         cooldown_minutes=15)
    cb.set_time(now)
    for _ in range(4):
        cb.record_loss(pnl=-0.50, equity=100.0)  # 4 × -$0.50 = -$2 = 2% daily
    can, reason = cb.can_open_trade(equity=100.0)
    expect_block("cb_4_losses_pass", not can, reason, expected_blocked=False)
    cb.record_loss(pnl=-0.50, equity=100.0)  # 5th → consecutive limit fires
    can, reason = cb.can_open_trade(equity=100.0)
    expect_block("cb_5_losses_block", not can, reason)

    # Daily loss limit — 6% loss > 5% → block
    cb = CircuitBreaker(daily_loss_limit_pct=0.05, consecutive_loss_limit=5,
                         cooldown_minutes=15)
    cb.set_time(now)
    cb.record_loss(pnl=-6.0, equity=100.0)  # -6% daily
    can, reason = cb.can_open_trade(equity=94.0)
    expect_block("cb_daily_6pct_block", not can, reason)

    # Flash crash — 11% drop → block
    cb = CircuitBreaker()
    cb.set_time(now)
    crashed = cb.check_flash_crash(price_drop_pct=11.0)
    expect_block("cb_flash_crash_11pct", crashed, "flash crash detected")
    can, reason = cb.can_open_trade()
    expect_block("cb_after_flash_block", not can, reason)

    # Cooldown expiry → unblock
    cb = CircuitBreaker(consecutive_loss_limit=3, cooldown_minutes=15)
    cb.set_time(now)
    for _ in range(3):
        cb.record_loss(pnl=-1.0, equity=100.0)
    can, reason = cb.can_open_trade()
    expect_block("cb_3_losses_block", not can, reason)
    # Advance time past cooldown
    cb.set_time(now + timedelta(minutes=20))
    can, reason = cb.can_open_trade()
    expect_block("cb_cooldown_expired_unblock", not can, reason, expected_blocked=False)


# ---------- 5-13. TradeBlocker (new) ----------

def test_trade_blocker():
    print("\n=== 5-13. TradeBlocker (new gap-filler blocks) ===")
    b = TradeBlocker(
        daily_trade_count_limit=20,
        weekly_trade_count_limit=80,
        hard_max_lots=0.50,
        max_risk_pct=0.05,
        min_sl_distance_pct=0.05,
        max_sl_distance_pct=5.0,
        margin_safety_factor=0.8,
    )

    # 6. position_limit
    v = b.check(BlockInput(open_positions=3, max_positions=3))
    expect_block("tb_position_limit_3of3", v.blocked, v.reason)
    v = b.check(BlockInput(open_positions=2, max_positions=3))
    expect_block("tb_position_pass_2of3", v.blocked, v.reason, expected_blocked=False)

    # 7. risk_pct_sanity — 6% > 5% max
    v = b.check(BlockInput(risk_pct=0.06, sl_distance_pct=0.30, lots=0.05))
    expect_block("tb_risk_6pct_block", v.blocked, v.reason)
    v = b.check(BlockInput(risk_pct=0.02, sl_distance_pct=0.30, lots=0.05))
    expect_block("tb_risk_2pct_pass", v.blocked, v.reason, expected_blocked=False)

    # 8. sl_too_tight — 0.03% < 0.05%
    v = b.check(BlockInput(risk_pct=0.02, sl_distance_pct=0.03, lots=0.05))
    expect_block("tb_sl_too_tight_0.03pct", v.blocked, v.reason)

    # 9. sl_too_wide — 6% > 5%
    v = b.check(BlockInput(risk_pct=0.02, sl_distance_pct=6.0, lots=0.05))
    expect_block("tb_sl_too_wide_6pct", v.blocked, v.reason)

    # 10. hard_max_lots — 0.60 > 0.50
    v = b.check(BlockInput(risk_pct=0.02, sl_distance_pct=0.30, lots=0.60))
    expect_block("tb_hard_lots_0.60", v.blocked, v.reason)
    v = b.check(BlockInput(risk_pct=0.02, sl_distance_pct=0.30, lots=0.40))
    expect_block("tb_hard_lots_0.40_pass", v.blocked, v.reason, expected_blocked=False)

    # 11. margin_safety — margin $90 > 80% of free $100
    v = b.check(BlockInput(risk_pct=0.02, sl_distance_pct=0.30, lots=0.05,
                            margin_required=90.0, free_margin=100.0))
    expect_block("tb_margin_90of100_block", v.blocked, v.reason)
    v = b.check(BlockInput(risk_pct=0.02, sl_distance_pct=0.30, lots=0.05,
                            margin_required=70.0, free_margin=100.0))
    expect_block("tb_margin_70of100_pass", v.blocked, v.reason, expected_blocked=False)

    # 12. daily_trade_count — 20/20
    v = b.check(BlockInput(risk_pct=0.02, sl_distance_pct=0.30, lots=0.05,
                            daily_trades_today=20))
    expect_block("tb_daily_count_20_block", v.blocked, v.reason)
    v = b.check(BlockInput(risk_pct=0.02, sl_distance_pct=0.30, lots=0.05,
                            daily_trades_today=10))
    expect_block("tb_daily_count_10_pass", v.blocked, v.reason, expected_blocked=False)

    # 12b. learning_mode bypasses daily count
    v = b.check(BlockInput(risk_pct=0.02, sl_distance_pct=0.30, lots=0.05,
                            daily_trades_today=20, learning_mode=True))
    expect_block("tb_daily_count_bypass_learning", v.blocked, v.reason, expected_blocked=False)

    # 13. weekly_trade_count — 80/80
    v = b.check(BlockInput(risk_pct=0.02, sl_distance_pct=0.30, lots=0.05,
                            weekly_trades_this_week=80))
    expect_block("tb_weekly_count_80_block", v.blocked, v.reason)

    # 14. ORDER OF CHECKS — position_limit fires before risk_pct_sanity
    v = b.check(BlockInput(open_positions=3, max_positions=3, risk_pct=0.99))
    expect_block("tb_order_pos_first", v.blocked, v.reason)
    _results.append(("tb_order_pos_first_correct", v.block_name == "position_limit",
                       f"block_name={v.block_name}"))
    print(f"  {'✅' if v.block_name == 'position_limit' else '❌'} "
          f"tb_order_pos_first: block_name='{v.block_name}' (expected 'position_limit')")

    # 15. All-pass case
    v = b.check(BlockInput(open_positions=0, max_positions=3, daily_trades_today=5,
                            weekly_trades_this_week=20, lots=0.05, risk_pct=0.02,
                            sl_distance_pct=0.30, margin_required=10.0,
                            free_margin=100.0))
    expect_block("tb_all_pass", v.blocked, v.reason, expected_blocked=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    print("=" * 72)
    print("BLOCK VERIFICATION — proves each blocking signal actually blocks")
    print("=" * 72)

    test_sizing()
    test_max_positions()
    test_drawdown()
    test_circuit_breaker()
    test_trade_blocker()

    # Summary
    passed = sum(1 for _, ok, _ in _results if ok)
    failed = sum(1 for _, ok, _ in _results if not ok)
    total = len(_results)
    print("\n" + "=" * 72)
    print(f"SUMMARY: {passed}/{total} passed, {failed} failed")
    print("=" * 72)
    if failed:
        print("\nFAILURES:")
        for name, ok, reason in _results:
            if not ok:
                print(f"  ❌ {name}: {reason}")
        sys.exit(1)
    else:
        print("\n✅ All blocks verified — block ได้จริง")
        sys.exit(0)


if __name__ == "__main__":
    main()