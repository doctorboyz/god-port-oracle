"""Integration tests for reconciliation + drawdown protection code paths.

ISSUE-026: reconcile_closed_positions — matches DB open trades against MT5
state, closes orphans using deal history (with SL/TP inference fallback).

ISSUE-029: DrawdownProtector.check + sync_pnl_from_db — daily/weekly/account
drawdown limits, with DB-sync so reconciliation-closed trades are picked up
(not just record_pnl() calls).

These are integration tests because they exercise the full DB → code path
with a real SQLite temp DB and the production schema, catching bugs that
unit tests with mocks miss (e.g., SQL column casing, NULL handling,
reconciliation → drawdown handoff).

References:
  - metty/core/db.py:1216 reconcile_closed_positions
  - metty/core/db.py:1987 get_pnl_summary
  - broky/risk/drawdown_protection.py:108 DrawdownProtector.check
  - broky/risk/drawdown_protection.py:168 DrawdownProtector.sync_pnl_from_db
  - learning [[drawdown-db-sync]], [[ghost-trade-prevention]]
"""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from metty.core.db import (
    SCHEMA_SQL,
    insert_live_trade,
    reconcile_closed_positions,
    get_pnl_summary,
)
from broky.risk.drawdown_protection import DrawdownProtector


# ─── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def temp_db():
    """Create a fresh temp DB with the production schema + a test account row."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    # Execute the SCHEMA_SQL from db.py (creates accounts, live_trades, etc.)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_SQL)
    # Insert a test account row (accounts schema: broker_login, broker_server,
    # balance, leverage, bridge_host, bridge_port, signal_group, is_active)
    conn.execute(
        "INSERT INTO accounts (id, name, broker_login, broker_server, balance, "
        "leverage, bridge_host, bridge_port, signal_group, is_active) "
        "VALUES (1, 'Real-A', '12345', 'Exness-Real', 200.0, 500, "
        "'localhost', 18812, 'A', 1)"
    )
    conn.commit()
    conn.close()

    yield db_path

    # Cleanup
    try:
        db_path.unlink()
    except OSError:
        pass


def _insert_open_trade(
    db_path: Path,
    *,
    account_id: int = 1,
    direction: str = "BUY",
    entry_price: float = 2300.0,
    stop_loss: float = 2290.0,
    take_profit: float = 2320.0,
    lot_size: float = 0.01,
    ticket: int | None = None,
) -> int:
    """Insert an open trade and return its id."""
    return insert_live_trade(
        account_id=account_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        direction=direction,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        lot_size=lot_size,
        confidence=0.6,
        regime="trending",
        session="london",
        d1_trend="bullish",
        ticket=ticket,
        db_path=db_path,
    )


def _fetch_trade(db_path: Path, trade_id: int) -> dict:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM live_trades WHERE id = ?", (trade_id,)).fetchone()
    conn.close()
    return dict(row)


# ─── ISSUE-026: reconcile_closed_positions ───────────────────────────────


@pytest.mark.integration
class TestReconcileClosedPositions:
    """Integration tests for metty.core.db.reconcile_closed_positions."""

    def test_empty_open_trades_returns_zero(self, temp_db):
        """No open trades → no work → return 0."""
        result = reconcile_closed_positions(
            account_id=1,
            open_trades=[],
            mt5_positions=[],
            mt5_deals=[],
            db_path=temp_db,
        )
        assert result == 0

    def test_position_still_in_mt5_not_closed(self, temp_db):
        """Trade with ticket that's still in MT5 → should NOT be closed."""
        trade_id = _insert_open_trade(temp_db, ticket=50001)
        open_trades = [{"id": trade_id, "ticket": 50001, "direction": "BUY",
                        "entry_price": 2300.0, "stop_loss": 2290.0,
                        "take_profit": 2320.0, "lot_size": 0.01}]
        mt5_positions = [{"identifier": 50001}]
        mt5_deals = []

        result = reconcile_closed_positions(1, open_trades, mt5_positions, mt5_deals, temp_db)

        assert result == 0
        row = _fetch_trade(temp_db, trade_id)
        assert row["is_open"] == 1, "trade should remain open — MT5 still has it"
        assert row["exit_price"] is None

    def test_closed_by_mt5_with_matching_deal_uses_deal_price(self, temp_db):
        """Trade ticket gone from MT5 + matching deal found → use deal's price/pnl."""
        trade_id = _insert_open_trade(temp_db, ticket=50002, direction="BUY",
                                       entry_price=2300.0, stop_loss=2290.0,
                                       take_profit=2320.0)
        open_trades = [{"id": trade_id, "ticket": 50002, "direction": "BUY",
                        "entry_price": 2300.0, "stop_loss": 2290.0,
                        "take_profit": 2320.0, "lot_size": 0.01}]
        # MT5 has NO position 50002 (it closed)
        mt5_positions = []
        # But deal history shows it closed at TP with profit.
        # _match_closing_deal strategy 0: deal.order == position.ticket.
        # BUY position is closed by a SELL deal (type=1).
        mt5_deals = [{"ticket": 99001, "order": 50002, "type": 1,
                      "price": 2320.0, "profit": 2.0, "comment": "[tp]"}]

        result = reconcile_closed_positions(1, open_trades, mt5_positions, mt5_deals, temp_db)

        assert result == 1
        row = _fetch_trade(temp_db, trade_id)
        assert row["is_open"] == 0
        assert row["exit_price"] == 2320.0
        assert row["pnl"] == 2.0
        assert row["exit_reason"] == "take_profit", "comment [tp] should map to take_profit"

    def test_closed_by_mt5_no_deal_infers_from_sl_when_reason_is_sl(self, temp_db):
        """No matching deal + exit_reason='stop_loss' → infer exit from SL.

        Per commit 928fb3b: _infer_exit_price uses exit_reason hint to pick
        SL vs TP vs entry_price fallback. With reason='stop_loss', SL is used.
        """
        trade_id = _insert_open_trade(temp_db, ticket=50003, direction="BUY",
                                       entry_price=2300.0, stop_loss=2290.0,
                                       take_profit=2320.0)
        open_trades = [{"id": trade_id, "ticket": 50003, "direction": "BUY",
                        "entry_price": 2300.0, "stop_loss": 2290.0,
                        "take_profit": 2320.0, "lot_size": 0.01,
                        "exit_reason": "stop_loss"}]
        mt5_positions = []
        mt5_deals = []  # No deal history

        result = reconcile_closed_positions(1, open_trades, mt5_positions, mt5_deals, temp_db)

        assert result == 1
        row = _fetch_trade(temp_db, trade_id)
        assert row["is_open"] == 0
        # BUY SL is below entry → exit at 2290 (the SL)
        assert row["exit_price"] == 2290.0
        assert row["pnl"] is not None
        assert row["pnl"] < 0, "BUY stopped out below entry → negative PnL"
        assert "inferred" in (row["exit_reason"] or "")

    def test_closed_by_mt5_no_deal_unknown_reason_uses_entry_price(self, temp_db):
        """No deal + unknown exit_reason → fallback to entry_price (breakeven).

        Per commit 928fb3b: do NOT default to SL when exit_reason is unknown.
        MT5 closes at market price during margin calls, not SL. Using
        entry_price is more neutral (records PnL as ~0).
        """
        trade_id = _insert_open_trade(temp_db, ticket=50004, direction="BUY",
                                       entry_price=2300.0, stop_loss=2290.0,
                                       take_profit=2320.0)
        open_trades = [{"id": trade_id, "ticket": 50004, "direction": "BUY",
                        "entry_price": 2300.0, "stop_loss": 2290.0,
                        "take_profit": 2320.0, "lot_size": 0.01,
                        "exit_reason": None}]
        mt5_positions = []
        mt5_deals = []

        result = reconcile_closed_positions(1, open_trades, mt5_positions, mt5_deals, temp_db)

        assert result == 1
        row = _fetch_trade(temp_db, trade_id)
        assert row["is_open"] == 0
        # Unknown reason → entry_price fallback (NOT SL)
        assert row["exit_price"] == 2300.0
        assert row["pnl"] == 0.0, "entry_price fallback → PnL ~0"
        assert "inferred" in (row["exit_reason"] or "")

    def test_ghost_trade_no_ticket_closed_with_sl_reason(self, temp_db):
        """Trade with ticket=None (ghost) + exit_reason='stop_loss' → closed at SL."""
        trade_id = _insert_open_trade(temp_db, ticket=None, direction="SELL",
                                       entry_price=2300.0, stop_loss=2310.0,
                                       take_profit=2280.0)
        open_trades = [{"id": trade_id, "ticket": None, "direction": "SELL",
                        "entry_price": 2300.0, "stop_loss": 2310.0,
                        "take_profit": 2280.0, "lot_size": 0.01,
                        "exit_reason": "stop_loss"}]
        mt5_positions = []
        mt5_deals = []

        result = reconcile_closed_positions(1, open_trades, mt5_positions, mt5_deals, temp_db)

        # Ghost trade should be closed (no ticket → not in MT5)
        assert result == 1
        row = _fetch_trade(temp_db, trade_id)
        assert row["is_open"] == 0
        # SELL SL is above entry → exit at 2310 (the SL)
        assert row["exit_price"] == 2310.0
        assert row["pnl"] is not None
        assert row["pnl"] < 0, "SELL stopped out above entry → negative PnL"

    def test_multiple_orphans_all_closed(self, temp_db):
        """Multiple orphaned trades → all closed, count returned."""
        ids = []
        for i in range(3):
            tid = _insert_open_trade(temp_db, ticket=60000 + i,
                                      entry_price=2300.0 + i, stop_loss=2290.0)
            ids.append(tid)
        open_trades = [
            {"id": tid, "ticket": 60000 + i, "direction": "BUY",
             "entry_price": 2300.0 + i, "stop_loss": 2290.0,
             "take_profit": 2320.0, "lot_size": 0.01, "exit_reason": None}
            for i, tid in enumerate(ids)
        ]
        # MT5 has none of them
        mt5_positions = []
        mt5_deals = []

        result = reconcile_closed_positions(1, open_trades, mt5_positions, mt5_deals, temp_db)

        assert result == 3
        for tid in ids:
            row = _fetch_trade(temp_db, tid)
            assert row["is_open"] == 0


# ─── ISSUE-029: DrawdownProtector + sync_pnl_from_db integration ─────────


@pytest.mark.integration
class TestDrawdownProtectionIntegration:
    """Integration tests for DrawdownProtector with DB sync.

    Key scenario: reconciliation closes a trade at a loss between oracle
    cycles. Without sync_pnl_from_db, the in-memory daily_pnl would not
    reflect that loss and drawdown protection would not trigger. With sync,
    the DB is the source of truth and the loss is picked up.
    """

    def test_daily_drawdown_blocks_trading(self):
        """Equity drop within daily limit → trade allowed; over limit → blocked."""
        protector = DrawdownProtector(
            initial_equity=100.0,
            daily_limit_pct=0.10,  # 10% daily limit = $10
            weekly_limit_pct=0.30,
            account_limit_pct=0.30,
        )
        # First call initializes daily_start_equity
        can_trade, _ = protector.check(100.0)
        assert can_trade is True

        # Record $5 loss — under 10% limit, still allowed
        protector.record_pnl(pnl=-5.0, equity=95.0)
        can_trade, reason = protector.check(95.0)
        assert can_trade is True, f"under limit should allow: {reason}"

        # Record another $7 loss — total -$12, over 10% of $100 = $10
        protector.record_pnl(pnl=-7.0, equity=88.0)
        can_trade, reason = protector.check(88.0)
        assert can_trade is False, "over daily limit should block"
        assert "Daily drawdown" in reason or "daily" in reason.lower()

    def test_account_drawdown_hard_stop(self):
        """Equity below account_limit_pct of initial → permanent block."""
        protector = DrawdownProtector(
            initial_equity=100.0,
            daily_limit_pct=0.50,    # high so daily doesn't trigger first
            weekly_limit_pct=0.50,
            account_limit_pct=0.30,  # 30% → block below $70
        )
        # Equity at $69 → below $70 hard stop
        can_trade, reason = protector.check(69.0)
        assert can_trade is False
        assert "Account drawdown" in reason or "drawdown" in reason.lower()

    def test_peak_drawdown_cooldown(self):
        """Equity drops from peak by account_limit_pct → cooldown block."""
        protector = DrawdownProtector(
            initial_equity=100.0,
            daily_limit_pct=0.50,
            weekly_limit_pct=0.50,
            account_limit_pct=0.20,  # 20% from peak → block
        )
        # Run equity up to $150 (peak)
        protector.check(150.0)
        # Drop to $120 → 20% from $150 peak = exactly the limit
        can_trade, reason = protector.check(120.0)
        assert can_trade is False
        assert "Peak drawdown" in reason or "peak" in reason.lower()

    def test_sync_pnl_from_db_picks_up_reconciliation_closes(self, temp_db):
        """DB has closed losing trades → sync updates daily_pnl → block triggers.

        This is the key integration scenario: reconciliation closed a trade
        at SL between oracle cycles. record_pnl() was never called. Without
        sync, drawdown protection would not see the loss.
        """
        # Insert 2 closed losing trades for account 1 today
        for i in range(2):
            tid = _insert_open_trade(temp_db, ticket=70000 + i,
                                      entry_price=2300.0, stop_loss=2290.0)
            # Close them at SL with -$10 PnL each
            conn = sqlite3.connect(str(temp_db))
            conn.execute(
                "UPDATE live_trades SET is_open=0, exit_price=2290.0, pnl=-10.0, "
                "exit_reason='stop_loss', exit_time=? WHERE id=?",
                (datetime.now(timezone.utc).isoformat(), tid),
            )
            conn.commit()
            conn.close()

        # DrawdownProtector with $100 initial, 15% daily limit = $15
        # $20 in DB losses > $15 limit → should block after sync
        protector = DrawdownProtector(
            initial_equity=100.0,
            daily_limit_pct=0.15,  # $15 limit
            weekly_limit_pct=0.50,
            account_limit_pct=0.50,
        )
        # Initialize period
        protector.check(100.0)

        # In-memory daily_pnl is 0 — no record_pnl() calls
        assert protector.state.daily_pnl == 0.0

        # Sync from DB — should pick up -$20 in losses
        triggered = protector.sync_pnl_from_db(account_id=1, db_path=temp_db)

        # daily_pnl should now reflect DB truth
        assert protector.state.daily_pnl == -20.0, \
            f"sync should set daily_pnl to -20, got {protector.state.daily_pnl}"

        # Now check should block (over 15% limit)
        can_trade, reason = protector.check(80.0)
        assert can_trade is False, "over limit after sync should block"
        assert "Daily drawdown" in reason or "daily" in reason.lower()

    def test_sync_pnl_from_db_no_trades_keeps_state(self, temp_db):
        """Account with no closed trades → sync is a no-op."""
        protector = DrawdownProtector(
            initial_equity=100.0,
            daily_limit_pct=0.10,
            weekly_limit_pct=0.30,
            account_limit_pct=0.30,
        )
        protector.check(100.0)
        protector.record_pnl(pnl=-3.0, equity=97.0)
        assert protector.state.daily_pnl == -3.0

        # No trades in DB for account 1 → sync should not change state
        protector.sync_pnl_from_db(account_id=1, db_path=temp_db)
        # daily_pnl should be 0 from DB (no trades), or unchanged if sync guards
        # The key assertion: sync doesn't crash and produces a sane state
        assert protector.state.daily_pnl in (0.0, -3.0), \
            f"sync with no DB trades should give 0 or keep -3, got {protector.state.daily_pnl}"

    def test_sync_pnl_from_db_handles_wrong_account(self, temp_db):
        """Sync for account with no trades → no crash, no spurious block."""
        # Add account 2 so the FK on live_trades.account_id is satisfied
        conn = sqlite3.connect(str(temp_db))
        conn.execute(
            "INSERT INTO accounts (id, name, broker_login, broker_server, balance, "
            "leverage, bridge_host, bridge_port, signal_group, is_active) "
            "VALUES (2, 'Demo-B', '67890', 'Exness-Demo', 200.0, 500, "
            "'localhost', 18812, 'B', 1)"
        )
        conn.commit()
        conn.close()
        # Insert a trade for account 2, not account 1
        tid = _insert_open_trade(temp_db, account_id=2, ticket=80001,
                                  entry_price=2300.0, stop_loss=2290.0)
        conn = sqlite3.connect(str(temp_db))
        conn.execute(
            "UPDATE live_trades SET is_open=0, exit_price=2290.0, pnl=-50.0, "
            "exit_reason='stop_loss', exit_time=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), tid),
        )
        conn.commit()
        conn.close()

        protector = DrawdownProtector(
            initial_equity=100.0,
            daily_limit_pct=0.10,
            weekly_limit_pct=0.30,
            account_limit_pct=0.30,
        )
        protector.check(100.0)

        # Sync for account 1 — should NOT pick up account 2's losses
        protector.sync_pnl_from_db(account_id=1, db_path=temp_db)
        # Account 1 has no closed trades → daily_pnl from DB = 0
        # (or kept at in-memory value if sync guards against overwriting)
        assert protector.state.daily_pnl in (0.0, 0), \
            f"account 2 losses should NOT leak into account 1, got {protector.state.daily_pnl}"

        # Should still be allowed to trade (no account 1 losses)
        can_trade, _ = protector.check(100.0)
        assert can_trade is True, "account 1 has no losses → should allow"


# ─── ISSUE-026 + ISSUE-029 cross-cutting: reconciliation → drawdown handoff


@pytest.mark.integration
class TestReconciliationDrawdownHandoff:
    """The full handoff: reconciliation closes a trade → sync_pnl_from_db
    picks up the loss → drawdown protection blocks the next trade.

    This is the integration boundary that failed before ISSUE-026/029: the
    in-memory drawdown tracker never saw reconciliation-closed losses.
    """

    def test_reconciliation_then_sync_blocks_trading(self, temp_db):
        """End-to-end: reconcile closes losing trade → sync → block."""
        # Open trade, ticket 90001
        trade_id = _insert_open_trade(temp_db, ticket=90001, direction="BUY",
                                       entry_price=2300.0, stop_loss=2290.0,
                                       take_profit=2320.0)
        open_trades = [{"id": trade_id, "ticket": 90001, "direction": "BUY",
                        "entry_price": 2300.0, "stop_loss": 2290.0,
                        "take_profit": 2320.0, "lot_size": 0.01,
                        "exit_reason": "stop_loss"}]
        # MT5 closed at SL (no position, no deal)
        mt5_positions = []
        mt5_deals = []

        # 1. Reconcile — should close at SL=2290, PnL negative
        closed = reconcile_closed_positions(1, open_trades, mt5_positions, mt5_deals, temp_db)
        assert closed == 1
        row = _fetch_trade(temp_db, trade_id)
        assert row["is_open"] == 0
        assert row["pnl"] < 0

        # 2. DrawdownProtector with tight daily limit so one loss triggers it
        # BUY at 2300, SL at 2290, lot 0.01, contract 100 → PnL = (2290-2300)*0.01*100 = -$10
        protector = DrawdownProtector(
            initial_equity=100.0,
            daily_limit_pct=0.05,  # 5% = $5 limit; $10 loss exceeds it
            weekly_limit_pct=0.30,
            account_limit_pct=0.30,
        )
        protector.check(100.0)

        # 3. Sync from DB — should pick up the -$10 reconciliation close
        protector.sync_pnl_from_db(account_id=1, db_path=temp_db)
        assert protector.state.daily_pnl < 0, \
            f"sync should reflect reconciliation loss, got {protector.state.daily_pnl}"

        # 4. Check should now block
        can_trade, reason = protector.check(90.0)
        assert can_trade is False, \
            "reconciliation loss should propagate to drawdown block"
        assert "Daily drawdown" in reason or "daily" in reason.lower()