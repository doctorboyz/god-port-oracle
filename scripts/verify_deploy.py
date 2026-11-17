#!/usr/bin/env python3
"""Post-deploy verification — read config from the LIVE LiveTrader object.

ISSUE-089 (2026-09-23): "env ตั้งครบ" ≠ "config ถูกใช้". The RR bug shipped
because verification only echoed os.environ — the dead `if not risk_config:`
branch was invisible to it. This script instantiates the real LiveTrader per
account and reads the attribute values the trading loop actually uses.

Run INSIDE the engine container (it needs the container's env + code):

    # from repo root on the VPS:
    docker cp scripts/verify_deploy.py oracle-engine-train:/tmp/verify_deploy.py
    docker exec oracle-engine-train python /tmp/verify_deploy.py --accounts B,C,D

    # against the winner-clone matrix (2026-10-27, exit 1 on any mismatch):
    docker exec oracle-engine-train python /tmp/verify_deploy.py \
        --accounts B,C,D --preset winners

    # against the P22 cent ladder (2026-11-16, engine-p1/p2/p3 containers —
    # run INSIDE each engine container, one at a time):
    docker exec oracle-engine-p1 python /tmp/verify_deploy.py \
        --accounts P1 --preset p22-cent
    docker exec oracle-engine-p2 python /tmp/verify_deploy.py \
        --accounts P2 --preset p22-cent
    docker exec oracle-engine-p3 python /tmp/verify_deploy.py \
        --accounts P3 --preset p22-cent

Real-A check (oracle-engine container) uses --accounts A with NO preset —
it prints values only, so a human confirms against the approved Real-A matrix.

Never trust os.environ alone: see learning 2026-09-23_env-set-does-not-mean-env-used.
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, ".")

# Fields read from the live object: (label, getattr chain, formatter)
_FIELDS = [
    ("strategy_id", "strategy_id", str),
    ("rr_ratio", "risk.risk_reward_ratio", float),
    ("min_confidence", "risk.min_confidence", float),
    ("atr_multiplier", "risk.atr_multiplier", float),
    ("risk_per_trade", "risk.risk_per_trade", float),
    ("time_stop_bars", "risk.time_stop_bars", int),
    ("trailing_tp_enabled", "risk.trailing_tp_enabled", bool),
    ("partial_tp_enabled", "risk.partial_tp_enabled", bool),
    ("mr_mode", "_mr_mode", bool),
    ("mr_boll_threshold", "_mr_boll_threshold", float),
    ("max_spread_points", "_max_spread_points", int),
    ("ml_filter_enabled", "_ml_enabled", bool),
    ("initial_equity", "_drawdown_protector._state.initial_equity", float),
    ("equity_per_position", "_equity_per_position", float),
    ("sl_cap_price", "_trade_blocker.max_sl_distance_price", float),
    ("tp_cost_mult", "_trade_blocker.tp_cost_mult", float),
    ("hard_max_lots", "_trade_blocker.hard_max_lots", float),
]

# Labels asserted from the container's os.environ (not baked into the
# LiveTrader object — read at call time or consumed by the runner).
_ENV_ONLY_FIELDS = {
    "entry_hours": lambda acct: f"ENTRY_HOURS_{acct}",
    "blocked_hours": lambda acct: f"BLOCKED_HOURS_{acct}",
    "dry_run": lambda acct: "DRY_RUN",
    "mt5_server": lambda acct: f"MT5_SERVER_{acct}",
    "mt5_symbol": lambda acct: f"MT5_SYMBOL_{acct}",
    "ranging_hard_block": lambda acct: "RANGING_HARD_BLOCK",
    "m5_scalp_enabled": lambda acct: "M5_SCALP_ENABLED",
    "trade_blocker_daily_limit": lambda acct: "TRADE_BLOCKER_DAILY_LIMIT",
    "trade_blocker_weekly_limit": lambda acct: "TRADE_BLOCKER_WEEKLY_LIMIT",
}

# Approved mr-bet matrix (2026-09-21, commit d433839 + 5e91619 + b51744d).
# Only the knobs the matrix pins — everything else is informational.
_MR_BET_EXPECT = {
    "B": {"strategy_id": "mr-bet-B", "rr_ratio": 0.8, "min_confidence": 0.55,
          "atr_multiplier": 2.0, "risk_per_trade": 0.005, "time_stop_bars": 12,
          "trailing_tp_enabled": False, "partial_tp_enabled": False,
          "mr_mode": True, "ml_filter_enabled": False},
    "C": {"strategy_id": "mr-bet-C", "rr_ratio": 1.0, "min_confidence": 0.55,
          "atr_multiplier": 2.0, "risk_per_trade": 0.005, "time_stop_bars": 12,
          "trailing_tp_enabled": False, "partial_tp_enabled": False,
          "mr_mode": True, "ml_filter_enabled": False},
    "D": {"strategy_id": "mr-bet-D", "rr_ratio": 1.2, "min_confidence": 0.55,
          "atr_multiplier": 2.0, "risk_per_trade": 0.005, "time_stop_bars": 12,
          "trailing_tp_enabled": False, "partial_tp_enabled": False,
          "mr_mode": True, "ml_filter_enabled": False},
}

# Winner-clone matrix (2026-10-27): B=โคลน P21 / C=โคลน P22 / D=โคลน P16 —
# farm selection รอบแรก (league table 2026-10-27: P21 +80.83 / P22 +62.10 /
# P16 +22.73). ค่าตาม spec โจทย์ 27/10 (คุณหมอตัดสิน D=ATR 2.5).
# NOTE-1 D-ATR: container oracle-engine-farm1 วัด ATR_MULTIPLIER_P16=2.0 —
# เทียบผล paper-vs-real ของ D ต้องจำ delta นี้.
# NOTE-2 risk_per_trade ไม่ถูก assert ใน preset นี้ (ไม่อยู่ใน knob matrix
# ของโจทย์): D รัน 1% จาก host .env บน VPS (RISK_PER_TRADE_D=0.01, ยุค
# slow-but-sure) ขัดกับ mr-bet flat 0.5% — ค้างตัดสินใจ ดู handoff 27/10.
# entry_hours/blocked_hours เช็คจาก os.environ ใน container โดยตรง เพราะ
# portfolio_gate.entry_hour_gate อ่าน env ตอน call time (ไม่ bake ใน object).
_WINNERS_EXPECT = {
    "B": {"strategy_id": "mr-bet-p21-B", "rr_ratio": 1.2, "min_confidence": 0.55,
          "atr_multiplier": 2.0, "time_stop_bars": 24,
          "trailing_tp_enabled": False, "partial_tp_enabled": False,
          "mr_mode": True, "ml_filter_enabled": False, "mr_boll_threshold": 0.85,
          "entry_hours": "", "blocked_hours": ""},
    "C": {"strategy_id": "mr-bet-p22-C", "rr_ratio": 1.2, "min_confidence": 0.55,
          "atr_multiplier": 2.0, "time_stop_bars": 12,
          "trailing_tp_enabled": True, "partial_tp_enabled": False,
          "mr_mode": True, "ml_filter_enabled": False, "mr_boll_threshold": 0.85,
          "entry_hours": "", "blocked_hours": ""},
    "D": {"strategy_id": "mr-bet-p16-D", "rr_ratio": 0.8, "min_confidence": 0.55,
          "atr_multiplier": 2.5, "time_stop_bars": 24,
          "trailing_tp_enabled": False, "partial_tp_enabled": False,
          "mr_mode": True, "ml_filter_enabled": False, "mr_boll_threshold": 0.70,
          "entry_hours": "6,8,18,19,20", "blocked_hours": "3,4,5,9,14"},
}

_PRESETS = {
    "mr-bet": _MR_BET_EXPECT,
    "winners": _WINNERS_EXPECT,
}

# P22 cent ladder (2026-11-16, คุณหมอสั่งผ่าน Hermes): engine-p1/p2/p3 run
# the P22 champion on REAL Exness Standard Cent accounts, stage 1 =
# DRY_RUN (orders computed, never sent). Config mirrors demo C
# (mr-bet-p22-C). CENT UNITS: the bridge reports balance/equity in USC
# (1 USD = 100 USC), so every baseline compared against bridge equity
# (initial_equity → in-engine account-level DD line) is pinned in USC —
# 5000/2000/1000, NOT the USD deposit amounts (50/20/10). A USD-unit
# baseline against USC equity is 100x off and the 20% DD line never fires
# (causal proof: tests/test_p22_cent_deploy_causal.py).
_P22_CENT_COMMON = {
    "rr_ratio": 1.2, "min_confidence": 0.55, "atr_multiplier": 2.0,
    "risk_per_trade": 0.005, "time_stop_bars": 12,
    "trailing_tp_enabled": True, "partial_tp_enabled": False,
    "mr_mode": True, "ml_filter_enabled": False, "mr_boll_threshold": 0.85,
    "max_spread_points": 30, "sl_cap_price": 12.0, "tp_cost_mult": 3.0,
    "hard_max_lots": 0.05, "equity_per_position": None,  # per-account USC
    "initial_equity": None,  # per-account USC
    "entry_hours": "", "blocked_hours": "",
    "dry_run": "1", "ranging_hard_block": None,  # None = must be UNSET
    "m5_scalp_enabled": "0",
    "mt5_server": "Exness-MT5Real25", "mt5_symbol": "XAUUSDc",
    "trade_blocker_daily_limit": "10", "trade_blocker_weekly_limit": "30",
}
for _name, _usc in (("P1", 5000.0), ("P2", 2000.0), ("P3", 1000.0)):
    _PRESETS.setdefault("p22-cent", {})[_name] = {
        **_P22_CENT_COMMON,
        "strategy_id": f"mr-bet-p22-{_name}",
        "initial_equity": _usc,
        "equity_per_position": _usc,
    }


def _resolve(obj, chain):
    """Walk a dotted attribute chain; return (ok, value)."""
    cur = obj
    for part in chain.split("."):
        cur = getattr(cur, part, None)
        if cur is None:
            return False, None
    return True, cur


def _fmt(label, kind, value):
    if kind is float and value is not None:
        return f"{value:g}"
    if kind is bool:
        return "1" if value else "0"
    return str(value)


def verify_account(account, expect):
    from metty.execution.live_trader import LiveTrader, RiskConfig

    # P-accounts resolve account_id from the accounts table, so the trader
    # must point at THIS container's DB (DB_PATH env, e.g. /app/data/
    # oracle_p1.db — db.py's module default is data/oracle.db and would be
    # the WRONG file in a P engine). Legacy A-D resolve statically and
    # never touch the DB during __init__.
    db_path = Path(os.environ["DB_PATH"]) if os.environ.get("DB_PATH") else None
    # ISSUE-089 follow-up (2026-11-17): construct the trader the way
    # oracle_runner.run_trader REALLY does — it ALWAYS passes a risk_config
    # built from the GENERIC key (RISK_PER_TRADE, 2% default). The old bare
    # construction verified an object the runtime never built: the P22 cent
    # engines shipped 2% sizing (lots 0.04/0.02) with this script green,
    # because only the no-risk_config path honored RISK_PER_TRADE_{account}.
    # Mirroring the runner 1:1 means what this script asserts IS what the
    # trading loop uses. See tests/test_verify_deploy_risk_causal.py.
    risk_config = RiskConfig(
        risk_per_trade=float(os.environ.get("RISK_PER_TRADE", "0.02"))
    )
    trader = LiveTrader(
        account=account, dry_run=True, db_path=db_path,
        risk_config=risk_config,
    )
    rows = []
    failures = []
    for label, chain, kind in _FIELDS:
        ok, value = _resolve(trader, chain)
        if not ok:
            failures.append(f"{label}: <attribute missing: {chain}>")
            rows.append((label, "<missing>"))
            continue
        shown = _fmt(label, kind, value)
        rows.append((label, shown))
        if expect is not None and label in expect:
            want = expect[label]
            got = kind(value)
            if kind is float:
                match = abs(got - want) < 1e-9
            else:
                match = got == want
            if not match:
                failures.append(f"{label}: got {got!r}, expected {want!r}")
    return rows, failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--accounts", default="B,C,D",
                        help="comma-separated account names (default B,C,D)")
    parser.add_argument("--preset", choices=sorted(_PRESETS), default=None,
                        help="assert against a known approved matrix")
    args = parser.parse_args()

    accounts = [a.strip().upper() for a in args.accounts.split(",") if a.strip()]
    if not accounts:
        parser.error("--accounts is empty")

    expect_map = _PRESETS.get(args.preset, {}) if args.preset else {}
    total_failures = []
    for account in accounts:
        expect = expect_map.get(account)
        if args.preset and expect is None:
            print(f"[{account}] ⚠️ no {args.preset} expectations for this account — print only")
        rows, failures = verify_account(account, expect)
        # Env-only knobs (hour gates, DRY_RUN, symbol, scalp path, blocker
        # limits, RANGING_HARD_BLOCK) live in the container environment,
        # not in the LiveTrader object — assert them from os.environ.
        if expect is not None:
            import os
            for label, env_name_fn in _ENV_ONLY_FIELDS.items():
                if label not in expect:
                    continue
                want = expect[label]
                got = os.environ.get(env_name_fn(account))
                shown = got if got not in (None, "") else "<empty>"
                if want is None:
                    # None = the key must be UNSET (e.g. RANGING_HARD_BLOCK
                    # on MR accounts — setting it starves every MR entry).
                    if got not in (None, "", "0"):
                        failures.append(
                            f"{label}: got {got!r}, expected UNSET/0")
                else:
                    if got != str(want):
                        failures.append(
                            f"{label}: got {got!r}, expected {str(want)!r}")
                rows.append((label, shown))
        print(f"[{account}]")
        for label, shown in rows:
            print(f"    {label:<22} {shown}")
        if expect:
            for f in failures:
                print(f"    ❌ {f}")
            if not failures:
                print("    ✅ all preset expectations met")
        else:
            for f in failures:
                print(f"    ❌ {f}")
        total_failures.extend(f"{account}: {f}" for f in failures)

    if total_failures:
        print(f"\nFAILED {len(total_failures)} check(s):")
        for f in total_failures:
            print(f"  - {f}")
        sys.exit(1)
    print("\nOK — all attribute reads succeeded" +
          (" and preset matched" if args.preset else ""))


if __name__ == "__main__":
    main()