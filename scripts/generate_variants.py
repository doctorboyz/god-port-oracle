"""Variant generator for the cent-account portfolio (P-accounts).

Seeds the variants table with the approved 5-variant set (Hermes approval
2026-09-16: deploy P1-P3 first, P4/P5 only after 48h healthy + signals seen).

Every variant comes from the sweet-spot analysis of 2,154 real trades
(oracle_vps.db): NY session PF 2.26 / SL $10-15 / trade age > 1h /
golden hours 01-03, 07-08, 13, 15 BKK / avoid 10-12, 16, 21 BKK.

Demo-D lesson: min_confidence is capped at 0.50 on every variant — a
threshold nobody can reach means zero trades, which proves nothing.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from metty.core.db import (
    get_account_portfolio_state,
    get_connection,
    insert_variant,
    log_portfolio_event,
    set_portfolio_status,
)

BKK_UTC_OFFSET = 7  # Bangkok = UTC+7

# Hours every variant avoids (Hermes: 10-12, 16, 21 BKK are negative-EV)
BLOCKED_HOURS_BKK = [10, 11, 12, 16, 21]

# Risk rules shared by every variant (Hermes requirement #4)
COMMON_RULES = {
    "max_positions": 1,
    "cb_consecutive_losses": 3,       # circuit breaker: 3 losses in a row...
    "cb_pause_hours": 24,             # ...-> pause 24h (persistent cooldown)
    "freeze_dd_pct": 20.0,            # portfolio manager freezes at DD >= 20% from peak
    "no_martingale": True,            # system has none — recorded for audit
    "no_grid": True,
    "trailing_activation_pct": 0.40,  # Real-A Fix #1: don't squeeze winners before 1h
    "trailing_stop_pct": 0.20,
    "ml_ensemble_mode": "or",
    "ml_ensemble_threshold": 0.50,    # same as Demo-D (Hermes Q3: comparable baseline)
}

VARIANT_DEFS = [
    {
        "variant_id": "P1-base-ny",
        "label": "conservative sweet-spot baseline",
        "accounts": ["P1"],
        "atr_multiplier": 2.0,
        "rr_ratio": 3.0,
        "min_confidence": 0.50,
        "entry_hours_bkk": [1, 2, 3, 13, 15],
        "risk_per_trade": 0.005,
        "sweet_spot_basis": "NY session PF 2.26 WR 56.9%; golden hours 01-03/13/15 BKK; SL mid-range $10-15",
    },
    {
        "variant_id": "P2-tight-sl",
        "label": "tight SL at the sweet-spot lower edge",
        "accounts": ["P2"],
        "atr_multiplier": 1.8,
        "rr_ratio": 2.5,
        "min_confidence": 0.50,
        "entry_hours_bkk": [7, 8, 13, 15],
        "risk_per_trade": 0.005,
        "sweet_spot_basis": "SL $10-15 only (>= $15 negative); golden hours 07-08/13/15 BKK",
    },
    {
        "variant_id": "P3-more-entry",
        "label": "wider entry window, lower confidence floor",
        "accounts": ["P3"],
        "atr_multiplier": 2.2,
        "rr_ratio": 3.0,
        "min_confidence": 0.45,
        "entry_hours_bkk": [1, 2, 3, 7, 8],
        "risk_per_trade": 0.010,
        "sweet_spot_basis": "golden hours 01-03/07-08 BKK; Demo-D lesson — conf 0.45 keeps trades reachable",
    },
    {
        "variant_id": "P4-ny-focus",
        "label": "NY-focused, wider SL tolerance",
        "accounts": ["P4"],
        "atr_multiplier": 2.5,
        "rr_ratio": 2.5,
        "min_confidence": 0.45,
        "entry_hours_bkk": [7, 8, 13, 15],
        "risk_per_trade": 0.010,
        "sweet_spot_basis": "NY session PF 2.26; SL upper edge of $10-15 band",
    },
    {
        "variant_id": "P5-wide-sl",
        "label": "widest SL — survives noise, fewest entries",
        "accounts": ["P5"],
        "atr_multiplier": 2.8,
        "rr_ratio": 3.0,
        "min_confidence": 0.45,
        "entry_hours_bkk": [1, 2, 3, 15],
        "risk_per_trade": 0.0075,
        "sweet_spot_basis": "trade age > 1h (max_holding WR 66.9%) — wider ATR lets winners run",
    },
]


def bkk_hours_to_utc(hours_bkk: list) -> list:
    """Convert BKK clock hours to UTC hours, wrapping midnight correctly."""
    return sorted({(h - BKK_UTC_OFFSET) % 24 for h in hours_bkk})


def build_params(defn: dict) -> dict:
    """Full parameter set for one variant (engine env config comes from this)."""
    params = {
        "atr_multiplier": defn["atr_multiplier"],
        "rr_ratio": defn["rr_ratio"],
        "min_confidence": defn["min_confidence"],
        "risk_per_trade": defn["risk_per_trade"],
        "entry_hours_bkk": defn["entry_hours_bkk"],
        "entry_hours_utc": bkk_hours_to_utc(defn["entry_hours_bkk"]),
        "blocked_hours_bkk": BLOCKED_HOURS_BKK,
        "blocked_hours_utc": bkk_hours_to_utc(BLOCKED_HOURS_BKK),
        **COMMON_RULES,
    }
    return params


def enroll_accounts(
    accounts: list,
    db_path,
    baseline_balance: float = 100.0,
    account_type: str = "demo",
) -> list:
    """Attach each account to its variant + baseline + running status.

    Returns the account names actually enrolled (skips accounts that don't
    exist in the accounts table yet or are already closed).
    """
    enrolled = []
    conn = get_connection(db_path)
    account_rows = {r[0]: r[1] for r in conn.execute("SELECT name, id FROM accounts")}
    existing_variant = {
        r[0] for r in conn.execute("SELECT name FROM accounts WHERE variant_id != ''")
    }
    conn.close()

    for defn in VARIANT_DEFS:
        for name in defn["accounts"]:
            if name not in accounts or name not in account_rows:
                continue
            if name in existing_variant:
                print(f"  {name}: variant already assigned — skipping (supersede manually if changing)")
                continue
            conn = get_connection(db_path)
            conn.execute(
                "UPDATE accounts SET variant_id = ?, account_type = ?, baseline_balance = ?, "
                "portfolio_status = 'running', peak_equity = ? WHERE name = ?",
                (defn["variant_id"], account_type, baseline_balance,
                 baseline_balance, name),
            )
            conn.commit()
            conn.close()
            log_portfolio_event(
                name, "enrolled",
                f"variant={defn['variant_id']} baseline={baseline_balance} type={account_type}",
                {"variant_id": defn["variant_id"], "baseline_balance": baseline_balance},
                db_path,
            )
            enrolled.append(name)
            print(f"  {name}: enrolled with {defn['variant_id']} (baseline ${baseline_balance})")
    return enrolled


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Seed portfolio variants + enroll P-accounts")
    parser.add_argument("--db-path", default=None, help="portfolio DB path (default: oracle.db)")
    parser.add_argument("--accounts", default="P1,P2,P3",
                        help="comma-separated account names to enroll (default P1,P2,P3)")
    parser.add_argument("--baseline-balance", type=float, default=100.0,
                        help="starting balance per account (default 100.0)")
    parser.add_argument("--account-type", default="demo", choices=["demo", "cent_real"],
                        help="demo for paper-parallel, cent_real for live phase")
    parser.add_argument("--enroll", action="store_true",
                        help="also enroll accounts (attach variant, set baseline)")
    args = parser.parse_args(argv)

    db_path = Path(args.db_path) if args.db_path else None
    accounts = [a.strip() for a in args.accounts.split(",") if a.strip()]

    print("Seeding variant definitions:")
    for defn in VARIANT_DEFS:
        insert_variant(defn["variant_id"], defn["label"], build_params(defn),
                       defn["sweet_spot_basis"], db_path)
        print(f"  {defn['variant_id']}: ATR {defn['atr_multiplier']} / RR {defn['rr_ratio']} / "
              f"conf {defn['min_confidence']} / risk {defn['risk_per_trade']*100:.2f}% / "
              f"hours BKK {defn['entry_hours_bkk']} -> UTC {bkk_hours_to_utc(defn['entry_hours_bkk'])}")

    if args.enroll:
        print(f"Enrolling accounts {accounts} (type={args.account_type}):")
        enrolled = enroll_accounts(accounts, db_path, args.baseline_balance, args.account_type)
        print(f"Enrolled: {enrolled or 'none'}")
    else:
        print("(dry run — pass --enroll to attach accounts)")

    # Emit the env snippet the docker-compose P-services will use
    print("\n# docker-compose.vps.yml env (per-account suffix pattern):")
    for defn in VARIANT_DEFS:
        for name in defn["accounts"]:
            p = build_params(defn)
            print(f"# {name} ({defn['variant_id']}):")
            print(f"#   ATR_MULTIPLIER_{name}={p['atr_multiplier']}")
            print(f"#   RR_RATIO_{name}={p['rr_ratio']}")
            print(f"#   MIN_CONFIDENCE_{name}={p['min_confidence']}")
            print(f"#   RISK_PER_TRADE_{name}={p['risk_per_trade']}")
            print(f"#   ENTRY_HOURS_{name}={','.join(str(h) for h in p['entry_hours_utc'])}")
            print(f"#   BLOCKED_HOURS_{name}={','.join(str(h) for h in p['blocked_hours_utc'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())