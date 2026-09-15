"""Tests for account portfolio schema + helpers (P-accounts, cent portfolio).

Covers: variants table, portfolio_events audit log, accounts portfolio columns,
freeze/cooldown/peak-equity state transitions, and consecutive-loss counting.
"""

import pytest

from metty.core.db import (
    clear_cooldown,
    get_account_portfolio_state,
    get_consecutive_losses,
    get_recent_portfolio_events,
    get_variant,
    init_db,
    insert_account,
    insert_live_trade,
    insert_variant,
    log_portfolio_event,
    set_cooldown,
    set_portfolio_status,
    update_peak_equity,
)


@pytest.fixture()
def p_db(tmp_path):
    """Fresh portfolio DB with one account seeded."""
    db_path = tmp_path / "oracle_portfolio.db"
    init_db(db_path)
    insert_account(
        name="P1",
        balance=100.0,
        leverage=2000,
        bridge_host="mt5p1",
        bridge_port=8001,
        signal_group="portfolio",
        db_path=db_path,
    )
    return db_path


class TestVariantTable:
    def test_insert_and_get_variant_roundtrip(self, p_db):
        params = {"atr_multiplier": 2.0, "rr_ratio": 3.0, "min_confidence": 0.50,
                  "entry_hours_bkk": [1, 2, 3, 13, 15], "risk_per_trade": 0.005}
        insert_variant("P1-base-ny", "baseline sweet spot", params,
                       "NY session PF 2.26 (oracle_vps.db 2,154 trades)", p_db)

        got = get_variant("P1-base-ny", p_db)
        assert got is not None
        assert got["label"] == "baseline sweet spot"
        assert got["params"] == params
        assert "oracle_vps.db" in got["sweet_spot_basis"]

    def test_insert_variant_is_idempotent_upsert(self, p_db):
        insert_variant("P2-tight-sl", "v1", {"atr_multiplier": 1.8},
                       "SL $10-15 sweet spot", p_db)
        insert_variant("P2-tight-sl", "v2 relabeled", {"atr_multiplier": 1.9},
                       "SL $10-15 sweet spot", p_db)

        got = get_variant("P2-tight-sl", p_db)
        assert got["label"] == "v2 relabeled"
        assert got["params"]["atr_multiplier"] == 1.9

    def test_get_variant_missing_returns_none(self, p_db):
        assert get_variant("nope", p_db) is None


class TestPortfolioColumns:
    def test_new_account_defaults_to_running_demo(self, p_db):
        state = get_account_portfolio_state("P1", p_db)
        assert state is not None
        assert state["portfolio_status"] == "running"
        assert state["account_type"] == "demo"
        assert state["variant_id"] == ""
        assert state["cooldown_until"] is None
        assert state["peak_equity"] is None

    def test_missing_account_returns_none(self, p_db):
        assert get_account_portfolio_state("PX", p_db) is None

    def test_freeze_sets_status_reason_and_timestamp(self, p_db):
        assert set_portfolio_status("P1", "frozen", "DD 20% from peak", p_db) is True
        state = get_account_portfolio_state("P1", p_db)
        assert state["portfolio_status"] == "frozen"
        assert state["frozen_reason"] == "DD 20% from peak"
        assert state["frozen_at"] is not None

    def test_unfreeze_clears_frozen_at_keeps_history_in_events(self, p_db):
        set_portfolio_status("P1", "frozen", "DD 20%", p_db)
        set_portfolio_status("P1", "running", "", p_db)
        state = get_account_portfolio_state("P1", p_db)
        assert state["portfolio_status"] == "running"
        assert state["frozen_at"] is None

    def test_invalid_status_rejected(self, p_db):
        with pytest.raises(ValueError):
            set_portfolio_status("P1", "paused", "", p_db)

    def test_set_and_clear_cooldown(self, p_db):
        assert set_cooldown("P1", "2026-09-20T00:00:00", "3 consecutive losses", p_db) is True
        state = get_account_portfolio_state("P1", p_db)
        assert state["cooldown_until"] == "2026-09-20T00:00:00"

        assert clear_cooldown("P1", p_db) is True
        state = get_account_portfolio_state("P1", p_db)
        assert state["cooldown_until"] is None

    def test_peak_equity_is_high_water_mark_only(self, p_db):
        assert update_peak_equity("P1", 105.0, p_db) is True
        # Lower equity must NOT lower the peak
        assert update_peak_equity("P1", 98.0, p_db) is False
        # Higher equity raises it
        assert update_peak_equity("P1", 110.0, p_db) is True
        state = get_account_portfolio_state("P1", p_db)
        assert state["peak_equity"] == 110.0


class TestPortfolioEvents:
    def test_log_event_and_read_back(self, p_db):
        log_portfolio_event("P1", "frozen", "DD 20% from peak",
                            {"equity": 80.0, "dd_pct": 20.0, "streak": 2}, p_db)
        events = get_recent_portfolio_events("P1", 10, p_db)
        assert len(events) == 1
        assert events[0]["event_type"] == "frozen"
        assert events[0]["metrics"]["dd_pct"] == 20.0

    def test_events_are_append_only_across_freeze_cycle(self, p_db):
        log_portfolio_event("P1", "enrolled", "paper-parallel start", {"baseline": 100.0}, p_db)
        set_portfolio_status("P1", "frozen", "DD 20%", p_db)
        log_portfolio_event("P1", "frozen", "DD 20%", {"equity": 80.0}, p_db)
        set_portfolio_status("P1", "running", "manual resume", p_db)
        log_portfolio_event("P1", "unfrozen", "manual resume", None, p_db)

        events = get_recent_portfolio_events("P1", 10, p_db)
        # Kappa #1: every decision stays on the record
        assert [e["event_type"] for e in events] == ["unfrozen", "frozen", "enrolled"]

    def test_recent_events_filter_by_account(self, p_db):
        insert_account(name="P2", balance=100.0, leverage=2000,
                       bridge_host="mt5p2", bridge_port=8002,
                       signal_group="portfolio", db_path=p_db)
        log_portfolio_event("P1", "enrolled", "start", None, p_db)
        log_portfolio_event("P2", "enrolled", "start", None, p_db)

        p1 = get_recent_portfolio_events("P1", 10, p_db)
        assert all(e["account_name"] == "P1" for e in p1)
        all_events = get_recent_portfolio_events(limit=10, db_path=p_db)
        assert len(all_events) == 2


class TestConsecutiveLosses:
    def _closed_trade(self, p_db, account_id, pnl, exit_time):
        trade_id = insert_live_trade(
            account_id=account_id,
            timestamp=exit_time,
            direction="BUY",
            entry_price=2400.0,
            stop_loss=2390.0,
            take_profit=2430.0,
            lot_size=0.01,
            confidence=0.55,
            db_path=p_db,
        )
        from metty.core.db import get_connection
        conn = get_connection(p_db)
        try:
            conn.execute(
                "UPDATE live_trades SET is_open=0, exit_price=?, exit_time=?, pnl=? WHERE id=?",
                (2395.0, exit_time, pnl, trade_id),
            )
            conn.commit()
        finally:
            conn.close()
        return trade_id

    def test_zero_when_no_trades(self, p_db):
        assert get_consecutive_losses(1, p_db) == 0

    def test_counts_trailing_loss_run(self, p_db):
        self._closed_trade(p_db, 1, -1.0, "2026-09-16T01:00:00")
        self._closed_trade(p_db, 1, -1.2, "2026-09-16T02:00:00")
        self._closed_trade(p_db, 1, 3.0, "2026-09-16T03:00:00")
        self._closed_trade(p_db, 1, -0.8, "2026-09-16T04:00:00")
        self._closed_trade(p_db, 1, -1.1, "2026-09-16T05:00:00")
        self._closed_trade(p_db, 1, -0.9, "2026-09-16T06:00:00")
        assert get_consecutive_losses(1, p_db) == 3

    def test_stops_at_first_win(self, p_db):
        self._closed_trade(p_db, 1, -1.0, "2026-09-16T01:00:00")
        self._closed_trade(p_db, 1, 2.0, "2026-09-16T02:00:00")
        self._closed_trade(p_db, 1, -1.0, "2026-09-16T03:00:00")
        assert get_consecutive_losses(1, p_db) == 1

    def test_ignores_open_trades(self, p_db):
        insert_live_trade(
            account_id=1, timestamp="2026-09-16T07:00:00", direction="SELL",
            entry_price=2400.0, stop_loss=2410.0, take_profit=2370.0,
            lot_size=0.01, confidence=0.55, db_path=p_db,
        )
        assert get_consecutive_losses(1, p_db) == 0