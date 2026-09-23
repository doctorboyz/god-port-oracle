#!/usr/bin/env python3
"""DB schema preflight — print real column names before writing SQL.

ISSUE-076 (2026-09-23): guessed column names cost debugging time twice
(`accounts.equity` doesn't exist — it's `balance`; `live_trades.lot_size`
not `lots`; `accounts.id` not `account_id`). Read the schema with this
BEFORE writing any ad-hoc SQL against oracle.db / oracle_train.db.

Local:
    python3 scripts/db_schema.py --db data/oracle.db

On the VPS (inside the engine container):
    docker cp scripts/db_schema.py oracle-engine-train:/tmp/db_schema.py
    docker exec oracle-engine-train python /tmp/db_schema.py --db /app/data/oracle_train.db

See also ψ/memory/learnings/2026-06-28_db-schema-reference.md.
"""

import argparse
import sqlite3
import sys

# Tables worth dumping by default (trading-loop queries hit these).
DEFAULT_TABLES = ["accounts", "live_trades", "rejected_signals", "signals", "trade_outcomes"]


def dump_schema(db_path: str, tables: list[str], rows: int = 0) -> int:
    conn = sqlite3.connect(db_path)
    try:
        all_tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        print(f"db: {db_path}")
        print(f"tables ({len(all_tables)}): {', '.join(all_tables)}\n")

        targets = tables or [t for t in DEFAULT_TABLES if t in all_tables]
        for t in targets:
            if t not in all_tables:
                print(f"[{t}] ❌ not in this db")
                continue
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
            print(f"[{t}] ({len(cols)} cols):")
            print(f"    {', '.join(cols)}")
            if rows > 0:
                count = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                print(f"    rows: {count}")
            print()
    finally:
        conn.close()
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/oracle.db",
                        help="path to the SQLite db (default data/oracle.db)")
    parser.add_argument("--tables", default="",
                        help="comma-separated table names (default: the trading-loop tables)")
    parser.add_argument("--rows", action="store_true",
                        help="also print row counts")
    args = parser.parse_args()

    tables = [t.strip() for t in args.tables.split(",") if t.strip()]
    sys.exit(dump_schema(args.db, tables, rows=1 if args.rows else 0))


if __name__ == "__main__":
    main()