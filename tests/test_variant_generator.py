"""Tests for the portfolio variant generator (P-accounts).

Validates: UTC conversion, variant constraints from Hermes' requirements,
DB seeding, and account enrollment semantics.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from metty.core.db import (
    get_account_portfolio_state,
    get_recent_portfolio_events,
    get_variant,
    init_db,
    insert_account,
)
from scripts.generate_variants import (
    BLOCKED_HOURS_BKK,
    VARIANT_DEFS,
    bkk_hours_to_utc,
    build_params,
    enroll_accounts,
    main as generate_main,
)


@pytest.fixture()
def p_db(tmp_path):
    db_path = tmp_path / "oracle_portfolio.db"
    init_db(db_path)
    for i, name in enumerate(["P1", "P2", "P3"], start=1):
        insert_account(name=name, balance=100.0, leverage=2000,
                       bridge_host=f"mt5p{i}", bridge_port=8001,
                       signal_group="portfolio", db_path=db_path)
    return db_path


class TestBkkToUtc:
    def test_golden_hours_convert(self):
        # 01-03 BKK -> 18-20 UTC (previous day)
        assert bkk_hours_to_utc([1, 2, 3]) == [18, 19, 20]
        # 07-08 BKK -> 00-01 UTC (wraps midnight)
        assert bkk_hours_to_utc([7, 8]) == [0, 1]
        # 13 BKK -> 06 UTC, 15 BKK -> 08 UTC
        assert bkk_hours_to_utc([13, 15]) == [6, 8]

    def test_blocked_hours_convert(self):
        # 10-12, 16, 21 BKK -> 03-05, 09, 14 UTC
        assert bkk_hours_to_utc(BLOCKED_HOURS_BKK) == [3, 4, 5, 9, 14]

    def test_wrap_is_idempotent_sorted_unique(self):
        assert bkk_hours_to_utc([1, 1, 3, 3]) == [18, 20]
        assert bkk_hours_to_utc([7, 8, 1]) == [0, 1, 18]


class TestVariantConstraints:
    """Hermes' binding requirements on the variant set."""

    def test_every_variant_confidence_reachable(self):
        # Demo-D lesson: no threshold above 0.50
        for d in VARIANT_DEFS:
            assert d["min_confidence"] <= 0.50, f"{d['variant_id']} conf too high"

    def test_risk_within_half_to_one_percent(self):
        for d in VARIANT_DEFS:
            assert 0.005 <= d["risk_per_trade"] <= 0.010, f"{d['variant_id']} risk out of band"

    def test_atr_multiplier_varies_across_variants(self):
        # Requirement: each account walks its own path
        atrs = {d["atr_multiplier"] for d in VARIANT_DEFS}
        assert len(atrs) == len(VARIANT_DEFS), "ATR multipliers must all differ"

    def test_params_include_utc_hours_and_common_rules(self):
        p = build_params(VARIANT_DEFS[0])
        assert p["entry_hours_utc"] == [6, 8, 18, 19, 20]
        assert p["blocked_hours_utc"] == [3, 4, 5, 9, 14]
        assert p["cb_consecutive_losses"] == 3
        assert p["cb_pause_hours"] == 24
        assert p["freeze_dd_pct"] == 20.0
        assert p["ml_ensemble_threshold"] == 0.50  # Q3: same as Demo-D for comparison
        assert p["no_martingale"] is True

    def test_sl_distance_in_sweet_spot_band(self):
        """SL $10-15 corresponds to ATR multiplier 1.8-2.8 band (sweet spot)."""
        for d in VARIANT_DEFS:
            assert 1.8 <= d["atr_multiplier"] <= 2.8, f"{d['variant_id']} ATR out of sweet spot"

    def test_entry_hours_inside_golden_hours(self):
        golden = {1, 2, 3, 7, 8, 13, 15}
        for d in VARIANT_DEFS:
            assert set(d["entry_hours_bkk"]).issubset(golden), \
                f"{d['variant_id']} has off-golden-hour entries"


class TestSeeding:
    def test_main_seeds_all_five_variants(self, p_db, capsys):
        rc = generate_main(["--db-path", str(p_db)])
        assert rc == 0
        for d in VARIANT_DEFS:
            got = get_variant(d["variant_id"], p_db)
            assert got is not None
            assert got["params"]["atr_multiplier"] == d["atr_multiplier"]
            assert got["params"]["entry_hours_utc"] == bkk_hours_to_utc(d["entry_hours_bkk"])

    def test_main_is_idempotent(self, p_db):
        generate_main(["--db-path", str(p_db)])
        generate_main(["--db-path", str(p_db)])
        got = get_variant("P1-base-ny", p_db)
        assert got["params"]["atr_multiplier"] == 2.0


class TestEnrollment:
    def test_enroll_attaches_variant_baseline_and_status(self, p_db):
        generate_main(["--db-path", str(p_db)])
        enrolled = enroll_accounts(["P1", "P2", "P3"], p_db, 100.0, "demo")
        assert sorted(enrolled) == ["P1", "P2", "P3"]

        state = get_account_portfolio_state("P1", p_db)
        assert state["variant_id"] == "P1-base-ny"
        assert state["account_type"] == "demo"
        assert state["baseline_balance"] == 100.0
        assert state["peak_equity"] == 100.0
        assert state["portfolio_status"] == "running"

    def test_enroll_writes_audit_event(self, p_db):
        generate_main(["--db-path", str(p_db)])
        enroll_accounts(["P1"], p_db, 100.0, "demo")
        events = get_recent_portfolio_events("P1", 5, p_db)
        assert any(e["event_type"] == "enrolled" and "P1-base-ny" in e["reason"]
                   for e in events)

    def test_enroll_skips_unknown_account(self, p_db):
        generate_main(["--db-path", str(p_db)])
        enrolled = enroll_accounts(["P1", "PX"], p_db, 100.0, "demo")
        assert enrolled == ["P1"]

    def test_enroll_does_not_supersede_existing_assignment(self, p_db):
        # Kappa #1: assignment changes are explicit, never silent
        generate_main(["--db-path", str(p_db)])
        enroll_accounts(["P1"], p_db, 100.0, "demo")
        enrolled = enroll_accounts(["P1"], p_db, 999.0, "demo")
        assert enrolled == []
        state = get_account_portfolio_state("P1", p_db)
        assert state["baseline_balance"] == 100.0

    def test_main_with_enroll_flag(self, p_db, capsys):
        rc = generate_main(["--db-path", str(p_db), "--enroll",
                            "--accounts", "P1,P2,P3", "--account-type", "demo"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Enrolled: ['P1', 'P2', 'P3']" in out
        for name in ["P1", "P2", "P3"]:
            assert get_account_portfolio_state(name, p_db)["portfolio_status"] == "running"