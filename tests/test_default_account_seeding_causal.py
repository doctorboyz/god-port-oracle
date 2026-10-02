"""Causal proof test: farm must not re-seed A/B/C demo accounts on restart.

Hypothesis
----------
oracle_runner seeds demo A/B/C into whatever DB it starts against,
unconditionally, on EVERY boot. The paper farm DBs hold only P11-P20 —
we deleted the legacy A/B/C rows once and the 10:00 UTC 2026-10-02
redeploy silently re-created them (new ids 14-16, caught by the new
balance-snapshot script). Deleting rows is futile while the seeder runs
every restart; the farm must be able to opt out.

Fix: SEED_DEFAULT_ACCOUNTS=0 skips _seed_default_accounts entirely.
Unset/1 → legacy seeding (VPS behavior unchanged).

Causal proof
------------
Fresh DB + SEED_DEFAULT_ACCOUNTS=0 → A/B/C absent after seeding call.
Control: env unset → A/B/C present with the legacy balances
(100/500/1000) — live/VPS path preserved.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from metty.core.db import init_db  # noqa: E402
from scripts.oracle_runner import _seed_default_accounts  # noqa: E402


def _accounts(db: Path) -> set[str]:
    import sqlite3
    conn = sqlite3.connect(str(db))
    names = {r[0] for r in conn.execute("SELECT name FROM accounts")}
    conn.close()
    return names


class TestDefaultAccountSeedingCausal:
    """Farm opt-out must stop the A/B/C zombie; legacy path unchanged."""

    def test_farm_env_skips_seeding(self, tmp_path, monkeypatch):
        db = tmp_path / "farm.db"
        init_db(db)
        monkeypatch.setenv("SEED_DEFAULT_ACCOUNTS", "0")

        _seed_default_accounts(db)

        names = _accounts(db)
        assert not ({"A", "B", "C"} & names), (
            f"SEED_DEFAULT_ACCOUNTS=0 must not seed A/B/C, found {sorted(names)}"
        )

    def test_env_unset_seeds_legacy(self, tmp_path, monkeypatch):
        # Control: no env → legacy behavior (VPS relies on this)
        db = tmp_path / "oracle.db"
        init_db(db)
        monkeypatch.delenv("SEED_DEFAULT_ACCOUNTS", raising=False)

        _seed_default_accounts(db)

        names = _accounts(db)
        assert {"A", "B", "C"} <= names, (
            f"unset env must keep legacy A/B/C seeding, got {sorted(names)}"
        )
        import sqlite3
        conn = sqlite3.connect(str(db))
        balances = dict(conn.execute("SELECT name, balance FROM accounts"))
        conn.close()
        assert balances["A"] == 100.0
        assert balances["B"] == 500.0
        assert balances["C"] == 1000.0