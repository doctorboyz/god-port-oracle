"""Tests for scripts/portfolio_manager.py — the kill/freeze/compound brain.

Causal tests throughout: the SAME account+equity either freezes or doesn't
purely because of peak_equity / loss-streak state.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from metty.core.db import (
    get_account_portfolio_state,
    get_recent_portfolio_events,
    init_db,
    insert_account,
    set_portfolio_status,
)
from scripts.portfolio_manager import (
    CB_CONSECUTIVE_LOSSES,
    FREEZE_DD_PCT,
    build_weekly_summary,
    check_account,
    is_weekly_window,
)


@pytest.fixture()
def p1_db(tmp_path):
    """Per-account DB with P1 enrolled at $100 baseline / $100 peak."""
    db_path = tmp_path / "oracle_p1.db"
    init_db(db_path)
    insert_account(name="P1", balance=100.0, leverage=2000,
                   bridge_host="mt5p1", bridge_port=8001,
                   signal_group="portfolio", db_path=db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE accounts SET baseline_balance = 100.0, peak_equity = 100.0, "
            "portfolio_status = 'running', variant_id = 'p1-base-ny' WHERE name = 'P1'"
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _add_closed_trade(db_path, pnl: float, minutes_ago: int = 10) -> None:
    """Insert a closed trade row for account P1 (account_id=1)."""
    ts = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO live_trades (account_id, timestamp, direction, entry_price, "
            "lot_size, confidence, exit_time, pnl, is_open) "
            "VALUES (1, ?, 'BUY', 2300.0, 0.01, 0.6, ?, ?, 0)",
            (ts.isoformat(), ts.isoformat(), pnl),
        )
        conn.commit()
    finally:
        conn.close()


class TestCheckAccountFreeze:
    def test_dd_at_kill_line_freezes(self, p1_db):
        """peak 100, equity 80 → DD 20% → frozen + audit event."""
        actions = check_account("P1", p1_db, equity=80.0)
        state = get_account_portfolio_state("P1", p1_db)
        assert state["portfolio_status"] == "frozen"
        assert "drawdown 20.0%" in state["frozen_reason"]
        assert any(a["type"] == "frozen" for a in actions)
        events = get_recent_portfolio_events("P1", db_path=p1_db)
        assert any(e["event_type"] == "frozen" for e in events)

    def test_causal_smaller_dd_does_not_freeze(self, p1_db):
        """Same account, equity 80.01 (DD 19.99%) → still running.
        Proves the freeze is caused by crossing the 20% line, not by any fall."""
        check_account("P1", p1_db, equity=80.01)
        state = get_account_portfolio_state("P1", p1_db)
        assert state["portfolio_status"] == "running"

    def test_dd_from_high_water_not_baseline(self, p1_db):
        """peak rose to 150 (winner compounding), equity 118 → DD from PEAK
        is >20% even though equity > baseline 100. Freeze must trigger."""
        conn = sqlite3.connect(p1_db)
        try:
            conn.execute("UPDATE accounts SET peak_equity = 150.0 WHERE name = 'P1'")
            conn.commit()
        finally:
            conn.close()
        check_account("P1", p1_db, equity=118.0)  # (150-118)/150 = 21.3%
        state = get_account_portfolio_state("P1", p1_db)
        assert state["portfolio_status"] == "frozen"

    def test_high_water_mark_never_lowers(self, p1_db):
        """Equity falls → peak stays at the old high (compounding base intact)."""
        check_account("P1", p1_db, equity=80.0)
        state = get_account_portfolio_state("P1", p1_db)
        assert state["peak_equity"] == 100.0  # not dragged down to 80

    def test_peak_rises_on_new_high(self, p1_db):
        actions = check_account("P1", p1_db, equity=120.0)
        state = get_account_portfolio_state("P1", p1_db)
        assert state["peak_equity"] == 120.0
        assert any(a["type"] == "peak_updated" and a["equity"] == 120.0 for a in actions)

    def test_frozen_account_not_refrozen(self, p1_db):
        """Idempotent: already frozen → no duplicate freeze events."""
        set_portfolio_status("P1", "frozen", "manual test freeze", p1_db)
        actions = check_account("P1", p1_db, equity=80.0)
        assert not any(a["type"] == "frozen" for a in actions)
        events = get_recent_portfolio_events("P1", db_path=p1_db)
        assert not any(e["event_type"] == "frozen" for e in events)

    def test_closed_account_untouched_by_automation(self, p1_db):
        """closed is MANUAL-ONLY: even at DD 40% the manager must not act,
        and must not update the peak either."""
        set_portfolio_status("P1", "closed", "หมอปิดใบนี้แล้ว", p1_db)
        actions = check_account("P1", p1_db, equity=50.0)
        assert actions == []
        state = get_account_portfolio_state("P1", p1_db)
        assert state["portfolio_status"] == "closed"

    def test_dry_run_writes_nothing(self, p1_db):
        """--dry-run reports the freeze decision but leaves DB untouched."""
        actions = check_account("P1", p1_db, equity=80.0, dry_run=True)
        assert any(a["type"] == "frozen" for a in actions)
        state = get_account_portfolio_state("P1", p1_db)
        assert state["portfolio_status"] == "running"
        assert get_recent_portfolio_events("P1", db_path=p1_db) == []


class TestCheckAccountCircuitBreaker:
    def test_three_consecutive_losses_start_cooldown(self, p1_db):
        for i in range(3):
            _add_closed_trade(p1_db, pnl=-1.0, minutes_ago=60 - i * 10)
        now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
        actions = check_account("P1", p1_db, equity=100.0, now_utc=now)
        state = get_account_portfolio_state("P1", p1_db)
        assert any(a["type"] == "cooldown_set" for a in actions)
        assert state["cooldown_until"] is not None
        until = datetime.fromisoformat(state["cooldown_until"])
        assert until == now + timedelta(hours=24)
        events = get_recent_portfolio_events("P1", db_path=p1_db)
        assert any(e["event_type"] == "cooldown_set" for e in events)

    def test_causal_two_losses_no_cooldown(self, p1_db):
        """Same account, same equity — only the streak length differs."""
        for i in range(2):
            _add_closed_trade(p1_db, pnl=-1.0, minutes_ago=60 - i * 10)
        check_account("P1", p1_db, equity=100.0)
        state = get_account_portfolio_state("P1", p1_db)
        assert state["cooldown_until"] is None

    def test_win_breaks_the_streak(self, p1_db):
        for i in range(3):
            _add_closed_trade(p1_db, pnl=-1.0, minutes_ago=90 - i * 10)
        _add_closed_trade(p1_db, pnl=+2.0, minutes_ago=5)  # newest trade wins
        check_account("P1", p1_db, equity=100.0)
        state = get_account_portfolio_state("P1", p1_db)
        assert state["cooldown_until"] is None

    def test_equity_unavailable_still_checks_losses(self, p1_db):
        """Bridge down → equity None → sizing checks skipped, but the
        loss-streak cooldown still protects the account."""
        for i in range(3):
            _add_closed_trade(p1_db, pnl=-1.0, minutes_ago=60 - i * 10)
        actions = check_account("P1", p1_db, equity=None)
        assert any(a["type"] == "cooldown_set" for a in actions)
        assert not any(a["type"] == "frozen" for a in actions)

    def test_unenrolled_account_skipped(self, tmp_path):
        db_path = tmp_path / "oracle_p9.db"
        init_db(db_path)  # schema, no P9 row
        assert check_account("P9", db_path, equity=100.0) == []


class TestWeeklySummary:
    def test_summary_lists_every_account(self, p1_db, tmp_path):
        text = build_weekly_summary(["P1", "P9"], tmp_path, {"P1": p1_db, "P9": tmp_path / "oracle_p9.db"})
        assert "P1 [running]" in text
        assert "P9" in text and "not enrolled" in text

    def test_summary_counts_weekly_trades(self, p1_db, tmp_path):
        _add_closed_trade(p1_db, pnl=+3.0, minutes_ago=60)
        _add_closed_trade(p1_db, pnl=-1.0, minutes_ago=30)
        text = build_weekly_summary(["P1"], tmp_path, {"P1": p1_db})
        assert "1W/1L" in text
        assert "+2.00" in text


class TestWeeklyWindow:
    def test_sunday_13utc_is_weekly(self):
        assert is_weekly_window(datetime(2026, 9, 13, 13, 30, tzinfo=timezone.utc)) is True

    def test_sunday_other_hour_not_weekly(self):
        assert is_weekly_window(datetime(2026, 9, 13, 14, 0, tzinfo=timezone.utc)) is False

    def test_wednesday_13utc_not_weekly(self):
        assert is_weekly_window(datetime(2026, 9, 16, 13, 0, tzinfo=timezone.utc)) is False


class TestDbPathLayout:
    def test_matches_vps_bind_mount_layout(self):
        """Host cron reads ${PORTFOLIO_DATA_DIR}/p1/oracle_p1.db — the same
        file the engine container writes at /app/data/oracle_p1.db."""
        from scripts.portfolio_manager import account_db_path
        assert account_db_path("P1", "/opt/god-port/data") == \
            Path("/opt/god-port/data/p1/oracle_p1.db")