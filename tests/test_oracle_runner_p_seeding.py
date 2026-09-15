"""Tests for oracle_runner P-account seeding — self-contained startup.

The P-engine container must come up fully enrolled with ZERO extra deploy
steps: accounts row + variant definitions + variant_id/baseline/peak/status.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from metty.core.db import (
    get_account_id_by_name,
    get_account_portfolio_state,
    get_recent_portfolio_events,
    get_variant,
    init_db,
)
from scripts.generate_variants import VARIANT_DEFS
from scripts.oracle_runner import _seed_portfolio_accounts


@pytest.fixture()
def p_db(tmp_path):
    db_path = tmp_path / "oracle_p1.db"
    init_db(db_path)
    return db_path


class TestSeedPortfolioAccounts:
    def test_seeds_and_enrolls_p1(self, p_db, monkeypatch):
        """One call → P1 row + variant attached + baseline/peak + running."""
        monkeypatch.setenv("INITIAL_BALANCE_P1", "100")
        monkeypatch.setenv("MT5_BRIDGE_P1_HOST", "mt5p1")
        _seed_portfolio_accounts(["P1"], p_db)

        assert get_account_id_by_name("P1", p_db) is not None
        state = get_account_portfolio_state("P1", p_db)
        assert state["portfolio_status"] == "running"
        assert state["baseline_balance"] == 100.0
        assert state["peak_equity"] == 100.0
        assert state["variant_id"] == "P1-base-ny"
        assert state["account_type"] == "demo"

    def test_seeds_all_variant_definitions(self, p_db, monkeypatch):
        """The variants table is populated so any future P4/P5 expansion has
        its parameter set already on file."""
        monkeypatch.setenv("INITIAL_BALANCE_P1", "100")
        _seed_portfolio_accounts(["P1"], p_db)
        for defn in VARIANT_DEFS:
            v = get_variant(defn["variant_id"], p_db)
            assert v is not None, f"variant {defn['variant_id']} missing"
            assert "entry_hours_utc" in v["params"]

    def test_enrollment_audited(self, p_db, monkeypatch):
        monkeypatch.setenv("INITIAL_BALANCE_P1", "100")
        _seed_portfolio_accounts(["P1"], p_db)
        events = get_recent_portfolio_events("P1", db_path=p_db)
        assert any(e["event_type"] == "enrolled" for e in events)

    def test_idempotent_no_double_seed_no_supersede(self, p_db, monkeypatch):
        """Second startup after container restart must not re-enroll (Kappa #1:
        supersede requires explicit action) and must not fail."""
        monkeypatch.setenv("INITIAL_BALANCE_P1", "100")
        _seed_portfolio_accounts(["P1"], p_db)
        state1 = get_account_portfolio_state("P1", p_db)
        _seed_portfolio_accounts(["P1"], p_db)
        state2 = get_account_portfolio_state("P1", p_db)
        assert state1 == state2
        enrolled_events = [e for e in get_recent_portfolio_events("P1", db_path=p_db)
                           if e["event_type"] == "enrolled"]
        assert len(enrolled_events) == 1

    def test_legacy_accounts_untouched(self, p_db):
        """A-D in the list must be skipped entirely by P-seeding."""
        _seed_portfolio_accounts(["A", "B", "C", "D"], p_db)
        assert get_account_id_by_name("A", p_db) is None
        assert get_account_id_by_name("B", p_db) is None

    def test_account_type_env_respected(self, p_db, monkeypatch):
        """cent_real phase later only needs ACCOUNT_TYPE_P1=cent_real in .env."""
        monkeypatch.setenv("ACCOUNT_TYPE_P1", "cent_real")
        monkeypatch.setenv("INITIAL_BALANCE_P1", "100")
        _seed_portfolio_accounts(["P1"], p_db)
        assert get_account_portfolio_state("P1", p_db)["account_type"] == "cent_real"