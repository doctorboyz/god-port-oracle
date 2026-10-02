#!/usr/bin/env python3
"""Daily balance snapshot for the paper farm — ISSUE-096.

Hermes' anomaly criterion is "-5% per day" but the farm DB only stores
trades, not balances — the criterion can't actually be checked from
league_table output. This script writes one snapshot row per variant per
UTC day into each farm DB:

    balance_snapshots(account_id, date, closed_pnl, floating_pnl,
                      equity, open_trades, created_at)

Equity uses the SAME formula as the engine's paper fallback
(live_trader._paper_equity_from_db): initial (accounts.balance) +
closed pnl + floating of open positions marked at the last M5 close of
the feed CSV — BUY (mark-entry)*lot*100, SELL (entry-mark)*lot*100
(CONTRACT_SIZE = 100 oz). Never fabricate a mark: no feed → floating 0.

Idempotent per (account, date): INSERT OR REPLACE — launchd can re-run
it freely. Run daily via launchd (com.godport.farm-snapshot).

Usage:
    python3 scripts/farm_balance_snapshot.py            # all farm DBs
    python3 scripts/farm_balance_snapshot.py db1.db    # explicit DBs
"""

from __future__ import annotations

import csv
import glob
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FEED_M5 = ROOT / "data" / "paper-feed" / "XAUUSD_M5.csv"
CONTRACT_SIZE = 100  # XAUUSD: 1 lot = 100 oz — same as live_trader

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS balance_snapshots (
    account_id INTEGER NOT NULL,
    date TEXT NOT NULL,           -- UTC YYYY-MM-DD
    closed_pnl REAL NOT NULL,
    floating_pnl REAL NOT NULL,
    equity REAL NOT NULL,
    open_trades INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (account_id, date)
)
"""


def _db_paths(args: list[str]) -> list[Path]:
    """Live farm DBs only — skip data/farm/backup/ (same as league_table)."""
    if args:
        return [Path(a) for a in args]
    found = sorted(glob.glob(str(ROOT / "data" / "farm" / "*" / "*.db")))
    return [Path(p) for p in found if Path(p).parent.name != "backup"]


def _last_close(feed_csv: Path) -> float | None:
    """Last close of the feed's newest M5 bar (premium format, no header).

    Never fabricated: missing/short file → None → floating counted as 0.
    """
    try:
        with open(feed_csv, newline="") as fh:
            rows = list(csv.reader(fh))
        for row in reversed(rows):
            if len(row) >= 5 and row[4]:
                return float(row[4])
    except (OSError, ValueError):
        pass
    return None


def snapshot_db(db_path: Path, mark: float | None, date: str) -> int:
    """Write one row per account for `date`. Returns rows written."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(_CREATE_TABLE)
        has_trades = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='live_trades'"
        ).fetchone()
        if not has_trades:
            return 0  # not a farm DB — nothing to do
        accounts = conn.execute(
            "SELECT id, name, balance FROM accounts ORDER BY name"
        ).fetchall()
        now = datetime.now(timezone.utc).isoformat()
        written = 0
        for acc in accounts:
            closed = conn.execute(
                "SELECT COALESCE(SUM(pnl), 0.0) FROM live_trades "
                "WHERE account_id = ? AND is_open = 0 AND pnl IS NOT NULL",
                (acc["id"],),
            ).fetchone()[0]
            floating = 0.0
            open_rows = conn.execute(
                "SELECT direction, entry_price, lot_size FROM live_trades "
                "WHERE account_id = ? AND is_open = 1",
                (acc["id"],),
            ).fetchall()
            for t in open_rows:
                lot = float(t["lot_size"] or 0.0)
                entry = float(t["entry_price"] or 0.0)
                if mark is None or lot <= 0 or entry <= 0:
                    continue
                # Same formula as _paper_equity_from_db / _monitor_positions
                if (t["direction"] or "").upper() == "BUY":
                    floating += (mark - entry) * lot * CONTRACT_SIZE
                else:
                    floating += (entry - mark) * lot * CONTRACT_SIZE
            equity = float(acc["balance"]) + float(closed) + floating
            conn.execute(
                "INSERT OR REPLACE INTO balance_snapshots "
                "(account_id, date, closed_pnl, floating_pnl, equity, "
                " open_trades, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (acc["id"], date, round(float(closed), 2), round(floating, 2),
                 round(equity, 2), len(open_rows), now),
            )
            written += 1
        conn.commit()
    finally:
        conn.close()
    return written


def main() -> None:
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    mark = _last_close(FEED_M5)
    total = 0
    for db_path in _db_paths(sys.argv[1:]):
        n = snapshot_db(db_path, mark, date)
        print(f"{db_path.name}: {n} snapshot rows (mark={mark})")
        total += n
    print(f"Done — {total} rows for {date}")


if __name__ == "__main__":
    main()