"""Causal proof tests for live_trader external-close reconcile bug (2026-07-06).

Hypothesis
----------
On Real-A after 2026-07-03 no-ML deploy, a BUY trade (ticket 2715202701)
opened at $4170.26 was closed by broker-side SL at $4158.06 (loss -$12.20).
DB still shows is_open=1 because `_monitor_positions` calls
`_close_mt5_position_with_fill`, which returns `(False, None)` when
`positions_get(ticket)` returns empty (position already gone). The trader
logs "MT5 close failed — leaving DB open, will retry next cycle" and
`continue`s, so DB never closes. M5 scalp then refuses to trade because
it sees "1 open in DB".

Root cause
----------
`_close_mt5_position_with_fill` and its callers conflate two distinct cases:
1. Position no longer exists in MT5 (broker closed it via SL/TP/manual)
   → DB should be reconciled to is_open=0 using deal history's actual fill
2. MT5 connection / order_send failed
   → should retry next cycle (current behavior)

Treating case 1 as case 2 leaves ghost trades that block all new entries.

Fix
---
When `_close_mt5_position_with_fill` returns `(False, None)` for a ticket,
call new helper `_reconcile_external_close(ticket, ...)` which queries deal
history and matches the closing deal (Strategy 0: deal.order == ticket
AND deal.type == closing_type; Strategy 1: price near SL/TP + direction
match; Strategy 2: position_id field). If a closing deal is found, use
its price as the actual fill and proceed to close DB. If not found,
keep the "retry next cycle" behavior.

References
----------
- Bug found 2026-07-06 after Real-A no-ML deploy
- Trade #5537 ticket 2715202701 closed by SL on 2026-07-03 14:22 UTC
  but DB still is_open=1 as of 2026-07-06
- Affected code: metty/execution/live_trader.py:_monitor_positions (line 1126)
  and _execute_tp1_close (line 1248)
- Pattern: same as ISSUE-038/049 ghost trades, but in _monitor_positions path
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
    os.environ.setdefault("MT5_BRIDGE_A_HOST", "localhost")
    os.environ.setdefault("MT5_BRIDGE_A_PORT", "8001")
    os.environ.setdefault("MT5_LOGIN_A", "1")
    os.environ.setdefault("MT5_PASSWORD_A", "x")
    os.environ.setdefault("MT5_SERVER_A", "Exness-MT5Real15")
    t = LiveTrader(account="A", dry_run=True)
    return t


class TestReconcileExternalClose:
    """When broker has already closed the position (SL/TP hit on broker
    side), `_monitor_positions` must reconcile DB to is_open=0 using the
    deal history's actual fill price — NOT leave DB open as a ghost."""

    def _setup_db(self, tmp_path, ticket=2715202701):
        import sqlite3
        from metty.core.db import init_db

        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO accounts (id, name, broker_login, broker_server, "
            "balance, leverage, bridge_host, bridge_port, signal_group) "
            "VALUES (1,'A','102246409','Exness-MT5Real15',400.0,500,'mt5a',8001,'volume')"
        )
        # Open BUY trade — SL hit detected locally but broker already closed it
        conn.execute(
            "INSERT INTO live_trades (id, account_id, timestamp, direction, "
            "symbol, entry_price, stop_loss, take_profit, lot_size, confidence, "
            "regime, session, d1_trend, reason, trading_mode, strategy_id, ticket, "
            "is_open, atr_multiplier, rr_ratio, min_confidence_threshold) "
            "VALUES (5537, 1, '2026-07-03T13:15:00+00:00', 'BUY', 'XAUUSD', "
            "4170.257, 4158.10, 4200.65, 0.01, 0.715, 'ranging', 'overlap', 'bearish', "
            "'reversal BUY', 'swing', 'swing-A', ?, 1, 2.5, 2.5, 0.45)",
            (ticket,),
        )
        conn.commit()
        conn.close()
        return db_path

    def test_ghost_reconciled_when_position_already_closed_by_broker(self, tmp_path, monkeypatch):
        """Causal test: trade where MT5 says position not found AND deal
        history shows closing deal → DB must be marked is_open=0 with
        actual broker fill price (not left as ghost)."""
        import sqlite3

        db_path = self._setup_db(tmp_path)
        t = _make_swing_trader()
        monkeypatch.setattr(t, "db_path", db_path)
        monkeypatch.setattr(t, "account_id", 1)
        monkeypatch.setattr(t, "display_name", "A")
        monkeypatch.setattr(t, "dry_run", False)

        # Mock _close_mt5_position_with_fill: sync method returns (False, None)
        # — the bug case where MT5 says position not found (already closed)
        def fake_close_with_fill(ticket):
            return False, None  # position not found → triggers reconcile path
        monkeypatch.setattr(t, "_close_mt5_position_with_fill", fake_close_with_fill)

        # Deal history: closing deal found via Strategy 0 (order == ticket)
        # MT5 closing SELL deal for BUY position
        deals = [
            {  # open deal
                "ticket": 1447349455, "order": 2715202701, "time": 1783084618,
                "type": 0, "reason": 3, "volume": 0.01, "price": 4170.257,
                "profit": 0.0, "symbol": "XAUUSDm", "comment": "god-port-A",
            },
            {  # close deal — SL hit, SELL closing a BUY
                "ticket": 1447389174, "order": 0, "time": 1783087735,
                "type": 1, "reason": 4, "volume": 0.01, "price": 4158.06,
                "profit": -12.20, "symbol": "XAUUSDm", "comment": "[sl 4158.06000]",
            },
        ]
        monkeypatch.setattr(t, "_get_deal_history", lambda days_back=7: deals)

        # Mock equity getter (DP record needs it)
        monkeypatch.setattr(t, "_get_equity", lambda: 387.80)

        # Build candle data that triggers SL hit (price below SL)
        import pandas as pd
        from datetime import datetime, timezone
        ts = datetime(2026, 7, 3, 14, 22, tzinfo=timezone.utc)
        m5 = pd.DataFrame(
            {"open": [4170.0], "high": [4172.0], "low": [4157.0], "low_price": [4157.0],
             "close": [4158.0], "volume": [100]},
            index=pd.DatetimeIndex([ts], name="timestamp"),
        )
        candles = {"M5": m5}

        closed = t._monitor_positions(candles)

        # Verify: trade was closed in DB (NOT left as ghost)
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT is_open, exit_price, pnl, exit_reason FROM live_trades WHERE id=5537"
        ).fetchone()
        conn.close()

        assert row[0] == 0, (
            f" externally-closed position must be reconciled to is_open=0. "
            f"Got is_open={row[0]} (ghost). This is the bug — _monitor_positions "
            f"treats 'position not found' as 'MT5 close failed' and skips DB close."
        )
        assert row[1] == pytest.approx(4158.06, abs=0.01), (
            f"exit_price must be the broker's actual SL fill ($4158.06 from deal history), "
            f"not theoretical SL ($4158.10) or entry_price fallback. Got {row[1]}"
        )
        assert row[2] == pytest.approx(-12.20, abs=0.01), (
            f"pnl must reflect actual broker close (-$12.20), got {row[2]}"
        )
        assert row[3] is not None, (
            f"exit_reason must be set, got None"
        )

    def test_ghost_remains_when_no_closing_deal_found(self, tmp_path, monkeypatch):
        """Sanity: if position not found AND no closing deal in history
        (e.g. bridge connection error during close) — must keep current
        retry behavior so we don't false-close with wrong PnL."""
        import sqlite3

        db_path = self._setup_db(tmp_path)
        t = _make_swing_trader()
        monkeypatch.setattr(t, "db_path", db_path)
        monkeypatch.setattr(t, "account_id", 1)
        monkeypatch.setattr(t, "display_name", "A")
        monkeypatch.setattr(t, "dry_run", False)

        def fake_close_with_fill(ticket):
            return False, None
        monkeypatch.setattr(t, "_close_mt5_position_with_fill", fake_close_with_fill)

        # No deals returned (bridge OK, but no closing deal for this ticket)
        monkeypatch.setattr(t, "_get_deal_history", lambda days_back=7: [])
        monkeypatch.setattr(t, "_get_equity", lambda: 400.0)

        import pandas as pd
        from datetime import datetime, timezone
        ts = datetime(2026, 7, 3, 14, 22, tzinfo=timezone.utc)
        m5 = pd.DataFrame(
            {"open": [4170.0], "high": [4172.0], "low": [4157.0], "low_price": [4157.0],
             "close": [4158.0], "volume": [100]},
            index=pd.DatetimeIndex([ts], name="timestamp"),
        )
        candles = {"M5": m5}

        closed = t._monitor_positions(candles)

        # Verify: trade STAYS open (no false close with wrong PnL)
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT is_open, exit_price FROM live_trades WHERE id=5537"
        ).fetchone()
        conn.close()

        assert row[0] == 1, (
            f"when no closing deal found, trade must stay is_open=1 (retry next cycle). "
            f"Got is_open={row[0]} — false close with wrong PnL would be worse than ghost."
        )
        assert row[1] is None, (
            f"exit_price must be NULL (no close happened), got {row[1]}"
        )

    def test_bridge_failure_does_not_false_close(self, tmp_path, monkeypatch):
        """Sanity: if deal history fetch fails (bridge down, returns None),
        must NOT close DB — fall through to retry. Same as bridge-down case
        in test_live_trader_reconcile_bridge_down.py."""
        import sqlite3

        db_path = self._setup_db(tmp_path)
        t = _make_swing_trader()
        monkeypatch.setattr(t, "db_path", db_path)
        monkeypatch.setattr(t, "account_id", 1)
        monkeypatch.setattr(t, "display_name", "A")
        monkeypatch.setattr(t, "dry_run", False)

        def fake_close_with_fill(ticket):
            return False, None
        monkeypatch.setattr(t, "_close_mt5_position_with_fill", fake_close_with_fill)

        # Bridge down → _get_deal_history returns None
        monkeypatch.setattr(t, "_get_deal_history", lambda days_back=7: None)
        monkeypatch.setattr(t, "_get_equity", lambda: 400.0)

        import pandas as pd
        from datetime import datetime, timezone
        ts = datetime(2026, 7, 3, 14, 22, tzinfo=timezone.utc)
        m5 = pd.DataFrame(
            {"open": [4170.0], "high": [4172.0], "low": [4157.0], "low_price": [4157.0],
             "close": [4158.0], "volume": [100]},
            index=pd.DatetimeIndex([ts], name="timestamp"),
        )
        candles = {"M5": m5}

        closed = t._monitor_positions(candles)

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT is_open FROM live_trades WHERE id=5537"
        ).fetchone()
        conn.close()

        assert row[0] == 1, (
            f"bridge down → deal history None → must NOT false-close. "
            f"Got is_open={row[0]}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])