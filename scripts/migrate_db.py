#!/usr/bin/env python3
"""Database migration framework for god-port-oracle.

Provides versioned migrations with rollback support instead of
ad-hoc SQL over SSH. Each migration is a Python function that
can apply or rollback schema changes.

Usage:
    python scripts/migrate_db.py              # Run all pending migrations
    python scripts/migrate_db.py --status    # Show migration status
    python scripts/migrate_db.py --rollback N # Rollback N most recent migrations
    python scripts/migrate_db.py --check     # Verify data integrity after migrations
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Default DB path
DEFAULT_DB = Path(__file__).parent.parent / "data" / "oracle.db"

# ─── Migration Registry ───────────────────────────────────────────────

# Each migration is (version, description, forward_sql, rollback_sql)
# forward_sql can be a list of statements or a single string
MIGRATIONS: list[tuple[int, str, str | list[str], str | list[str]]] = [
    (
        1,
        "Add trading_mode and strategy_id columns to live_trades",
        [
            "ALTER TABLE live_trades ADD COLUMN trading_mode TEXT NOT NULL DEFAULT 'swing'",
            "ALTER TABLE live_trades ADD COLUMN strategy_id TEXT NOT NULL DEFAULT ''",
        ],
        [
            # SQLite doesn't support DROP COLUMN before 3.35.0
            # Rollback: recreate table without these columns (manual)
            "SELECT 1 -- Cannot rollback: SQLite < 3.35 does not support DROP COLUMN",
        ],
    ),
    (
        2,
        "Add signal_id column to live_trades",
        "ALTER TABLE live_trades ADD COLUMN signal_id INTEGER",
        "SELECT 1 -- Cannot rollback: DROP COLUMN not supported",
    ),
    (
        3,
        "Add MFE/MAE columns to live_trades",
        [
            "ALTER TABLE live_trades ADD COLUMN mfe REAL",
            "ALTER TABLE live_trades ADD COLUMN mae REAL",
            "ALTER TABLE live_trades ADD COLUMN mfe_pct REAL",
            "ALTER TABLE live_trades ADD COLUMN mae_pct REAL",
        ],
        "SELECT 1 -- Cannot rollback: DROP COLUMN not supported",
    ),
    (
        4,
        "Add exit context columns to live_trades",
        [
            "ALTER TABLE live_trades ADD COLUMN exit_regime TEXT",
            "ALTER TABLE live_trades ADD COLUMN exit_d1_trend TEXT",
            "ALTER TABLE live_trades ADD COLUMN exit_h4_trend TEXT",
        ],
        "SELECT 1 -- Cannot rollback: DROP COLUMN not supported",
    ),
    (
        5,
        "Add pnl/pnl_pct columns to trade_outcomes",
        [
            "ALTER TABLE trade_outcomes ADD COLUMN pnl REAL",
            "ALTER TABLE trade_outcomes ADD COLUMN pnl_pct REAL",
        ],
        "SELECT 1 -- Cannot rollback: DROP COLUMN not supported",
    ),
    (
        6,
        "Add partial TP columns to live_trades",
        [
            "ALTER TABLE live_trades ADD COLUMN tp1_price REAL",
            "ALTER TABLE live_trades ADD COLUMN parent_trade_id INTEGER",
            "ALTER TABLE live_trades ADD COLUMN tp_level INTEGER DEFAULT 0",
            "ALTER TABLE live_trades ADD COLUMN remaining_lots REAL",
        ],
        "SELECT 1 -- Cannot rollback: DROP COLUMN not supported",
    ),
    (
        7,
        "Add trading parameter columns to live_trades and trade_outcomes",
        [
            "ALTER TABLE live_trades ADD COLUMN atr_multiplier REAL",
            "ALTER TABLE live_trades ADD COLUMN rr_ratio REAL",
            "ALTER TABLE live_trades ADD COLUMN min_confidence_threshold REAL",
            "ALTER TABLE trade_outcomes ADD COLUMN atr_multiplier REAL",
            "ALTER TABLE trade_outcomes ADD COLUMN rr_ratio REAL",
            "ALTER TABLE trade_outcomes ADD COLUMN min_confidence_threshold REAL",
        ],
        "SELECT 1 -- Cannot rollback: DROP COLUMN not supported",
    ),
]


def get_connection(db_path: Path) -> sqlite3.Connection:
    """Connect to the database and ensure migration tracking table exists."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def ensure_migration_table(conn: sqlite3.Connection) -> None:
    """Create the migration tracking table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _migrations (
            version INTEGER PRIMARY KEY,
            description TEXT NOT NULL,
            applied_at TEXT NOT NULL,
            rolled_back_at TEXT
        )
    """)
    conn.commit()


def get_applied_versions(conn: sqlite3.Connection) -> set[int]:
    """Get set of migration versions that have been applied (not rolled back)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _migrations (
            version INTEGER PRIMARY KEY,
            description TEXT NOT NULL,
            applied_at TEXT NOT NULL,
            rolled_back_at TEXT
        )
    """)
    rows = conn.execute(
        "SELECT version FROM _migrations WHERE rolled_back_at IS NULL"
    ).fetchall()
    return {r[0] for r in rows}


def run_migrations(db_path: Path, dry_run: bool = False) -> list[int]:
    """Run all pending migrations. Returns list of versions applied."""
    conn = get_connection(db_path)
    try:
        ensure_migration_table(conn)
        applied = get_applied_versions(conn)
        to_apply = [(v, desc, sql) for v, desc, sql, _ in MIGRATIONS if v not in applied]

        if not to_apply:
            logger.info("No pending migrations")
            return []

        applied_versions = []
        for version, desc, sql in to_apply:
            logger.info("Applying migration %d: %s", version, desc)
            if not dry_run:
                statements = sql if isinstance(sql, list) else [sql]
                for stmt in statements:
                    try:
                        conn.execute(stmt)
                    except sqlite3.OperationalError as e:
                        if "duplicate column name" in str(e).lower():
                            logger.info("  Column already exists, skipping: %s", stmt[:60])
                        else:
                            raise
                conn.execute(
                    "INSERT INTO _migrations (version, description, applied_at) VALUES (?, ?, ?)",
                    (version, desc, datetime.now(timezone.utc).isoformat()),
                )
                conn.commit()
            applied_versions.append(version)
            logger.info("  ✓ Migration %d applied", version)

        return applied_versions
    finally:
        conn.close()


def rollback_migrations(db_path: Path, count: int = 1, dry_run: bool = False) -> list[int]:
    """Rollback the N most recent migrations. Returns list of versions rolled back.

    Note: SQLite < 3.35 does not support DROP COLUMN, so most rollbacks
    are no-ops (the migration is marked as rolled back but columns remain).
    """
    conn = get_connection(db_path)
    try:
        ensure_migration_table(conn)
        applied = get_applied_versions(conn)

        if not applied:
            logger.info("No migrations to rollback")
            return []

        # Get the most recent N applied versions
        to_rollback = sorted(applied, reverse=True)[:count]
        rolled_back = []

        for version in to_rollback:
            # Find migration info
            migration = next((m for m in MIGRATIONS if m[0] == version), None)
            if not migration:
                logger.warning("Migration %d not found in registry, skipping", version)
                continue

            _, desc, _, rollback_sql = migration
            logger.info("Rolling back migration %d: %s", version, desc)

            if not dry_run:
                statements = rollback_sql if isinstance(rollback_sql, list) else [rollback_sql]
                for stmt in statements:
                    if "Cannot rollback" in stmt or stmt.strip() == "SELECT 1":
                        logger.info("  ⚠ No-op rollback (SQLite < 3.35 limitation)")
                        continue
                    try:
                        conn.execute(stmt)
                    except sqlite3.OperationalError as e:
                        logger.warning("  Rollback statement failed: %s", e)

                conn.execute(
                    "UPDATE _migrations SET rolled_back_at = ? WHERE version = ?",
                    (datetime.utcnow().isoformat(), version),
                )
                conn.commit()

            rolled_back.append(version)
            logger.info("  ✓ Migration %d rolled back (marked)", version)

        return rolled_back
    finally:
        conn.close()


def check_integrity(db_path: Path) -> dict:
    """Run data integrity checks and return results."""
    conn = get_connection(db_path)
    try:
        results = {}

        # Check live_trades trading params
        lt_total = conn.execute("SELECT COUNT(*) FROM live_trades").fetchone()[0]
        lt_null_atr = conn.execute("SELECT COUNT(*) FROM live_trades WHERE atr_multiplier IS NULL").fetchone()[0]
        lt_null_rr = conn.execute("SELECT COUNT(*) FROM live_trades WHERE rr_ratio IS NULL").fetchone()[0]
        lt_null_conf = conn.execute("SELECT COUNT(*) FROM live_trades WHERE min_confidence_threshold IS NULL").fetchone()[0]

        results["live_trades"] = {
            "total": lt_total,
            "null_atr_multiplier": lt_null_atr,
            "null_rr_ratio": lt_null_rr,
            "null_min_confidence_threshold": lt_null_conf,
            "healthy": lt_null_atr == 0 and lt_null_rr == 0 and lt_null_conf == 0,
        }

        # Check trade_outcomes trading params
        to_total = conn.execute("SELECT COUNT(*) FROM trade_outcomes").fetchone()[0]
        to_null_atr = conn.execute("SELECT COUNT(*) FROM trade_outcomes WHERE atr_multiplier IS NULL").fetchone()[0]
        to_null_rr = conn.execute("SELECT COUNT(*) FROM trade_outcomes WHERE rr_ratio IS NULL").fetchone()[0]
        to_null_conf = conn.execute("SELECT COUNT(*) FROM trade_outcomes WHERE min_confidence_threshold IS NULL").fetchone()[0]

        results["trade_outcomes"] = {
            "total": to_total,
            "null_atr_multiplier": to_null_atr,
            "null_rr_ratio": to_null_rr,
            "null_min_confidence_threshold": to_null_conf,
            "healthy": to_null_atr == 0 and to_null_rr == 0 and to_null_conf == 0,
        }

        results["healthy"] = results["live_trades"]["healthy"] and results["trade_outcomes"]["healthy"]
        return results
    finally:
        conn.close()


def show_status(db_path: Path) -> None:
    """Show migration status."""
    conn = get_connection(db_path)
    try:
        ensure_migration_table(conn)
        applied = get_applied_versions(conn)
        all_versions = {m[0] for m in MIGRATIONS}

        logger.info("Migration Status:")
        logger.info("=" * 60)

        for version, desc, _, _ in sorted(MIGRATIONS, key=lambda m: m[0]):
            status = "✓ Applied" if version in applied else "○ Pending"
            logger.info("  [%s] v%d: %s", status, version, desc)

        logger.info("=" * 60)
        logger.info("Applied: %d/%d", len(applied & all_versions), len(MIGRATIONS))
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Database migration tool")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="Path to SQLite database")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without making changes")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("status", help="Show migration status")
    subparsers.add_parser("migrate", help="Run pending migrations")
    subparsers.add_parser("check", help="Run data integrity checks")

    rollback_parser = subparsers.add_parser("rollback", help="Rollback recent migrations")
    rollback_parser.add_argument("--count", type=int, default=1, help="Number of migrations to rollback")

    args = parser.parse_args()

    if args.command == "status" or args.command is None:
        show_status(args.db)
    elif args.command == "migrate":
        applied = run_migrations(args.db, dry_run=args.dry_run)
        if applied:
            logger.info("Applied %d migrations", len(applied))
            # Run integrity check after migrations
            try:
                results = check_integrity(args.db)
                if results.get("healthy"):
                    logger.info("Data integrity check: PASSED")
                else:
                    logger.warning("Data integrity check: FAILED — %s", results)
            except Exception as e:
                logger.warning("Could not run integrity check: %s", e)
    elif args.command == "rollback":
        rolled_back = rollback_migrations(args.db, count=args.count, dry_run=args.dry_run)
        if rolled_back:
            logger.info("Rolled back %d migrations", len(rolled_back))
    elif args.command == "check":
        results = check_integrity(args.db)
        print(json.dumps(results, indent=2))
        if not results.get("healthy"):
            logger.warning("Data integrity check FAILED")


if __name__ == "__main__":
    main()