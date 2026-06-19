"""Integration tests for _check_existing_position / _check_existing_*_position.

Tests the MT5-first position check logic shared by LiveTrader, ScalpTrader,
and M5ScalpTrader:

1. MT5 has position → returns True
2. MT5 has no position + DB has ghost trades → closes ghosts, returns False
3. MT5 has no position + DB empty → returns False
4. MT5 unreachable + DB has trades → falls back to DB, returns True
5. MT5 unreachable + DB empty → returns False
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Columns matching the real live_trades schema in metty/core/db.py
LIVE_TRADES_COLUMNS = """
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    direction TEXT NOT NULL,
    symbol TEXT NOT NULL DEFAULT 'XAUUSD',
    entry_price REAL NOT NULL,
    stop_loss REAL NOT NULL DEFAULT 0,
    take_profit REAL NOT NULL DEFAULT 0,
    lot_size REAL NOT NULL,
    confidence REAL NOT NULL DEFAULT 0,
    regime TEXT,
    session TEXT,
    d1_trend TEXT,
    reason TEXT,
    trading_mode TEXT NOT NULL DEFAULT 'swing',
    strategy_id TEXT NOT NULL DEFAULT '',
    signal_id INTEGER,
    ticket INTEGER,
    exit_price REAL,
    exit_time TEXT,
    pnl REAL,
    pnl_pct REAL,
    exit_reason TEXT,
    is_open INTEGER NOT NULL DEFAULT 1,
    mfe REAL,
    mae REAL,
    mfe_pct REAL,
    mae_pct REAL
"""


def _create_test_db(db_path: str, trades: list[dict] | None = None) -> None:
    """Create a test database with the real live_trades schema."""
    conn = sqlite3.connect(db_path)
    conn.execute(f"CREATE TABLE IF NOT EXISTS live_trades ({LIVE_TRADES_COLUMNS})")
    if trades:
        for t in trades:
            conn.execute(
                "INSERT INTO live_trades (account_id, timestamp, direction, symbol, "
                "entry_price, stop_loss, take_profit, lot_size, confidence, "
                "strategy_id, trading_mode, is_open, ticket) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    t.get("account_id", 1),
                    t.get("timestamp", datetime.now(timezone.utc).isoformat()),
                    t.get("direction", "BUY"),
                    t.get("symbol", "XAUUSD"),
                    t.get("entry_price", 2300.0),
                    t.get("stop_loss", 2290.0),
                    t.get("take_profit", 2320.0),
                    t.get("lot_size", 0.01),
                    t.get("confidence", 0.7),
                    t.get("strategy_id", "scalp"),
                    t.get("trading_mode", "scalp"),
                    t.get("is_open", 1),
                    t.get("ticket"),
                ),
            )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# LiveTrader._check_existing_position tests
# ---------------------------------------------------------------------------

class TestLiveTraderCheckExistingPosition:
    """Tests for LiveTrader._check_existing_position."""

    def _make_trader(self, db_path: str, account: str = "B"):
        from metty.execution.live_trader import LiveTrader
        return LiveTrader(account=account, dry_run=True, db_path=db_path)

    @patch("rpyc.connect")
    def test_mt5_has_position_returns_true(self, mock_connect, tmp_path):
        """When MT5 reports an open position, should return True."""
        db_path = str(tmp_path / "test.db")
        _create_test_db(db_path, trades=[
            {"account_id": 2, "direction": "BUY", "ticket": 12345, "strategy_id": "live"},
        ])
        trader = self._make_trader(db_path, account="B")

        mock_conn = MagicMock()
        mock_conn.root.positions_get.return_value = [
            {"ticket": 12345, "symbol": "XAUUSD", "type": 0}
        ]
        mock_connect.return_value = mock_conn

        assert trader._check_existing_position() is True

    @patch("rpyc.connect")
    def test_mt5_no_position_closes_ghost_trades(self, mock_connect, tmp_path):
        """When MT5 has no position but DB has ghost trades, close ghosts and return False."""
        db_path = str(tmp_path / "test.db")
        _create_test_db(db_path, trades=[
            {"account_id": 2, "direction": "BUY", "ticket": None, "strategy_id": "live"},
        ])
        trader = self._make_trader(db_path, account="B")

        mock_conn = MagicMock()
        mock_conn.root.positions_get.return_value = []
        mock_connect.return_value = mock_conn

        assert trader._check_existing_position() is False

        # Verify ghost trades were closed
        conn = sqlite3.connect(db_path)
        open_count = conn.execute(
            "SELECT COUNT(*) FROM live_trades WHERE is_open = 1"
        ).fetchone()[0]
        conn.close()
        assert open_count == 0

    @patch("rpyc.connect")
    def test_mt5_no_position_db_empty_returns_false(self, mock_connect, tmp_path):
        """When MT5 has no position and DB is empty, return False."""
        db_path = str(tmp_path / "test.db")
        _create_test_db(db_path)
        trader = self._make_trader(db_path)

        mock_conn = MagicMock()
        mock_conn.root.positions_get.return_value = []
        mock_connect.return_value = mock_conn

        assert trader._check_existing_position() is False

    @patch("rpyc.connect")
    def test_mt5_unreachable_db_has_trades_returns_true(self, mock_connect, tmp_path):
        """When MT5 is unreachable, fall back to DB and return True if trades exist."""
        db_path = str(tmp_path / "test.db")
        _create_test_db(db_path, trades=[
            {"account_id": 2, "direction": "BUY", "strategy_id": "live"},
        ])
        trader = self._make_trader(db_path, account="B")

        mock_connect.side_effect = Exception("Connection refused")

        assert trader._check_existing_position() is True

    @patch("rpyc.connect")
    def test_mt5_unreachable_db_empty_returns_false(self, mock_connect, tmp_path):
        """When MT5 is unreachable and DB is empty, return False."""
        db_path = str(tmp_path / "test.db")
        _create_test_db(db_path)
        trader = self._make_trader(db_path)

        mock_connect.side_effect = Exception("Connection refused")

        assert trader._check_existing_position() is False


# ---------------------------------------------------------------------------
# ScalpTrader._check_existing_scalp_position tests
# ---------------------------------------------------------------------------

class TestScalpTraderCheckExistingPosition:
    """Tests for ScalpTrader._check_existing_scalp_position."""

    def _make_trader(self, db_path: str, account: str = "B"):
        from metty.execution.scalp_trader import ScalpTrader
        return ScalpTrader(account=account, dry_run=True, db_path=db_path)

    @patch("rpyc.connect")
    def test_mt5_has_position_returns_true(self, mock_connect, tmp_path):
        """When MT5 has position and DB has matching scalp trade, return True."""
        db_path = str(tmp_path / "test.db")
        _create_test_db(db_path, trades=[
            {"account_id": 2, "direction": "BUY", "ticket": 12345,
             "strategy_id": "scalp", "trading_mode": "scalp"},
        ])
        trader = self._make_trader(db_path, account="B")

        mock_conn = MagicMock()
        mock_conn.root.positions_get.return_value = [
            {"ticket": 12345, "symbol": "XAUUSD", "type": 0}
        ]
        mock_connect.return_value = mock_conn

        assert trader._check_existing_scalp_position() is True

    @patch("rpyc.connect")
    def test_mt5_no_position_closes_ghost_trades(self, mock_connect, tmp_path):
        db_path = str(tmp_path / "test.db")
        _create_test_db(db_path, trades=[
            {"account_id": 2, "direction": "BUY", "ticket": None,
             "strategy_id": "scalp", "trading_mode": "scalp"},
        ])
        trader = self._make_trader(db_path, account="B")

        mock_conn = MagicMock()
        mock_conn.root.positions_get.return_value = []
        mock_connect.return_value = mock_conn

        assert trader._check_existing_scalp_position() is False

    @patch("rpyc.connect")
    def test_mt5_unreachable_db_has_scalp_trades(self, mock_connect, tmp_path):
        db_path = str(tmp_path / "test.db")
        _create_test_db(db_path, trades=[
            {"account_id": 2, "direction": "BUY", "strategy_id": "scalp"},
        ])
        trader = self._make_trader(db_path, account="B")

        mock_connect.side_effect = Exception("Connection refused")

        assert trader._check_existing_scalp_position() is True

    @patch("rpyc.connect")
    def test_mt5_unreachable_db_empty(self, mock_connect, tmp_path):
        db_path = str(tmp_path / "test.db")
        _create_test_db(db_path)
        trader = self._make_trader(db_path)

        mock_connect.side_effect = Exception("Connection refused")

        assert trader._check_existing_scalp_position() is False


# ---------------------------------------------------------------------------
# M5ScalpTrader._check_existing_m5_scalp_position tests
# ---------------------------------------------------------------------------

class TestM5ScalpTraderCheckExistingPosition:
    """Tests for M5ScalpTrader._check_existing_m5_scalp_position."""

    def _make_trader(self, db_path: str, account: str = "C"):
        from metty.execution.m5_scalp_trader import M5ScalpTrader
        return M5ScalpTrader(account=account, dry_run=True, db_path=db_path)

    @patch("rpyc.connect")
    def test_mt5_has_position_returns_true(self, mock_connect, tmp_path):
        """When MT5 has position and DB has matching m5_scalp trade, return True."""
        db_path = str(tmp_path / "test.db")
        _create_test_db(db_path, trades=[
            {"account_id": 3, "direction": "BUY", "ticket": 12345,
             "strategy_id": "m5_scalp", "trading_mode": "m5_scalp"},
        ])
        trader = self._make_trader(db_path, account="C")

        mock_conn = MagicMock()
        mock_conn.root.positions_get.return_value = [
            {"ticket": 12345, "symbol": "XAUUSD", "type": 0}
        ]
        mock_connect.return_value = mock_conn

        assert trader._check_existing_m5_scalp_position() is True

    @patch("rpyc.connect")
    def test_mt5_no_position_closes_ghost_trades(self, mock_connect, tmp_path):
        db_path = str(tmp_path / "test.db")
        _create_test_db(db_path, trades=[
            {"account_id": 3, "direction": "BUY", "ticket": None,
             "strategy_id": "m5_scalp", "trading_mode": "m5_scalp"},
        ])
        trader = self._make_trader(db_path, account="C")

        mock_conn = MagicMock()
        mock_conn.root.positions_get.return_value = []
        mock_connect.return_value = mock_conn

        assert trader._check_existing_m5_scalp_position() is False

        # Verify ghost trades were closed
        conn = sqlite3.connect(db_path)
        open_count = conn.execute(
            "SELECT COUNT(*) FROM live_trades WHERE is_open = 1"
        ).fetchone()[0]
        conn.close()
        assert open_count == 0

    @patch("rpyc.connect")
    def test_mt5_unreachable_db_has_m5_scalp_trades(self, mock_connect, tmp_path):
        db_path = str(tmp_path / "test.db")
        _create_test_db(db_path, trades=[
            {"account_id": 3, "direction": "BUY",
             "strategy_id": "m5_scalp", "trading_mode": "m5_scalp"},
        ])
        trader = self._make_trader(db_path, account="C")

        mock_connect.side_effect = Exception("Connection refused")

        assert trader._check_existing_m5_scalp_position() is True

    @patch("rpyc.connect")
    def test_mt5_unreachable_db_empty(self, mock_connect, tmp_path):
        db_path = str(tmp_path / "test.db")
        _create_test_db(db_path)
        trader = self._make_trader(db_path)

        mock_connect.side_effect = Exception("Connection refused")

        assert trader._check_existing_m5_scalp_position() is False