"""Causal proof test: daily balance snapshot must capture per-variant equity.

ISSUE-096: Hermes' anomaly criterion is "-5% per day" but the farm only
stores trades — the criterion can't be checked. scripts/farm_balance_snapshot.py
writes one balance_snapshots row per account per UTC day.

Causal proof
------------
A farm DB with initial 100, one closed trade (+5) and one open BUY
(lot 0.01, entry 4000) marked at feed close 4100 → snapshot equity must
be exactly 205.00 using the SAME formula as the engine
(live_trader._paper_equity_from_db): (4100-4000)*0.01*100 = 100 → 100 + 5 + 100.
Controls: SELL mirror, idempotent re-run (1 row/account/day), no feed →
floating 0 (never fabricate a mark).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from metty.core.db import init_db, insert_account  # noqa: E402
from scripts.farm_balance_snapshot import snapshot_db  # noqa: E402

_DATE = "2026-10-02"


def _seed_farm_db(db: Path, direction: str = "BUY") -> None:
    init_db(db)
    insert_account(
        name="P11", balance=100.0, leverage=2000,
        bridge_host="none", bridge_port=8001, signal_group="portfolio",
        db_path=db,
    )
    import sqlite3
    conn = sqlite3.connect(str(db))
    with conn:
        conn.execute(
            "INSERT INTO accounts (name, balance, leverage, bridge_host, "
            "bridge_port, signal_group) VALUES ('P12', 100, 2000, 'none', "
            "8001, 'portfolio')"
        )
        # closed winner on P11: +5.00
        conn.execute(
            "INSERT INTO live_trades (account_id, timestamp, direction, "
            "entry_price, exit_price, lot_size, confidence, is_open, pnl) "
            "VALUES (1, '2026-10-02T10:00', 'BUY', 4000, 4005, 0.01, 0.65, 0, 5.0)"
        )
        # open position on P11: BUY lot 0.01 @ 4000 (mark 4100 → +100.00 = (4100-4000)*0.01*100)
        conn.execute(
            "INSERT INTO live_trades (account_id, timestamp, direction, "
            "entry_price, lot_size, confidence, is_open) "
            f"VALUES (1, '2026-10-02T11:00', '{direction}', 4000, 0.01, 0.65, 1)"
        )
    conn.close()


def _row(db: Path, account_id: int) -> dict:
    import sqlite3
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM balance_snapshots WHERE account_id = ? AND date = ?",
        (account_id, _DATE),
    ).fetchone()
    conn.close()
    assert row is not None, f"no snapshot row for account {account_id}"
    return dict(row)


class TestBalanceSnapshotCausal:
    """Snapshot = initial + closed + floating marked at feed close."""

    def test_equity_matches_engine_formula_buy(self, tmp_path):
        db = tmp_path / "farm.db"
        _seed_farm_db(db, direction="BUY")

        n = snapshot_db(db, mark=4100.0, date=_DATE)
        assert n == 2, "one row per account"

        p11 = _row(db, 1)
        assert p11["closed_pnl"] == 5.0
        assert p11["floating_pnl"] == 100.0, (
            f"BUY floating must be (4100-4000)*0.01*100 = 100, got {p11['floating_pnl']}"
        )
        assert p11["equity"] == 205.0, (
            f"equity must be 100 + 5 + 100 = 205, got {p11['equity']}"
        )
        assert p11["open_trades"] == 1

    def test_sell_floating_mirrored(self, tmp_path):
        # Control: SELL floating = (entry - mark)*lot*100 → -100 at mark 4100
        db = tmp_path / "farm.db"
        _seed_farm_db(db, direction="SELL")

        snapshot_db(db, mark=4100.0, date=_DATE)
        p11 = _row(db, 1)
        assert p11["floating_pnl"] == -100.0
        assert p11["equity"] == 5.0

    def test_idempotent_per_day(self, tmp_path):
        # Re-run same day → REPLACE, never duplicate rows
        db = tmp_path / "farm.db"
        _seed_farm_db(db)

        snapshot_db(db, mark=4100.0, date=_DATE)
        snapshot_db(db, mark=4150.0, date=_DATE)

        import sqlite3
        conn = sqlite3.connect(str(db))
        count = conn.execute(
            "SELECT COUNT(*) FROM balance_snapshots WHERE account_id = 1 "
            "AND date = ?", (_DATE,)
        ).fetchone()[0]
        conn.close()
        assert count == 1, f"one row per account/day, got {count}"

    def test_no_feed_means_no_floating(self, tmp_path):
        # Control: mark None (feed frozen) → floating 0, never fabricated
        db = tmp_path / "farm.db"
        _seed_farm_db(db)

        n = snapshot_db(db, mark=None, date=_DATE)
        assert n == 2
        p11 = _row(db, 1)
        assert p11["floating_pnl"] == 0.0
        assert p11["equity"] == 105.0