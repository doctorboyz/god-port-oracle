"""Causal proof tests for swing live_trader reconcile bridge-down bug (2026-07-02).

Hypothesis
----------
On demo B/C/D after M5Scalp fix deploy, swing live_trader entered a runaway
loop opening new SELL positions every 5 minutes while the old ones were
still open in MT5. Logs:

  08:24:24 TRADE_FILLED | account=D | dir=SELL | ticket=3407249015
  08:29:27 Reconciled trade #95 (SELL XAUUSD @ 4074.318) → exit=4074.32 pnl=0.00 reason=closed_by_mt5_inferred
  08:29:28 TRADE_FILLED | account=D | dir=SELL | ticket=3407265458
  08:29:28 TRADE_FILLED | account=B | dir=SELL | ticket=2167377626

Root cause: when MT5 bridge is disconnected, `positions_get` returns None.
The swing live_trader treated `None` as "no position" (same as `[]`) and ran
reconcile_closed_positions. `_get_deal_history` swallowed bridge failure via
`deals or []` (returning `[]` instead of None), so the existing `if deals is
None` guard at lines 822-831 NEVER fired. Reconcile then fell back to
entry_price inference → marked real open trades as `closed_by_mt5_inferred`
PnL=0 → DB thinks 0 open → opens NEW position → MT5 accumulates real
positions → account goes more negative every cycle without hitting SL.

Three defects fixed in this session
-----------------------------------
- SWING-RECONCILE-1: live_trader._check_existing_position did not distinguish
  positions_raw is None (bridge down) from [] (bridge OK, no positions).
  Fix: skip reconcile entirely when positions_raw is None; return
  len(open_trades) > 0 to hold off new trades.
- M5SCALP-RECONCILE-1: same issue in m5_scalp_trader._check_existing_m5_scalp_position.
- DEALS-None-1: fetch_deal_history_sync swallowed connect failures and returned
  [] instead of None, breaking the `if deals is None` guards in both traders.
  Fix: return None on connect failure / exception; propagate through
  _get_deal_history (no `or []` swallowing).

References
----------
- Bug found in session 2026-07-02 after M5Scalp reconcile fix deploy
- Swing live_trader reconcile: metty/execution/live_trader.py:_check_existing_position
- Bridge client: metty/bridge/client.py:fetch_deal_history_sync
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _make_swing_trader():
    """Construct a LiveTrader instance without running it."""
    from metty.execution.live_trader import LiveTrader
    os.environ.setdefault("MT5_BRIDGE_B_HOST", "localhost")
    os.environ.setdefault("MT5_BRIDGE_B_PORT", "8001")
    os.environ.setdefault("MT5_LOGIN_B", "1")
    os.environ.setdefault("MT5_PASSWORD_B", "x")
    t = LiveTrader(account="B", dry_run=True)
    return t


class TestSwingReconcileSkipsOnBridgeDown:
    """SWING-RECONCILE-1: when positions_get returns None (bridge down),
    swing live_trader MUST NOT reconcile — must hold off new trades and
    leave existing DB trades untouched (no PnL=0 false close)."""

    def test_trade_not_closed_when_bridge_returns_none(self, tmp_path, monkeypatch):
        """Causal test: an open swing trade in DB must remain is_open=1
        when MT5 bridge returns None. Without the fix, the trade gets
        marked closed_by_mt5_inferred PnL=0 (because reconcile ran with
        empty deals → entry_price fallback)."""
        import sqlite3
        from metty.core.db import init_db

        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO accounts (id, name, balance, leverage, bridge_host, "
            "bridge_port, signal_group) VALUES (2,'B',201.74,500,'h',5005,'volume')"
        )
        conn.execute(
            "INSERT INTO live_trades (id, account_id, timestamp, direction, "
            "symbol, entry_price, stop_loss, take_profit, lot_size, confidence, "
            "regime, session, d1_trend, reason, trading_mode, strategy_id, ticket, "
            "is_open) VALUES (50, 2, '2026-07-02T08:24:00+00:00', 'SELL', 'XAUUSD', "
            "4074.318, 4089.74, 4035.76, 0.01, 0.62, 'ranging', 'london', 'bearish', "
            "'swing trade should stay open', 'swing', 'swing-B', 3407249015, 1)"
        )
        conn.commit()
        conn.close()

        t = _make_swing_trader()
        monkeypatch.setattr(t, "db_path", db_path)
        monkeypatch.setattr(t, "account_id", 2)
        monkeypatch.setattr(t, "display_name", "B")

        # Mock rpyc to return None (bridge disconnected)
        mock_conn = MagicMock()
        mock_conn.root.positions_get.return_value = None
        monkeypatch.setattr("rpyc.connect", lambda *a, **k: mock_conn)

        # _get_deal_history should NEVER be called (we skip before that)
        # But if it is, return None (bridge down) to be safe.
        monkeypatch.setattr(t, "_get_deal_history", lambda days_back=7: None)

        result = t._check_existing_position()

        # Bridge down → must return True (hold off new trades, since DB has 1 open)
        assert result is True, (
            "bridge down + DB has open trades → must return True to hold off new trades. "
            f"Got {result}"
        )

        # Trade must remain is_open=1 in DB (no false close)
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT is_open, exit_reason, pnl FROM live_trades WHERE id=50"
        ).fetchone()
        conn.close()
        assert row[0] == 1, (
            f"swing trade must remain is_open=1 — bridge down must not close it. "
            f"Got is_open={row[0]}, exit_reason={row[1]}, pnl={row[2]}"
        )
        assert row[1] is None, (
            f"swing trade exit_reason must be NULL, got {row[1]}"
        )


class TestSwingReconcileStillClosesOnBridgeOk:
    """Sanity: when bridge is OK (positions_get returns []), reconcile still
    closes genuinely orphaned trades (those whose ticket is not in MT5 and
    deal history confirms closed). The bridge-down fix must NOT break this."""

    def test_orphaned_trade_closed_when_bridge_ok_empty_positions(self, tmp_path, monkeypatch):
        """When MT5 returns [] (bridge OK, no positions) and deal history
        shows the trade was closed, reconcile must close it in DB."""
        import sqlite3
        from metty.core.db import init_db

        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO accounts (id, name, balance, leverage, bridge_host, "
            "bridge_port, signal_group) VALUES (2,'B',201.74,500,'h',5005,'volume')"
        )
        conn.execute(
            "INSERT INTO live_trades (id, account_id, timestamp, direction, "
            "symbol, entry_price, stop_loss, take_profit, lot_size, confidence, "
            "regime, session, d1_trend, reason, trading_mode, strategy_id, ticket, "
            "is_open) VALUES (51, 2, '2026-07-02T08:00:00+00:00', 'SELL', 'XAUUSD', "
            "4074.318, 4089.74, 4035.76, 0.01, 0.62, 'ranging', 'london', 'bearish', "
            "'orphaned should close', 'swing', 'swing-B', 999, 1)"
        )
        conn.commit()
        conn.close()

        t = _make_swing_trader()
        monkeypatch.setattr(t, "db_path", db_path)
        monkeypatch.setattr(t, "account_id", 2)
        monkeypatch.setattr(t, "display_name", "B")

        # Bridge OK, returns [] (no positions)
        mock_conn = MagicMock()
        mock_conn.root.positions_get.return_value = []
        monkeypatch.setattr("rpyc.connect", lambda *a, **k: mock_conn)

        # Deal history: trade 999 was closed at TP
        deals = [{
            "ticket": 999,
            "entry": 4074.318,
            "price": 4035.76,
            "comment": "[tp]",
            "type": 0,  # DEAL_TYPE_BUY (closing a SELL)
        }]
        monkeypatch.setattr(t, "_get_deal_history", lambda days_back=7: deals)

        t._check_existing_position()

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT is_open, exit_reason FROM live_trades WHERE id=51"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == 0, (
            f"orphaned trade must be closed by reconcile (bridge OK). "
            f"Got is_open={row[0]}, exit_reason={row[1]}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])