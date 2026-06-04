#!/usr/bin/env python3
"""Backfill d1_trend, h4_trend, session into trade_outcomes.features_json.

Problem: features_json in trade_outcomes was missing d1_trend, h4_trend, session
because the original backfill script stripped metadata keys. This causes train/serve
skew — the model trains without these features but receives them at prediction time.

Solution: JOIN trade_outcomes with live_trades via trade_id, inject d1_trend,
h4_trend, and session from live_trades into features_json.

Usage:
    python scripts/backfill_trend_features.py [--db-path PATH] [--dry-run]
"""

import json
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB = Path("data/oracle-vps.db")


def backfill_trend_features(db_path: Path = DEFAULT_DB, dry_run: bool = False) -> int:
    """Inject d1_trend, h4_trend, session from live_trades into trade_outcomes.features_json.

    Returns the number of rows updated.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Check which columns live_trades has
    live_cols = {r[1] for r in conn.execute("PRAGMA table_info(live_trades)").fetchall()}
    has_h4_trend = "h4_trend" in live_cols

    # Build query based on available columns
    h4_col = "lt.h4_trend" if has_h4_trend else "NULL as h4_trend"

    # Find trade_outcomes that can be joined with live_trades
    rows = conn.execute(f"""
        SELECT to_.id, to_.features_json, lt.d1_trend, {h4_col}, lt.session, lt.regime
        FROM trade_outcomes to_
        INNER JOIN live_trades lt ON lt.id = to_.trade_id
        WHERE to_.features_json IS NOT NULL
    """).fetchall()

    if not rows:
        print("No trade_outcomes rows to backfill")
        conn.close()
        return 0

    updated = 0
    already_had = 0
    skipped = 0

    for row in rows:
        features = json.loads(row["features_json"])

        d1_trend = row["d1_trend"]
        h4_trend = row["h4_trend"]
        session = row["session"]
        regime = row["regime"]

        # Skip if features already have these keys with non-null values
        has_d1 = features.get("d1_trend") not in (None, "unknown", "")
        has_h4 = features.get("h4_trend") not in (None, "unknown", "")
        has_session = features.get("session") not in (None, "unknown", "")

        # Track what we're adding
        changes = []
        if d1_trend and d1_trend not in ("unknown", "") and not has_d1:
            features["d1_trend"] = d1_trend
            changes.append(f"d1_trend={d1_trend}")
        if h4_trend and h4_trend not in ("unknown", "") and not has_h4:
            features["h4_trend"] = h4_trend
            changes.append(f"h4_trend={h4_trend}")
        if session and session not in ("unknown", "") and not has_session:
            features["session"] = session
            changes.append(f"session={session}")
        if regime and regime not in ("unknown", "") and features.get("regime") in (None, "unknown", ""):
            features["regime"] = regime
            changes.append(f"regime={regime}")

        if not changes:
            already_had += 1
            continue

        # Update features_json
        new_json = json.dumps(features, separators=(",", ":"))

        if not dry_run:
            conn.execute(
                "UPDATE trade_outcomes SET features_json = ? WHERE id = ?",
                (new_json, row["id"]),
            )
        updated += 1
        if updated <= 10 or updated % 200 == 0:
            print(f"  id={row['id']}: added {', '.join(changes)}")

    if not dry_run:
        conn.commit()
        print(f"\n✓ Committed {updated} updates")
    else:
        print(f"\n⊘ Dry run — would update {updated} rows")

    print(f"  Updated: {updated}")
    print(f"  Already had features: {already_had}")
    print(f"  Total checked: {len(rows)}")

    # Show resulting d1_trend distribution
    print("\n--- d1_trend distribution after backfill ---")
    dist = conn.execute("""
        SELECT d1_trend, direction, COUNT(*),
               AVG(CASE WHEN profit > 0 THEN 1.0 ELSE 0.0 END) as win_rate
        FROM (
            SELECT to_.id, to_.direction, to_.profit,
                   lt.d1_trend
            FROM trade_outcomes to_
            INNER JOIN live_trades lt ON lt.id = to_.trade_id
            WHERE to_.features_json IS NOT NULL
        )
        WHERE d1_trend IS NOT NULL AND d1_trend != 'unknown'
        GROUP BY d1_trend, direction
        ORDER BY d1_trend, direction
    """).fetchall()
    print(f"{'D1 Trend':<12} {'Direction':<10} {'Count':>8} {'WR':>8}")
    for r in dist:
        print(f"{r[0]:<12} {r[1]:<10} {r[2]:>8} {r[3]:>8.1%}")

    conn.close()
    return updated


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Backfill trend features into trade_outcomes")
    parser.add_argument("--db-path", type=str, default=str(DEFAULT_DB), help="Path to SQLite database")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without committing")
    args = parser.parse_args()

    db_path = Path(args.db_path)
    if not db_path.exists():
        print(f"Error: database not found at {db_path}")
        sys.exit(1)

    print(f"Backfilling trend features into {db_path}")
    if args.dry_run:
        print("(DRY RUN — no changes will be committed)")

    count = backfill_trend_features(db_path, dry_run=args.dry_run)
    print(f"\nDone. {'Would update' if args.dry_run else 'Updated'} {count} rows")