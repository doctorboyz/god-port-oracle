"""Causal proof tests: ISSUE-041 — vanished broker closes are never detected.

Hypothesis
----------
`_monitor_positions` detects exits ONLY from M5 candle closes
(`current_price = m5["close"].iloc[-1]`). ISSUE-063/077 already fixed the
PRICE side once an exit is triggered (actual fill / deal reconciliation),
but the DETECTION side still has a blind spot: when the broker closes the
position intra-bar (SL gapped through on news, then price recovered by the
bar close), the M5 close never crosses the SL/TP level, so the exit branch
never fires. Consequences:

  1. The DB trade stays is_open=1 forever — a ghost that blocks new entries
     (same ghost family as ISSUE-077, but upstream: the close is never even
     attempted because the candle never "sees" it).
  2. Worse — the actual deal PnL never reaches the kill switch: CB and DP
     are updated only inside the exit branch. A gapped SL that closed $8
     through the level is INVISIBLE to the circuit breaker / drawdown
     protector, so the kill switch fires late (or never) exactly on the
     worst losses. That is the live ISSUE-041 claim: "kill switch fires
     late on gaps".

The mechanism under test is the VANISH CHECK: for a live trade that the
candles say is still open, ask MT5 whether the position still exists; if it
is gone, reconcile the actual closing deal and record its real price/PnL.

Causal proof
------------
Drive `_monitor_positions` on a real temp DB holding ONE open live trade:
BUY entry=2300, SL=2290, TP=2320, ticket=777, lots=0.05. The M5 candles'
last close is 2295 — ABOVE the SL, so candle-based detection sees nothing.
The broker closed the position at 2282.00 (SL gapped $8 through, reason=SL)
and the position is gone from MT5.

  1. Vanished position → trade closed in DB at the DEAL price 2282,
     pnl=-90.00, exit_reason=stop_loss, and circuit_breaker.record_loss
     receives the REAL gapped loss. Pre-fix: nothing happens (trade stays
     open, CB never told) → RED.
  2. Control: position still exists in MT5 → no close (guard against
     false-closing healthy positions).
  3. Control: bridge unhealthy (position check unknown) → no close, retry
     next cycle (guard against false-close on bridge failure — same
     discipline as ISSUE-061/077: unknown ≠ gone).

References
----------
- ISSUE-041: theoretical vs actual close — kill switch blind to gapped closes
- ISSUE-063 (actual close fill), ISSUE-077 (external close reconcile),
  ISSUE-061 (None vs [] bridge semantics), ISSUE-080 (deal time guard)
- Production: metty/execution/live_trader.py `_monitor_positions` —
  candle-only exit detection, no position-existence check
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# The causal scenario: candles say the trade is fine, the broker says
# it closed $8 through the SL.
ENTRY_PRICE = 2300.0
SL_PRICE = 2290.0
TP_PRICE = 2320.0
LAST_M5_CLOSE = 2295.0  # above SL → candle detection sees nothing
DEAL_EXIT_PRICE = 2282.0  # SL gapped $8 through
LOTS = 0.05
CONTRACT_SIZE = 100.0
EXPECTED_PNL = round((DEAL_EXIT_PRICE - ENTRY_PRICE) * LOTS * CONTRACT_SIZE, 2)  # -90.00

TRADE_TICKET = 777


def _make_m5() -> pd.DataFrame:
    """M5 bars where the last close sits between SL and TP — no exit seen."""
    idx = pd.date_range("2026-01-01 04:00", periods=12, freq="5min", tz="UTC")
    return pd.DataFrame(
        {
            "open": [2294.0] * 11 + [2294.5],
            "high": [2296.0] * 12,
            "low": [2294.0] * 12,
            "close": [2294.5] * 11 + [LAST_M5_CLOSE],
            "volume": [100] * 12,
        },
        index=idx,
    )


def _closing_deal() -> list[dict]:
    """Deal history: open deal + broker SL close gapped through the level."""
    return [
        {"order": TRADE_TICKET, "type": 0, "price": ENTRY_PRICE,
         "time": 1000.0, "reason": 0, "position_id": TRADE_TICKET, "comment": ""},
        # closing SELL deal (type=1), reason=4 (DEAL_REASON_SL), $8 below SL
        {"order": TRADE_TICKET, "type": 1, "price": DEAL_EXIT_PRICE,
         "time": 2000.0, "reason": 4, "position_id": TRADE_TICKET,
         "comment": "[sl] 2282.00"},
    ]


def _make_trader_with_open_trade(tmp_path) -> tuple:
    """LiveTrader(B) on a real temp DB holding ONE open live trade."""
    from metty.core.db import init_db, insert_live_trade
    from metty.execution.live_trader import LiveTrader

    db_path = str(tmp_path / "live.db")
    init_db(db_path)
    # accounts row for the insert_live_trade FK (B → account_id 2)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR IGNORE INTO accounts (id, name, broker_login, broker_server, "
        "balance, leverage, bridge_port, signal_group) "
        "VALUES (2, 'demo-b', '1', 'Exness-MT5Trial15', 10000, 100, 8001, 'B')"
    )
    conn.commit()
    conn.close()

    trade_id = insert_live_trade(
        account_id=2,
        timestamp="2026-01-01T04:00:00+00:00",
        direction="BUY",
        entry_price=ENTRY_PRICE,
        stop_loss=SL_PRICE,
        take_profit=TP_PRICE,
        lot_size=LOTS,
        confidence=0.7,
        ticket=TRADE_TICKET,
        strategy_id="mr-bet-B",
        db_path=db_path,
    )

    t = LiveTrader(account="B", dry_run=True, db_path=db_path)
    t.dry_run = False  # live path: the vanish check must run
    return t, db_path, trade_id


def _install_bridge_mocks(t, monkeypatch, position_exists, deals) -> MagicMock:
    """Mock only bridge I/O; DB, reconcile, pnl, CB wiring stay real.

    `position_exists` is what the (to-be-added) `_position_exists_in_mt5`
    returns: False (gone), True (still open), or None (bridge unhealthy).
    Patched with raising=False so the suite runs BEFORE the fix adds the
    method — pre-fix it is simply never called, which IS the bug.
    """
    monkeypatch.setattr(t, "_position_exists_in_mt5", lambda ticket: position_exists, raising=False)
    monkeypatch.setattr(t, "_get_deal_history", lambda days_back=7: deals)
    monkeypatch.setattr(t, "_get_equity", lambda: 10000.0)

    cb = MagicMock()
    monkeypatch.setattr(t, "circuit_breaker", cb)
    monkeypatch.setattr(t, "_drawdown_protector", MagicMock())
    monkeypatch.setattr(t, "event_bus", None)
    return cb


def _fetch_trade_row(db_path: str, trade_id: int) -> tuple:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT is_open, exit_price, pnl, exit_reason FROM live_trades WHERE id = ?",
            (trade_id,),
        ).fetchone()
    finally:
        conn.close()


class TestVanishedBrokerCloseDetected:
    """ISSUE-041: a broker close the candles never saw must still be recorded."""

    def test_vanished_position_closed_at_actual_deal_price(self, tmp_path, monkeypatch):
        """Position gone from MT5, candles silent → close DB at the DEAL price.

        Pre-fix: candle-based detection sees close 2295 > SL 2290 → no exit
        branch → nothing happens, trade stays is_open=1, CB never told → RED.
        """
        t, db_path, trade_id = _make_trader_with_open_trade(tmp_path)
        cb = _install_bridge_mocks(t, monkeypatch, position_exists=False, deals=_closing_deal())

        closed = t._monitor_positions({"M5": _make_m5()})

        assert closed, (
            "ISSUE-041 unfixed: the broker closed ticket "
            f"{TRADE_TICKET} at ${DEAL_EXIT_PRICE} (SL gapped through "
            f"${SL_PRICE}) but the candles recovered — _monitor_positions "
            "detected nothing and the trade ghosts on"
        )
        is_open, exit_price, pnl, exit_reason = _fetch_trade_row(db_path, trade_id)
        assert is_open == 0, "vanished position must be closed in DB"
        assert exit_price == pytest.approx(DEAL_EXIT_PRICE, abs=0.01), (
            f"exit must be the ACTUAL deal price {DEAL_EXIT_PRICE}, "
            f"got {exit_price}"
        )
        assert pnl == pytest.approx(EXPECTED_PNL, abs=0.5), (
            f"pnl must reflect the gapped fill ({EXPECTED_PNL}), got {pnl}"
        )
        assert exit_reason == "stop_loss", f"reason from the deal, got {exit_reason}"

    def test_kill_switch_sees_the_real_gapped_loss(self, tmp_path, monkeypatch):
        """CB must record the actual gapped loss — the core ISSUE-041 claim.

        Pre-fix: record_loss is never called (exit branch never runs) → RED.
        """
        t, db_path, trade_id = _make_trader_with_open_trade(tmp_path)
        cb = _install_bridge_mocks(t, monkeypatch, position_exists=False, deals=_closing_deal())

        t._monitor_positions({"M5": _make_m5()})

        cb.record_loss.assert_called_once(), (
            "circuit breaker must be told about the gapped loss exactly once"
        )
        recorded = cb.record_loss.call_args[0][0]
        assert recorded == pytest.approx(EXPECTED_PNL, abs=0.5), (
            f"CB must see the REAL gapped pnl ({EXPECTED_PNL}), got {recorded} "
            f"— a theoretical-price recording understates the loss and the "
            f"kill switch fires late (ISSUE-041)"
        )


class TestVanishCheckGuards:
    """Controls: the vanish check must not false-close healthy trades."""

    def test_position_still_open_not_closed(self, tmp_path, monkeypatch):
        """Position alive in MT5 → nothing may close, regardless of candles."""
        t, db_path, trade_id = _make_trader_with_open_trade(tmp_path)
        cb = _install_bridge_mocks(t, monkeypatch, position_exists=True, deals=_closing_deal())

        closed = t._monitor_positions({"M5": _make_m5()})

        assert closed == [], "a position MT5 still holds must not be closed"
        is_open, exit_price, pnl, _ = _fetch_trade_row(db_path, trade_id)
        assert is_open == 1, "healthy position must stay open in DB"
        cb.record_loss.assert_not_called()
        cb.record_win.assert_not_called()

    def test_bridge_unhealthy_no_false_close(self, tmp_path, monkeypatch):
        """Position check UNKNOWN (bridge failure) → retry next cycle.

        Same discipline as ISSUE-061: unknown ≠ gone. False-closing on a
        bridge hiccup would record a made-up PnL and pollute CB/DP.
        """
        t, db_path, trade_id = _make_trader_with_open_trade(tmp_path)
        cb = _install_bridge_mocks(t, monkeypatch, position_exists=None, deals=_closing_deal())

        closed = t._monitor_positions({"M5": _make_m5()})

        assert closed == [], "unknown position state must not close the trade"
        is_open = _fetch_trade_row(db_path, trade_id)[0]
        assert is_open == 1, "bridge-unhealthy trade must stay open and retry"
        cb.record_loss.assert_not_called()