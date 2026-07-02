"""Causal proof tests for M5Scalp reconcile isolation bug (2026-07-02).

Hypothesis
----------
On demo B/C/D deploy with AEGIS-only (ML off), live logs show:

  06:08:14 [M5Scalp:Demo-B] Reconciled 1 closed positions
  06:08:14 Reconciled trade #85 (SELL XAUUSD @ 4072.875) → exit=4072.88 pnl=0.00 reason=closed_by_mt5_inferred

Trade #85 is a SWING trade (trading_mode='swing', ticket 2166708648) that is STILL
OPEN in MT5 (current price 4064.902, +7.98 floating). Yet M5Scalp marked it closed
with PnL=0 in DB. Root cause: when MT5 bridge returns an empty positions list
(transient disconnect — log "Spread unavailable — MT5 may be disconnected"),
M5Scalp._check_existing_m5_scalp_position reconciles ALL open DB trades for the
account, not just scalp trades. Because mt5_tickets set is empty, every open
trade looks orphaned → all closed with PnL=0.

Two defects
----------
- M5SCALP-1: get_open_trades returns ALL open trades for the account; M5Scalp
  passes them to reconcile_closed_positions without filtering to scalp-only
  (trading_mode='scalp' or strategy_id='m5-scalp-<acct>').
- M5SCALP-2: M5Scalp has no deals=None guard (swing live_trader has one at
  line 822-831); when deal history fetch fails, it passes None to reconcile
  → reconcile falls back to entry_price inference → PnL=0 false close.

Consequences
------------
1. max_positions block bypassed (DB thinks 0 open while MT5 has 7) → B opens
   7 positions exceeding cap=5 and dynamic_max=1.
2. _monitor_positions in swing live_trader reads DB=0 open → trailing TP / SL
   / TP / time_stop never fire on the real MT5 positions.
3. PnL=0 false closes pollute DP/CB counters (zero PnL recorded as breakeven).

Fix
---
- M5Scalp._check_existing_m5_scalp_position: filter open_trades to scalp-only
  (matching strategy_id OR trading_mode='scalp' OR 'm5_scalp') BEFORE calling
  reconcile_closed_positions.
- M5Scalp._check_existing_m5_scalp_position: add deals=None guard — skip
  reconcile and return open count (hold off opening new trades) when bridge
  is unhealthy, mirroring swing live_trader lines 822-831.

References
----------
- Bug found in session 2026-07-02 (retro 12.36_aegis-deploy-ml-off)
- Swing live_trader guard: metty/execution/live_trader.py:822-831
- Reconcile function: metty/core/db.py:1242 reconcile_closed_positions
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _make_scalp_trader():
    """Construct an M5ScalpTrader instance without running it."""
    from metty.execution.m5_scalp_trader import M5ScalpTrader
    os.environ.setdefault("MT5_BRIDGE_B_HOST", "localhost")
    os.environ.setdefault("MT5_BRIDGE_B_PORT", "8001")
    os.environ.setdefault("MT5_LOGIN_B", "1")
    os.environ.setdefault("MT5_PASSWORD_B", "x")
    t = M5ScalpTrader(account="B", dry_run=True)
    return t


# ─── M5SCALP-1: scalp-only filter ─────────────────────────────────────


class TestScalpReconcileOnlyScalpTrades:
    """M5SCALP-1: M5Scalp reconcile must NOT close swing trades.

    When MT5 returns an empty positions list, M5Scalp's
    _check_existing_m5_scalp_position calls reconcile_closed_positions on ALL
    open DB trades for the account. The fix filters open_trades to scalp-only
    before calling reconcile, so swing trades are never touched.
    """

    def test_swing_trade_not_closed_when_mt5_empty(self, tmp_path, monkeypatch):
        """Causal test: a swing trade in DB must remain is_open=1 after
        M5Scalp reconcile sees an empty MT5 positions list. Without the fix,
        the swing trade gets marked closed_by_mt5_inferred with PnL=0
        (because it's treated as orphaned)."""
        import sqlite3
        from metty.core.db import init_db

        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        conn = sqlite3.connect(db_path)
        # Insert account + 1 swing trade (open, ticket matches a 'live' MT5
        # ticket but we'll pretend MT5 returned empty)
        conn.execute(
            "INSERT INTO accounts (id, name, balance, leverage, bridge_host, "
            "bridge_port, signal_group) VALUES (2,'B',201.74,500,'h',5005,'volume')"
        )
        conn.execute(
            "INSERT INTO live_trades (id, account_id, timestamp, direction, "
            "symbol, entry_price, stop_loss, take_profit, lot_size, confidence, "
            "regime, session, d1_trend, reason, trading_mode, strategy_id, ticket, "
            "is_open) VALUES (10, 2, '2026-07-02T05:28:00+00:00', 'BUY', 'XAUUSD', "
            "4059.17, 4047.64, 4088.01, 0.01, 0.45, 'ranging', 'london', 'bullish', "
            "'swing trade should stay open', 'swing', 'swing-B', 2166523300, 1)"
        )
        conn.commit()
        conn.close()

        t = _make_scalp_trader()
        monkeypatch.setattr(t, "db_path", db_path)
        monkeypatch.setattr(t, "account_id", 2)
        monkeypatch.setattr(t, "display_name", "B")

        # Mock rpyc to return empty positions (transient disconnect)
        import metty.execution.m5_scalp_trader as mod
        mock_conn = MagicMock()
        mock_conn.root.positions_get.return_value = []  # empty = MT5 'no positions'
        monkeypatch.setattr(
            "rpyc.connect", lambda *a, **k: mock_conn
        )
        # Mock deal history to return [] (healthy bridge, just no deals)
        monkeypatch.setattr(t, "_get_deal_history", lambda days_back=7: [])

        result = t._check_existing_m5_scalp_position()

        # Verify swing trade is STILL OPEN in DB (not closed)
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT is_open, exit_reason, pnl FROM live_trades WHERE id=10"
        ).fetchone()
        conn.close()
        assert row is not None, "swing trade must still exist"
        assert row[0] == 1, (
            f"swing trade must remain is_open=1 — M5Scalp must not close it. "
            f"Got is_open={row[0]}, exit_reason={row[1]}, pnl={row[2]}"
        )
        assert row[1] is None, (
            f"swing trade exit_reason must be NULL, got {row[1]}"
        )

    def test_swing_trade_not_closed_when_deals_unavailable(self, tmp_path, monkeypatch):
        """M5SCALP-2: even if MT5 returns empty positions, M5Scalp must skip
        reconciliation when deal history is unavailable (bridge unhealthy).
        Mirrors swing live_trader lines 822-831 — when deals is None, return
        without closing anything; let the next cycle retry."""
        import sqlite3
        from metty.core.db import init_db

        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO accounts (id, name, balance, leverage, bridge_host, "
            "bridge_port, signal_group) VALUES (2,'B',201.74,500,'h',5005,'volume')"
        )
        # 1 swing trade + 1 scalp trade, both open
        conn.execute(
            "INSERT INTO live_trades (id, account_id, timestamp, direction, "
            "symbol, entry_price, stop_loss, take_profit, lot_size, confidence, "
            "regime, session, d1_trend, reason, trading_mode, strategy_id, ticket, "
            "is_open) VALUES (10, 2, '2026-07-02T05:28:00+00:00', 'BUY', 'XAUUSD', "
            "4059.17, 4047.64, 4088.01, 0.01, 0.45, 'ranging', 'london', 'bullish', "
            "'swing', 'swing', 'swing-B', 2166523300, 1)"
        )
        conn.execute(
            "INSERT INTO live_trades (id, account_id, timestamp, direction, "
            "symbol, entry_price, stop_loss, take_profit, lot_size, confidence, "
            "regime, session, d1_trend, reason, trading_mode, strategy_id, ticket, "
            "is_open) VALUES (11, 2, '2026-07-02T05:28:00+00:00', 'SELL', 'XAUUSD', "
            "4059.17, 4060.0, 4055.0, 0.01, 0.45, 'ranging', 'london', 'bullish', "
            "'scalp', 'm5_scalp', 'm5-scalp-B', 999, 1)"
        )
        conn.commit()
        conn.close()

        t = _make_scalp_trader()
        monkeypatch.setattr(t, "db_path", db_path)
        monkeypatch.setattr(t, "account_id", 2)
        monkeypatch.setattr(t, "display_name", "B")

        # MT5 returns empty positions AND deals unavailable (None)
        import metty.execution.m5_scalp_trader as mod
        mock_conn = MagicMock()
        mock_conn.root.positions_get.return_value = []
        monkeypatch.setattr("rpyc.connect", lambda *a, **k: mock_conn)
        monkeypatch.setattr(t, "_get_deal_history", lambda days_back=7: None)

        result = t._check_existing_m5_scalp_position()

        # Both trades must remain open — bridge is unhealthy, skip reconcile
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT id, is_open, exit_reason FROM live_trades ORDER BY id"
        ).fetchall()
        conn.close()
        for r in rows:
            assert r[1] == 1, (
                f"trade id={r[0]} must remain is_open=1 when deals unavailable "
                f"(bridge unhealthy — must skip reconcile). Got exit_reason={r[2]}"
            )


# ─── M5SCALP-1b: scalp trade IS reconciled when truly orphaned ─────────


class TestScalpReconcileStillClosesOrphanedScalp:
    """Sanity: the filter must not break legitimate reconciliation of scalp
    trades that are genuinely closed in MT5 (orphaned in DB but MT5 confirmed
    closed + deal history present)."""

    def test_orphaned_scalp_trade_closed_with_deal(self, tmp_path, monkeypatch):
        import sqlite3
        from metty.core.db import init_db

        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO accounts (id, name, balance, leverage, bridge_host, "
            "bridge_port, signal_group) VALUES (2,'B',201.74,500,'h',5005,'volume')"
        )
        # scalp trade, ticket 999 — MT5 will return empty so it's 'orphaned'
        conn.execute(
            "INSERT INTO live_trades (id, account_id, timestamp, direction, "
            "symbol, entry_price, stop_loss, take_profit, lot_size, confidence, "
            "regime, session, d1_trend, reason, trading_mode, strategy_id, ticket, "
            "is_open) VALUES (12, 2, '2026-07-02T05:28:00+00:00', 'SELL', 'XAUUSD', "
            "4059.17, 4060.0, 4055.0, 0.01, 0.45, 'ranging', 'london', 'bullish', "
            "'scalp', 'm5_scalp', 'm5-scalp-B', 999, 1)"
        )
        conn.commit()
        conn.close()

        t = _make_scalp_trader()
        monkeypatch.setattr(t, "db_path", db_path)
        monkeypatch.setattr(t, "account_id", 2)
        monkeypatch.setattr(t, "display_name", "B")

        # MT5 returns empty positions; deals shows the scalp trade was closed
        import metty.execution.m5_scalp_trader as mod
        mock_conn = MagicMock()
        mock_conn.root.positions_get.return_value = []
        monkeypatch.setattr("rpyc.connect", lambda *a, **k: mock_conn)
        # Deal history: one deal matching ticket 999, closed at TP
        deals = [{
            "ticket": 999,
            "entry": 4059.17,
            "price": 4055.0,
            "comment": "[tp]",
            "type": 1,  # DEAL_TYPE_SELL (closing a BUY)
        }]
        monkeypatch.setattr(t, "_get_deal_history", lambda days_back=7: deals)

        t._check_existing_m5_scalp_position()

        # Scalp trade must be closed (legit reconciliation)
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT is_open, exit_reason FROM live_trades WHERE id=12"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == 0, (
            f"orphaned scalp trade must be closed by reconcile. "
            f"Got is_open={row[0]}, exit_reason={row[1]}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])