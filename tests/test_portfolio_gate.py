"""Causal tests for portfolio gates: entry-hour window + freeze/cooldown.

Each test exercises the causal mechanism directly:
- hour gate blocks BECAUSE the env window says so (remove the env → passes)
- portfolio gate blocks BECAUSE the DB state says so (clear the state → passes)
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from metty.core.db import (
    get_account_portfolio_state,
    init_db,
    insert_account,
    log_portfolio_event,
    set_cooldown,
    set_portfolio_status,
    update_peak_equity,
)
from metty.core.portfolio_gate import (
    entry_hour_gate,
    parse_hour_list,
    portfolio_gate,
)


@pytest.fixture()
def p_db(tmp_path):
    db_path = tmp_path / "oracle_p1.db"
    init_db(db_path)
    insert_account(name="P1", balance=100.0, leverage=2000,
                   bridge_host="mt5p1", bridge_port=8001,
                   signal_group="portfolio", db_path=db_path)
    return db_path


def _utc(hour: int, day: int = 16) -> datetime:
    return datetime(2026, 9, day, hour, 0, tzinfo=timezone.utc)


class TestParseHourList:
    def test_parses_comma_separated(self):
        assert parse_hour_list("18,19,20,6,8") == {18, 19, 20, 6, 8}

    def test_empty_and_none(self):
        assert parse_hour_list("") == set()
        assert parse_hour_list(None) == set()

    def test_rejects_out_of_range(self):
        with pytest.raises(ValueError):
            parse_hour_list("24")
        with pytest.raises(ValueError):
            parse_hour_list("-1")


class TestEntryHourGate:
    """Causal: the ENV WINDOW causes the block — same time without env passes."""

    # P1 golden hours BKK 01-03,13,15 = UTC 18,19,20,6,8
    P1_ENV = {"ENTRY_HOURS_P1": "6,8,18,19,20", "BLOCKED_HOURS_P1": "3,4,5,9,14"}

    def test_blocked_at_negative_ev_hour(self):
        # 10:00 BKK = 03:00 UTC — Hermes says avoid
        allowed, reason = entry_hour_gate(_utc(3), "P1", self.P1_ENV)
        assert not allowed
        assert "blocked hour 03" in reason

    def test_blocked_outside_window(self):
        # 12:00 UTC = 19:00 BKK — neither golden nor in the blocked list,
        # but outside the configured window → blocked by ENTRY_HOURS
        allowed, reason = entry_hour_gate(_utc(12), "P1", self.P1_ENV)
        assert not allowed
        assert "outside entry window" in reason

    def test_allowed_inside_golden_hours(self):
        for hour in [6, 8, 18, 19, 20]:
            allowed, reason = entry_hour_gate(_utc(hour), "P1", self.P1_ENV)
            assert allowed, f"hour {hour} should be allowed: {reason}"

    def test_causal_no_env_no_block(self):
        """Same blocked time, no env config → allowed. Proves the env window
        is what causes the block, not the time itself."""
        allowed, _ = entry_hour_gate(_utc(3), "P1", {})
        assert allowed

    def test_legacy_account_untouched(self):
        # A-D containers set no ENTRY_HOURS_* → gate is a no-op at any hour
        for hour in range(24):
            allowed, _ = entry_hour_gate(_utc(hour), "A", {})
            assert allowed

    def test_entry_hours_alone_blocks_outside(self):
        allowed, _ = entry_hour_gate(_utc(12), "P2", {"ENTRY_HOURS_P2": "0,1"})
        assert not allowed

    def test_blocked_hours_alone_blocks_inside(self):
        allowed, _ = entry_hour_gate(_utc(14), "P2", {"BLOCKED_HOURS_P2": "14"})
        assert not allowed


class TestPortfolioGate:
    """Causal: DB STATE causes the block — clear the state → passes."""

    def test_running_account_allowed(self, p_db):
        allowed, reason = portfolio_gate("P1", p_db)
        assert allowed
        assert reason == ""

    def test_frozen_state_causes_block(self, p_db):
        set_portfolio_status("P1", "frozen", "DD 20% from peak", p_db)
        allowed, reason = portfolio_gate("P1", p_db)
        assert not allowed
        assert "portfolio_status=frozen" in reason
        assert "DD 20% from peak" in reason

    def test_causal_unfreeze_removes_block(self, p_db):
        """Same frozen account, status back to running → allowed. Proves the
        DB state (not time or side effects) causes the block."""
        set_portfolio_status("P1", "frozen", "DD 20%", p_db)
        assert not portfolio_gate("P1", p_db)[0]
        set_portfolio_status("P1", "running", "", p_db)
        assert portfolio_gate("P1", p_db)[0]

    def test_closed_account_blocked(self, p_db):
        set_portfolio_status("P1", "closed", "manual close by Hermes", p_db)
        allowed, reason = portfolio_gate("P1", p_db)
        assert not allowed
        assert "portfolio_status=closed" in reason

    def test_active_cooldown_blocks(self, p_db):
        future = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
        set_cooldown("P1", future, "3 consecutive losses", p_db)
        allowed, reason = portfolio_gate("P1", p_db)
        assert not allowed
        assert "cooldown" in reason

    def test_expired_cooldown_allows(self, p_db):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        set_cooldown("P1", past, "3 consecutive losses", p_db)
        allowed, _ = portfolio_gate("P1", p_db)
        assert allowed

    def test_naive_timestamp_treated_as_utc(self, p_db):
        future = (datetime.now(timezone.utc).replace(tzinfo=None)
                  + timedelta(hours=6)).isoformat()
        set_cooldown("P1", future, "CB pause", p_db)
        assert not portfolio_gate("P1", p_db)[0]

    def test_missing_account_fails_open(self, p_db):
        # Legacy accounts (A-D) have no portfolio state — must not break
        allowed, _ = portfolio_gate("A", p_db)
        assert allowed

    def test_unparseable_cooldown_ignored(self, p_db):
        from metty.core.db import get_connection
        conn = get_connection(p_db)
        conn.execute("UPDATE accounts SET cooldown_until = 'garbage' WHERE name = 'P1'")
        conn.commit()
        conn.close()
        allowed, _ = portfolio_gate("P1", p_db)
        assert allowed

    def test_gate_survives_container_restart(self, p_db):
        """The whole point of the DB-backed gate: state persists across engine
        restarts (DrawdownProtector is in-memory and resets)."""
        set_portfolio_status("P1", "frozen", "DD 20% from peak", p_db)
        log_portfolio_event("P1", "frozen", "DD 20% from peak",
                            {"equity": 80.0, "dd_pct": 20.0}, p_db)
        # Simulate restart: fresh process reads the same DB file
        allowed, _ = portfolio_gate("P1", p_db)
        assert not allowed
        state = get_account_portfolio_state("P1", p_db)
        assert state["portfolio_status"] == "frozen"

    def test_peak_equity_update_alone_does_not_block(self, p_db):
        # Manager tracks winners via peak_equity — high-water updates must
        # never interfere with the gate
        update_peak_equity("P1", 130.0, p_db)
        assert portfolio_gate("P1", p_db)[0]