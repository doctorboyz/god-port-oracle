"""Causal proof tests for raw rpyc missing initialize() bug (2026-07-07, ISSUE-078).

Hypothesis
----------
Both swing trader (live_trader.py:_check_existing_position) and m5 scalp
trader (m5_scalp_trader.py:_check_existing_m5_scalp_position) use raw
`rpyc.connect()` + `conn.root.positions_get()` WITHOUT first calling
`conn.root.initialize()`. The MT5Bridge wrapper used by live_collector's
`fetch_candles_sync` calls `initialize()` then `shutdown()` each cycle, so
the MT5 terminal in Wine flips between initialized and shutdown states.

When traders call `positions_get()` while the terminal is in shutdown
state, MT5 returns None (not []). The traders treat None as "bridge
disconnected" and hold off new trades — but actually the bridge is fine,
just not initialized. This creates a race condition where timing decides
whether trades happen.

Root cause
----------
Raw rpyc code paths skip `initialize()` before `positions_get()`. MT5
terminal requires `initialize()` to be called before any query works.
Without it, ALL queries (positions_get, account_info) return None.

Fix
---
Call `conn.root.initialize()` before `conn.root.positions_get()` in
both raw rpyc code paths. Use a try/finally to ensure `shutdown()` is
called to leave the terminal in a clean state for the next caller.

References
----------
- Bug found 2026-07-07 during Real-A post-deploy verification
- Verification: calling `initialize()` on a shutdown terminal made
  `account_info()` return real data (login=102246409, balance=$387.80)
  and `positions_get()` return a list instead of None
- Affected code:
  - metty/execution/live_trader.py:803 (swing _check_existing_position)
  - metty/execution/m5_scalp_trader.py:523 (m5 _check_existing_m5_scalp_position)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# ---------- test helpers ----------


class _FakeMt5Root:
    """Mock MT5 bridge root that mimics Wine terminal state.

    `positions_get` returns None when not initialized (matches real MT5
    behavior — terminal returns None for all queries when shutdown).
    `positions_get` returns [] when initialized (matches real MT5 —
    broker reachable, no open positions).

    Tracks call order so the test can assert initialize() is called
    before positions_get().
    """

    def __init__(self):
        self._initialized = False
        self.call_order: list[str] = []

    def initialize(self):
        self.call_order.append("initialize")
        self._initialized = True
        return True

    def last_error(self):
        return ""

    def positions_get(self, symbol=None, ticket=None):
        self.call_order.append("positions_get")
        if not self._initialized:
            return None  # terminal shutdown — matches real MT5 behavior
        return []  # terminal up, no positions

    def shutdown(self):
        self.call_order.append("shutdown")
        self._initialized = False


def _make_swing_trader():
    """Construct a LiveTrader instance without running it."""
    from metty.execution.live_trader import LiveTrader
    os.environ.setdefault("MT5_BRIDGE_A_HOST", "localhost")
    os.environ.setdefault("MT5_BRIDGE_A_PORT", "8001")
    os.environ.setdefault("MT5_LOGIN_A", "1")
    os.environ.setdefault("MT5_PASSWORD_A", "x")
    os.environ.setdefault("MT5_SERVER_A", "Exness-MT5Real15")
    return LiveTrader(account="A", dry_run=True)


def _make_m5_scalp_trader():
    """Construct an M5ScalpTrader instance without running it."""
    from metty.execution.m5_scalp_trader import M5ScalpTrader
    os.environ.setdefault("MT5_BRIDGE_A_HOST", "localhost")
    os.environ.setdefault("MT5_BRIDGE_A_PORT", "8001")
    os.environ.setdefault("MT5_LOGIN_A", "1")
    os.environ.setdefault("MT5_PASSWORD_A", "x")
    os.environ.setdefault("MT5_SERVER_A", "Exness-MT5Real15")
    return M5ScalpTrader(account="A", dry_run=True)


# ---------- swing trader tests ----------


class TestSwingCheckExistingPositionInitializes:
    """Swing trader's _check_existing_position must call initialize() on
    the raw rpyc connection BEFORE calling positions_get(). Without it,
    the MT5 terminal in Wine returns None (terminal shutdown state) and
    the trader falsely logs 'bridge disconnected'."""

    def test_initialize_called_before_positions_get(self, tmp_path, monkeypatch):
        import sqlite3
        import rpyc
        from metty.core.db import init_db

        # Setup DB with no open trades
        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO accounts (id, name, broker_login, broker_server, "
            "balance, leverage, bridge_host, bridge_port, signal_group) "
            "VALUES (1,'A','102246409','Exness-MT5Real15',400.0,500,'mt5a',8001,'volume')"
        )
        conn.commit()
        conn.close()

        t = _make_swing_trader()
        monkeypatch.setattr(t, "db_path", db_path)
        monkeypatch.setattr(t, "account_id", 1)
        monkeypatch.setattr(t, "display_name", "A")
        monkeypatch.setattr(t, "dry_run", True)

        # Mock rpyc.connect to return our fake connection
        fake_root = _FakeMt5Root()
        fake_conn = MagicMock()
        fake_conn.root = fake_root
        monkeypatch.setattr(rpyc, "connect", lambda *a, **kw: fake_conn)

        # Run _check_existing_position
        result = t._check_existing_position()

        # Causal assertion: initialize must be called before positions_get
        assert "initialize" in fake_root.call_order, (
            "Swing trader must call conn.root.initialize() before positions_get(). "
            "Without initialize, MT5 terminal in Wine returns None for all queries "
            "(positions_get, account_info) — making the trader think the bridge is "
            "disconnected when actually it's just not initialized. This is the bug."
        )
        init_idx = fake_root.call_order.index("initialize")
        pos_idx = fake_root.call_order.index("positions_get")
        assert init_idx < pos_idx, (
            f"initialize() must be called BEFORE positions_get(). "
            f"Got order: {fake_root.call_order} — initialize at {init_idx}, "
            f"positions_get at {pos_idx}. Calling positions_get before initialize "
            f"returns None (terminal shutdown) — false 'bridge disconnected'."
        )

        # With no positions and no open trades, result should be False
        assert result is False, (
            f"With MT5 terminal properly initialized, positions_get returns [] "
            f"(no positions) and DB has no open trades → result should be False. "
            f"Got {result}"
        )


# ---------- m5 scalp trader tests ----------


class TestM5ScalpCheckExistingPositionInitializes:
    """M5 scalp trader's _check_existing_m5_scalp_position must call
    initialize() on the raw rpyc connection BEFORE calling positions_get().
    Same bug as swing trader — same fix."""

    def test_initialize_called_before_positions_get(self, tmp_path, monkeypatch):
        import sqlite3
        import rpyc
        from metty.core.db import init_db

        # Setup DB with no open trades
        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO accounts (id, name, broker_login, broker_server, "
            "balance, leverage, bridge_host, bridge_port, signal_group) "
            "VALUES (1,'A','102246409','Exness-MT5Real15',400.0,500,'mt5a',8001,'volume')"
        )
        conn.commit()
        conn.close()

        t = _make_m5_scalp_trader()
        monkeypatch.setattr(t, "db_path", db_path)
        monkeypatch.setattr(t, "account_id", 1)
        monkeypatch.setattr(t, "display_name", "A")
        monkeypatch.setattr(t, "dry_run", True)

        # Mock rpyc.connect to return our fake connection
        fake_root = _FakeMt5Root()
        fake_conn = MagicMock()
        fake_conn.root = fake_root
        monkeypatch.setattr(rpyc, "connect", lambda *a, **kw: fake_conn)

        # Run _check_existing_m5_scalp_position
        result = t._check_existing_m5_scalp_position()

        # Causal assertion: initialize must be called before positions_get
        assert "initialize" in fake_root.call_order, (
            "M5 scalp trader must call conn.root.initialize() before positions_get(). "
            "Without initialize, MT5 terminal in Wine returns None for all queries "
            "— making the trader think the bridge is disconnected when actually it's "
            "just not initialized. Same bug as swing trader (ISSUE-078)."
        )
        init_idx = fake_root.call_order.index("initialize")
        pos_idx = fake_root.call_order.index("positions_get")
        assert init_idx < pos_idx, (
            f"initialize() must be called BEFORE positions_get(). "
            f"Got order: {fake_root.call_order} — initialize at {init_idx}, "
            f"positions_get at {pos_idx}. Calling positions_get before initialize "
            f"returns None (terminal shutdown) — false 'bridge disconnected'."
        )

        # With no positions and no open trades, result should be False
        assert result is False, (
            f"With MT5 terminal properly initialized, positions_get returns [] "
            f"(no positions) and DB has no open trades → result should be False. "
            f"Got {result}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])