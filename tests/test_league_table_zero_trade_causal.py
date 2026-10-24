"""Causal proof test: league table must show freshly seeded variants (2026-10-24).

Hypothesis
----------
league_table.collect() only creates a stats entry for an account when it
has a closed or open trade. The axis-2 expansion (P23 + farm2 P31-P33)
means up to 4 new variants hold ZERO trades for their first days — a
league table built from trade rows alone shows 12 rows, and Hermes
cannot tell "deployed and quiet" from "not deployed at all". The 16-variant
criterion (ada-task-20261024 T5) is unreachable without counting the
accounts table itself.

Causal proof
------------
A farm DB whose accounts exist but hold no trades must yield one
zero-trade row per account. Controls: closed trades still aggregate
exactly as before; the accounts table, not the trades table, is the
source of the zero rows.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import league_table as lt  # noqa: E402


def _make_farm_db(path: Path, accounts: list[str],
                  closed_pnls: dict[str, list[float]] | None = None) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT, balance REAL);
            CREATE TABLE live_trades (
                id INTEGER PRIMARY KEY, account_id INTEGER, strategy_id TEXT,
                pnl REAL, exit_time TEXT, timestamp TEXT, is_open INTEGER
            );
            """
        )
        for i, name in enumerate(accounts, start=1):
            conn.execute("INSERT INTO accounts VALUES (?, ?, 100.0)", (i, name))
        for name, pnls in (closed_pnls or {}).items():
            acc_id = accounts.index(name) + 1
            for j, pnl in enumerate(pnls):
                conn.execute(
                    "INSERT INTO live_trades VALUES (?, ?, ?, ?, ?, ?, 0)",
                    (j + 1, acc_id, f"s-{name}", pnl, f"2026-10-24 0{j}:00:00",
                     "2026-10-24 00:00:00"),
                )
        conn.commit()
    finally:
        conn.close()


class TestLeagueTableZeroTradeCausal:

    def test_zero_trade_variants_visible(self, tmp_path):
        # RED before the fix: farm2 seeds P31-P33 with no trades → collect()
        # returned {} and the axis-2 league table silently showed 12 rows
        db = tmp_path / "farm2.db"
        _make_farm_db(db, ["P31", "P32", "P33"])

        stats = lt.collect(db)

        assert set(stats) == {"P31", "P32", "P33"}, (
            "seeded-but-tradeless variants must appear in the league table "
            "with zero trades — otherwise 'deployed and quiet' is "
            "indistinguishable from 'not deployed'"
        )
        for s in stats.values():
            assert s["trades"] == 0 and s["pnl_sum"] == 0.0

    def test_tradeless_db_renders_rows(self, tmp_path):
        db = tmp_path / "farm2.db"
        _make_farm_db(db, ["P31"])

        report = lt.render(lt.collect(db), [db])

        assert "| P31 |" in report, (
            "the rendered table must carry the zero-trade row, not drop it"
        )

    def test_closed_trades_still_aggregate(self, tmp_path):
        # Control: the aggregation path must not change — same numbers in,
        # same stats out, with the accounts pre-seed in place
        db = tmp_path / "farm1.db"
        _make_farm_db(db, ["P11"], {"P11": [5.0, -2.0, 3.0]})

        stats = lt.collect(db)

        s = stats["P11"]
        assert s["trades"] == 3 and s["wins"] == 2
        assert abs(s["pnl_sum"] - 6.0) < 1e-9
        assert s["open"] == 0

    def test_zero_trade_row_survives_merge_with_traded_db(self, tmp_path):
        # The real shape after deploy: farm1 (13 traded/seeded accounts) +
        # farm2 (3 seeded) merged in main()'s stats.update — no clobbering
        db1 = tmp_path / "farm1.db"
        db2 = tmp_path / "farm2.db"
        _make_farm_db(db1, ["P11"], {"P11": [5.0]})
        _make_farm_db(db2, ["P31", "P32", "P33"])

        merged: dict = {}
        for p in (db1, db2):
            merged.update(lt.collect(p))

        assert set(merged) == {"P11", "P31", "P32", "P33"}
        assert merged["P11"]["trades"] == 1
        assert merged["P31"]["trades"] == 0