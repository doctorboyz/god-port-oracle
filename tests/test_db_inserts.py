"""Integration tests for metty.core.db — validates INSERT column/value counts.

Catches the bug where INSERT INTO live_trades had 30 columns but only 29 values.
"""
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def db_path(tmp_path):
    """Create a fresh test database with all migrations and a test account."""
    db_file = tmp_path / "test_trading.db"
    from metty.core.db import init_db, get_connection
    init_db(db_file)

    # Create test accounts so foreign keys work
    # accounts table: id, name, broker_login, broker_server, balance, leverage,
    #                 bridge_host, bridge_port, signal_group, is_active, created_at
    conn = get_connection(db_file)
    conn.execute(
        "INSERT OR IGNORE INTO accounts "
        "(id, name, broker_login, broker_server, balance, leverage, "
        "bridge_host, bridge_port, signal_group) "
        "VALUES (1, 'TestA', '12345', 'Exness-MT5', 1000.0, 2000, "
        "'localhost', 18812, 'swing')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO accounts "
        "(id, name, broker_login, broker_server, balance, leverage, "
        "bridge_host, bridge_port, signal_group) "
        "VALUES (2, 'TestB', '12346', 'Exness-MT5', 1000.0, 2000, "
        "'localhost', 18813, 'scalp')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO accounts "
        "(id, name, broker_login, broker_server, balance, leverage, "
        "bridge_host, bridge_port, signal_group) "
        "VALUES (3, 'TestC', '12347', 'Exness-MT5', 1000.0, 2000, "
        "'localhost', 18814, 'm5_scalp')"
    )
    conn.commit()
    conn.close()

    return db_file


class TestInsertLiveTrade:
    """Verify insert_live_trade writes all expected columns correctly."""

    def test_insert_live_trade_all_columns(self, db_path):
        """Insert a live trade with all columns populated and verify each one."""
        from metty.core.db import insert_live_trade, get_connection

        trade_id = insert_live_trade(
            account_id=1,
            timestamp="2026-06-04T00:00:00+00:00",
            direction="BUY",
            symbol="XAUUSD",
            entry_price=4450.0,
            stop_loss=4440.0,
            take_profit=4470.0,
            lot_size=0.01,
            confidence=0.75,
            regime="trending",
            session="london",
            d1_trend="bullish",
            reason="ML signal",
            ticket=12345,
            trading_mode="swing",
            strategy_id="test-strat",
            signal_id=99,
            # New columns
            spread_at_entry=0.35,
            slippage=0.05,
            atr_at_entry=5.2,
            ml_risk_multiplier=0.8,
            ml_risk_reason="ML risk: P(LOSS)=65%, 80% size",
            ml_model_version="v2.best",
            ml_loss_proba=0.65,
            ml_model_used="trending_BUY",
            minutes_to_next_event=45,
            next_event_type="NFP",
            next_event_impact="high",
            indicator_scores_json='{"adx": 0.5, "macd": -1.0}',
            db_path=db_path,
        )

        assert trade_id is not None
        assert trade_id > 0

        # Read back all columns and verify
        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT * FROM live_trades WHERE id = ?", (trade_id,)
        ).fetchone()

        # Get column names from cursor description
        cursor = conn.execute("SELECT * FROM live_trades WHERE id = ?", (trade_id,))
        col_names = [desc[0] for desc in cursor.description]
        row_dict = dict(zip(col_names, row))

        # Verify all columns have expected values
        assert row_dict["account_id"] == 1
        assert row_dict["direction"] == "BUY"
        assert row_dict["symbol"] == "XAUUSD"
        assert row_dict["entry_price"] == 4450.0
        assert row_dict["stop_loss"] == 4440.0
        assert row_dict["take_profit"] == 4470.0
        assert row_dict["lot_size"] == 0.01
        assert row_dict["confidence"] == 0.75
        assert row_dict["regime"] == "trending"
        assert row_dict["session"] == "london"
        assert row_dict["d1_trend"] == "bullish"
        assert row_dict["reason"] == "ML signal"
        assert row_dict["ticket"] == 12345
        assert row_dict["is_open"] == 1
        assert row_dict["trading_mode"] == "swing"
        assert row_dict["strategy_id"] == "test-strat"
        assert row_dict["signal_id"] == 99
        # New columns
        assert row_dict["spread_at_entry"] == 0.35
        assert row_dict["slippage"] == 0.05
        assert row_dict["atr_at_entry"] == 5.2
        assert row_dict["ml_risk_multiplier"] == 0.8
        assert row_dict["ml_risk_reason"] == "ML risk: P(LOSS)=65%, 80% size"
        assert row_dict["ml_model_version"] == "v2.best"
        assert row_dict["ml_loss_proba"] == 0.65
        assert row_dict["ml_model_used"] == "trending_BUY"
        assert row_dict["minutes_to_next_event"] == 45
        assert row_dict["next_event_type"] == "NFP"
        assert row_dict["next_event_impact"] == "high"
        assert row_dict["indicator_scores_json"] == '{"adx": 0.5, "macd": -1.0}'

        conn.close()

    def test_insert_live_trade_minimal_columns(self, db_path):
        """Insert a live trade with only required columns — NULLs for optional ones."""
        from metty.core.db import insert_live_trade, get_connection

        trade_id = insert_live_trade(
            account_id=2,
            timestamp="2026-06-04T01:00:00+00:00",
            direction="SELL",
            entry_price=4460.0,
            db_path=db_path,
        )

        assert trade_id > 0

        cursor = get_connection(db_path).execute(
            "SELECT * FROM live_trades WHERE id = ?", (trade_id,)
        )
        col_names = [desc[0] for desc in cursor.description]
        row = cursor.fetchone()
        row_dict = dict(zip(col_names, row))

        assert row_dict["direction"] == "SELL"
        assert row_dict["entry_price"] == 4460.0
        assert row_dict["spread_at_entry"] is None
        assert row_dict["ml_risk_multiplier"] is None
        assert row_dict["indicator_scores_json"] is None

    def test_close_live_trade_with_exit_columns(self, db_path):
        """Close a trade with MFE/MAE and exit regime data."""
        from metty.core.db import insert_live_trade, close_live_trade, get_connection

        trade_id = insert_live_trade(
            account_id=3,
            timestamp="2026-06-04T02:00:00+00:00",
            direction="BUY",
            entry_price=4470.0,
            db_path=db_path,
        )

        close_live_trade(
            trade_id=trade_id,
            exit_price=4480.0,
            exit_time="2026-06-04T03:00:00+00:00",
            pnl=10.0,
            pnl_pct=0.22,
            exit_reason="tp_hit",
            db_path=db_path,
            mfe=15.0,
            mae=5.0,
            mfe_pct=0.34,
            mae_pct=0.11,
            exit_regime="trending",
            exit_d1_trend="bullish",
            exit_h4_trend="bullish",
        )

        cursor = get_connection(db_path).execute(
            "SELECT * FROM live_trades WHERE id = ?", (trade_id,)
        )
        col_names = [desc[0] for desc in cursor.description]
        row = cursor.fetchone()
        row_dict = dict(zip(col_names, row))

        assert row_dict["is_open"] == 0
        assert row_dict["exit_price"] == 4480.0
        assert row_dict["pnl"] == 10.0
        assert row_dict["pnl_pct"] == 0.22
        assert row_dict["exit_reason"] == "tp_hit"
        assert row_dict["mfe"] == 15.0
        assert row_dict["mae"] == 5.0
        assert row_dict["mfe_pct"] == 0.34
        assert row_dict["mae_pct"] == 0.11
        assert row_dict["exit_regime"] == "trending"
        assert row_dict["exit_d1_trend"] == "bullish"
        assert row_dict["exit_h4_trend"] == "bullish"

    def test_insert_rejected_signal(self, db_path):
        """Verify rejected signal recording."""
        from metty.core.db import insert_rejected_signal, get_connection

        insert_rejected_signal(
            account_id=1,
            timestamp="2026-06-04T03:00:00+00:00",
            direction="BUY",
            confidence=0.3,
            price=4450.0,
            rejection_reason="ml_filter:P(LOSS)=85%",
            trading_mode="swing",
            strategy_id="test",
            regime="trending",
            session="london",
            d1_trend="bullish",
            db_path=db_path,
        )

        cursor = get_connection(db_path).execute(
            "SELECT * FROM rejected_signals WHERE account_id = 1"
        )
        row = cursor.fetchone()
        assert row is not None


class TestSQLColumnIntegrity:
    """Verify all INSERT statements have matching column/value counts.

    This test would have caught the '29 values for 30 columns' bug.
    """

    def test_all_inserts_column_value_match(self):
        """Parse db.py and verify every INSERT has matching column and value counts."""
        import re

        db_file = Path(__file__).resolve().parent.parent / "metty" / "core" / "db.py"
        content = db_file.read_text()

        # Find all INSERT INTO ... (columns) VALUES (...) with triple-quoted strings
        inserts = list(re.finditer(
            r'INSERT\s+INTO\s+(\w+)\s*\(([^)]+)\)\s*VALUES\s*\((.+?)\)\s*\"\"\",\s*\(',
            content, re.DOTALL,
        ))

        errors = []
        for m in inserts:
            table = m.group(1)
            cols_text = m.group(2)
            vals_text = m.group(3)

            if "{" in vals_text:
                continue  # Dynamic INSERT, skip

            cols = [c.strip() for c in cols_text.split(",") if c.strip()]
            n_cols = len(cols)

            # Count ? placeholders
            q_marks = vals_text.count("?")

            # Count function calls like datetime('now')
            func_calls = len(re.findall(r"\w+\([^)]*\)", vals_text))

            # Count remaining literals
            vals_no_funcs = re.sub(r"\w+\([^)]*\)", "FUNC_CALL", vals_text)
            literals = len(re.findall(r"\b(?:\d+|None|True|False|NULL)\b|'[^']*'", vals_no_funcs))

            n_vals = q_marks + func_calls + literals

            if n_cols != n_vals:
                errors.append(
                    f"{table}: {n_cols} columns but {n_vals} values "
                    f"({q_marks}? + {func_calls} funcs + {literals} literals)"
                )

        assert len(errors) == 0, (
            f"INSERT column/value mismatch:\n" + "\n".join(errors)
        )