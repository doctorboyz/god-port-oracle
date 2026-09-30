"""Causal proof test: paper equity fallback when no MT5 bridge exists.

Hypothesis
----------
run_once skips every cycle when _get_equity() returns None (live_trader.py
"equity unavailable — skipping cycle"). Without an MT5 bridge, _get_equity
ALWAYS returns None — so a brokerless paper farm (Mac mini, CSV feed only)
can never open a single trade: every cycle dies before the gates.

In dry_run the equity is fully knowable from the DB: initial equity +
closed PnL + floating PnL of open positions marked at the latest M5 close.
The floating PnL must use the SAME formula as _monitor_positions closes
((exit-entry)*lot*100 BUY / (entry-exit)*lot*100 SELL).

Causal proof
------------
Bridge down (fetch_account_info_sync → None) + dry_run → _get_equity
returns initial + closed pnl + floating. Control: dry_run=False → None
(live path unchanged — never fabricate equity for real money).

This test FAILS (RED) before the fix, PASSES (GREEN) after.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from metty.core.db import (  # noqa: E402
    get_account_id_by_name,
    init_db,
    insert_account,
    insert_live_trade,
)
from metty.execution.live_trader import RiskConfig  # noqa: E402

_TS = datetime.now(timezone.utc).isoformat()


@pytest.fixture()
def p11_db(tmp_path):
    db = tmp_path / "equity_test.db"
    init_db(db)
    insert_account(
        name="P11", balance=100.0, leverage=2000,
        bridge_host="none", bridge_port=8001, signal_group="farm",
        db_path=db,
    )
    return db


def _make_trader(monkeypatch, db, dry_run=True, last_close=None):
    """LiveTrader P11 with the bridge forced down (farm condition)."""
    monkeypatch.setenv("INITIAL_EQUITY_P11", "100")
    from metty.execution.live_trader import LiveTrader
    t = LiveTrader(account="P11", db_path=db, dry_run=dry_run,
                   risk_config=RiskConfig(risk_per_trade=0.01))
    t._last_close_price = last_close
    # Simulate bridge down: fetch_account_info_sync returns None (no raise —
    # the real bridge returns None on failure, which _get_equity translates
    # into equity=None).
    from metty.bridge.client import MT5Bridge
    monkeypatch.setattr(MT5Bridge, "fetch_account_info_sync", lambda self: None)
    return t


def _acct_id(db):
    return get_account_id_by_name("P11", db)


class TestPaperEquityFallbackCausal:
    """Bridge down + dry_run → equity from DB; live path unchanged."""

    def test_initial_equity_no_trades(self, monkeypatch, p11_db):
        t = _make_trader(monkeypatch, p11_db)
        eq = t._get_equity()
        assert eq == 100.0, (
            f"Bridge down + dry_run must fall back to initial equity, got {eq}"
        )

    def test_closed_pnl_added(self, monkeypatch, p11_db):
        insert_live_trade(
            account_id=_acct_id(p11_db), timestamp=_TS, direction="BUY",
            entry_price=4000.0, lot_size=0.01, strategy_id="s-P11",
            db_path=p11_db,
        )
        # Close it with pnl +12.5 directly in DB (close path is tested elsewhere)
        from metty.core.db import get_connection
        conn = get_connection(p11_db)
        conn.execute(
            "UPDATE live_trades SET is_open=0, exit_price=4012.5, pnl=12.5, exit_reason='take_profit' "
            "WHERE account_id=? AND is_open=1", (_acct_id(p11_db),),
        )
        conn.commit()
        conn.close()

        t = _make_trader(monkeypatch, p11_db)
        eq = t._get_equity()
        assert eq == pytest.approx(112.5), (
            f"Paper equity must add closed pnl: 100 + 12.5, got {eq}"
        )

    def test_open_buy_marked_at_last_close(self, monkeypatch, p11_db):
        # Open BUY: entry 4000, lot 0.01 → floating = (4020-4000)*0.01*100 = 20
        insert_live_trade(
            account_id=_acct_id(p11_db), timestamp=_TS, direction="BUY",
            entry_price=4000.0, lot_size=0.01, strategy_id="s-P11",
            db_path=p11_db,
        )
        t = _make_trader(monkeypatch, p11_db, last_close=4020.0)
        eq = t._get_equity()
        assert eq == pytest.approx(120.0), (
            f"BUY floating pnl must be (mark-entry)*lot*100 = 20, got {eq}"
        )

    def test_open_sell_marked_at_last_close(self, monkeypatch, p11_db):
        # Open SELL: entry 4000, lot 0.01, mark 3990 → (4000-3990)*0.01*100 = 10
        insert_live_trade(
            account_id=_acct_id(p11_db), timestamp=_TS, direction="SELL",
            entry_price=4000.0, lot_size=0.01, strategy_id="s-P11",
            db_path=p11_db,
        )
        t = _make_trader(monkeypatch, p11_db, last_close=3990.0)
        eq = t._get_equity()
        assert eq == pytest.approx(110.0)

    def test_no_mark_price_zero_floating(self, monkeypatch, p11_db):
        insert_live_trade(
            account_id=_acct_id(p11_db), timestamp=_TS, direction="BUY",
            entry_price=4000.0, lot_size=0.01, strategy_id="s-P11",
            db_path=p11_db,
        )
        t = _make_trader(monkeypatch, p11_db, last_close=None)
        eq = t._get_equity()
        assert eq == pytest.approx(100.0), (
            "Without a mark price floating pnl must be 0, not fabricated"
        )

    def test_live_never_fabricates_equity(self, monkeypatch, p11_db):
        """Control: dry_run=False + bridge down → None (live behavior unchanged)."""
        insert_live_trade(
            account_id=_acct_id(p11_db), timestamp=_TS, direction="BUY",
            entry_price=4000.0, lot_size=0.01, strategy_id="s-P11",
            db_path=p11_db,
        )
        t = _make_trader(monkeypatch, p11_db, dry_run=False, last_close=4020.0)
        assert t._get_equity() is None

    def test_losing_open_position_reduces_equity(self, monkeypatch, p11_db):
        # Open BUY under water: entry 4000, mark 3980 → floating = -20
        insert_live_trade(
            account_id=_acct_id(p11_db), timestamp=_TS, direction="BUY",
            entry_price=4000.0, lot_size=0.01, strategy_id="s-P11",
            db_path=p11_db,
        )
        t = _make_trader(monkeypatch, p11_db, last_close=3980.0)
        eq = t._get_equity()
        assert eq == pytest.approx(80.0)