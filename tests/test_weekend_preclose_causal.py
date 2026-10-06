"""Causal proof test: farm positions must not ride the weekend (ISSUE-106).

Hypothesis
----------
The farm has NO rule for the weekend closure. Every exit check in
_monitor_positions needs M5 bars to advance (SL/TP compare against the
latest close, time_stop counts bars) — but gold closes Friday ~21:00 UTC
and produces no bars until Sunday 22:00 UTC. So a position opened Friday
evening floats through the close: on 2026-10-02 the farm opened 9 BUYs
20:10-20:50 UTC and held them over the weekend, and Sunday's gap pnl
entered league stats — a risk the strategy was never judged on and the
backtest never priced.

Fix (declared rule, dry-run only — live keeps its own policy):
- Friday at/after the cutoff (default 20:30 UTC, env
  WEEKEND_PRECLOSE_UTC / WEEKEND_PRECLOSE_UTC_<ACCOUNT>, "HH:MM"):
  monitor force-closes every open position with exit_reason
  "weekend_pre_close" (market close → the ISSUE-098 exit fill pays the
  spread's closing half).
- No NEW entries inside the window (a fresh position minutes before the
  close would only be pre-closed next cycle = pure spread churn).
- TP1 scale-in is skipped in the window (same churn reason).

Causal proof
------------
1. Window logic: Friday 20:35 UTC → True; 20:29 → False; Monday → False;
   env cutoff moves the boundary.
2. Monitor in-window: open BUY floats mid-range → MUST close with
   "weekend_pre_close" (RED now: no close at all — the exact incident).
3. Out-of-window control: same trade → stays open.
4. In-window TP1: closes as weekend_pre_close, and NO scale-in appears.
5. Entry gate: forced actionable signal in-window → hold, no trade opened.
6. Live control: dry_run=False → window never closes anything.

This test FAILS (RED) before the fix, PASSES (GREEN) after.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from metty.core.db import (  # noqa: E402
    get_open_trades,
    init_db,
    insert_account,
    insert_live_trade,
)
from metty.execution.live_trader import CONTRACT_SIZE, LiveTrader, RiskConfig  # noqa: E402
from shared.models import Signal, SignalType  # noqa: E402

_M5_CSV = "XAUUSD_M5.csv"
LOT = 0.05


@pytest.fixture()
def farm_db(tmp_path):
    db = tmp_path / "weekend_preclose_test.db"
    init_db(db)
    insert_account(
        name="P11", balance=100.0, leverage=2000,
        bridge_host="none", bridge_port=8001, signal_group="portfolio",
        db_path=db,
    )
    return db


def _write_m5(data_dir: Path, last_close: float = 4010.0) -> pd.DataFrame:
    """Mid-range close 4010: inside SL 3990 / TP 4020 — only the weekend
    rule (or nothing, pre-fix) can close it."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    n = 120
    idx = pd.date_range(now - timedelta(minutes=5 * (n - 1)), periods=n, freq="5min")
    closes = pd.Series([4000.0 + i * 0.1 for i in range(n)], index=idx)
    closes.iloc[-1] = last_close
    df = pd.DataFrame({
        "open": closes, "high": closes + 0.05, "low": closes - 0.05,
        "close": closes, "volume": [100.0] * n,
    })
    df.index.name = "date"
    df.to_csv(data_dir / _M5_CSV, header=False, float_format="%.3f")
    return df


def _make_trader(monkeypatch, tmp_path, db, dry_run: bool = True,
                 partial_tp: str | None = None) -> LiveTrader:
    monkeypatch.setenv("ACCOUNTS", "P11")
    monkeypatch.setenv("MT5_BRIDGE_MAX_RETRIES", "1")
    monkeypatch.setenv("MT5_BRIDGE_RETRY_DELAY", "0")
    monkeypatch.setenv("INITIAL_EQUITY_P11", "100")
    monkeypatch.setenv("ML_FILTER_ENABLED", "0")
    monkeypatch.setenv("MOCK_SPREAD_POINTS", "20")
    if partial_tp is not None:
        monkeypatch.setenv("PARTIAL_TP_ENABLED_P11", partial_tp)
    t = LiveTrader(
        account="P11", db_path=db, data_dir=tmp_path, dry_run=dry_run,
        risk_config=RiskConfig(risk_per_trade=0.01),
    )
    from metty.bridge.client import MT5Bridge
    monkeypatch.setattr(MT5Bridge, "fetch_account_info_sync", lambda self: None)
    return t


def _insert_open_trade(db, t: LiveTrader, direction: str = "BUY",
                       tp1_price: float | None = None) -> int:
    return insert_live_trade(
        account_id=t.account_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        direction=direction,
        entry_price=4000.0,
        stop_loss=3990.0,
        take_profit=4020.0,
        lot_size=LOT,
        confidence=0.65,
        regime="ranging",
        session="asian",
        strategy_id="swing-P11",
        spread_at_entry=20.0,
        tp1_price=tp1_price,
        tp_level=1,
        db_path=db,
    )


def _force_signal(monkeypatch, t: LiveTrader, direction: str = "SELL") -> None:
    sig = Signal(
        signal_type=SignalType.BUY if direction == "BUY" else SignalType.SELL,
        confidence=0.65, price=4000.0,
        timestamp=datetime.now(timezone.utc),
        indicators={"trend_alignment": 1}, regime="ranging",
    )
    monkeypatch.setattr(t, "_generate_signal", lambda candles: sig)
    monkeypatch.setattr(t, "_get_calendar", lambda: [])


def _friday(dt_hour: int = 20, dt_min: int = 35) -> datetime:
    """A Friday at the given UTC time (real calendar Friday)."""
    now = datetime.now(timezone.utc)
    # Days until next Friday (4): Monday=0 ... Friday=4
    delta = (4 - now.weekday()) % 7
    fri = (now + timedelta(days=delta)).replace(
        hour=dt_hour, minute=dt_min, second=0, microsecond=0,
    )
    return fri


class TestWindowLogic:
    """The rule itself: Friday at/after cutoff, nothing else."""

    def test_calendar_boundary(self, monkeypatch, tmp_path, farm_db):
        t = _make_trader(monkeypatch, tmp_path, farm_db)
        assert t._in_weekend_pre_close_window(_friday(20, 35)) is True
        assert t._in_weekend_pre_close_window(_friday(20, 30)) is True
        assert t._in_weekend_pre_close_window(_friday(20, 29)) is False
        assert t._in_weekend_pre_close_window(_friday(23, 59)) is True
        # Non-Friday at the same time-of-day → never
        sat = _friday(20, 35) + timedelta(days=1)
        mon = _friday(20, 35) + timedelta(days=3)
        assert t._in_weekend_pre_close_window(sat) is False
        assert t._in_weekend_pre_close_window(mon) is False

    def test_env_moves_the_cutoff(self, monkeypatch, tmp_path, farm_db):
        monkeypatch.setenv("WEEKEND_PRECLOSE_UTC", "19:00")
        t = _make_trader(monkeypatch, tmp_path, farm_db)
        assert t._in_weekend_pre_close_window(_friday(19, 5)) is True
        assert t._in_weekend_pre_close_window(_friday(18, 59)) is False

    def test_account_scoped_env_wins(self, monkeypatch, tmp_path, farm_db):
        monkeypatch.setenv("WEEKEND_PRECLOSE_UTC", "19:00")
        monkeypatch.setenv("WEEKEND_PRECLOSE_UTC_P11", "21:00")
        t = _make_trader(monkeypatch, tmp_path, farm_db)
        assert t._in_weekend_pre_close_window(_friday(20, 30)) is False
        assert t._in_weekend_pre_close_window(_friday(21, 0)) is True

    def test_malformed_env_falls_back_to_default(
        self, monkeypatch, tmp_path, farm_db
    ):
        monkeypatch.setenv("WEEKEND_PRECLOSE_UTC", "banana")
        t = _make_trader(monkeypatch, tmp_path, farm_db)
        # default 20:30 still applies
        assert t._in_weekend_pre_close_window(_friday(20, 31)) is True
        assert t._in_weekend_pre_close_window(_friday(20, 29)) is False


class TestMonitorClosesInWindow:
    """The incident replay: Friday-evening BUY must not float to Sunday."""

    def test_in_window_position_is_flattened(self, monkeypatch, tmp_path, farm_db):
        m5 = _write_m5(tmp_path)  # close 4010 — between SL 3990 and TP 4020
        t = _make_trader(monkeypatch, tmp_path, farm_db)
        monkeypatch.setattr(t, "_in_weekend_pre_close_window", lambda now=None: True)
        _insert_open_trade(farm_db, t)

        closed = t._monitor_positions({"M5": m5})
        assert closed, (
            "Friday pre-close window: open position must be flattened — "
            "pre-fix it floats over the weekend (the 2026-10-02 incident)"
        )
        assert closed[0]["exit_reason"] == "weekend_pre_close"
        # Market close pays the exit-leg spread (ISSUE-098 fill): 4010 - 0.10
        assert closed[0]["exit_price"] == pytest.approx(4009.90)

    def test_out_of_window_position_stays_open(self, monkeypatch, tmp_path, farm_db):
        # Control: same floating trade, window closed → nothing happens.
        m5 = _write_m5(tmp_path)
        t = _make_trader(monkeypatch, tmp_path, farm_db)
        monkeypatch.setattr(t, "_in_weekend_pre_close_window", lambda now=None: False)
        _insert_open_trade(farm_db, t)

        closed = t._monitor_positions({"M5": m5})
        assert not closed, "outside the window the rule must not touch the trade"
        assert get_open_trades(t.account_id, farm_db), "trade must still be open"

    def test_no_scale_in_inside_window(self, monkeypatch, tmp_path, farm_db):
        # TP1 legitimately hit, but inside the window: close position 1 as
        # weekend_pre_close and do NOT open the scale-in (it would be
        # pre-closed 5 minutes later = pure spread churn).
        m5 = _write_m5(tmp_path, last_close=3990.0)  # <= TP1 3991.25 → TP1 hit
        t = _make_trader(monkeypatch, tmp_path, farm_db, partial_tp="1")
        monkeypatch.setattr(t, "_in_weekend_pre_close_window", lambda now=None: True)
        _insert_open_trade(farm_db, t, "SELL", tp1_price=4028.75)

        closed = t._monitor_positions({"M5": m5})
        assert closed, "window must still flatten the trade"
        assert closed[0]["exit_reason"] == "weekend_pre_close"
        assert not get_open_trades(t.account_id, farm_db), (
            "no scale-in position may be opened inside the pre-close window"
        )


class TestEntryGate:
    """No new farm entries inside the window."""

    def test_entry_held_in_window(self, monkeypatch, tmp_path, farm_db):
        _write_m5(tmp_path)
        t = _make_trader(monkeypatch, tmp_path, farm_db)
        monkeypatch.setattr(t, "_in_weekend_pre_close_window", lambda now=None: True)
        _force_signal(monkeypatch, t, "SELL")

        result = t.run_once()
        assert "weekend pre-close" in (result.get("reason") or ""), (
            f"actionable signal inside the window must be held, got {result}"
        )
        assert not get_open_trades(t.account_id, farm_db), (
            "no trade may be opened inside the pre-close window"
        )

    def test_entry_allowed_outside_window(self, monkeypatch, tmp_path, farm_db):
        # Control: identical setup, window closed → the farm still trades
        # (the rule must gate Friday evenings, not the whole market).
        _write_m5(tmp_path)
        t = _make_trader(monkeypatch, tmp_path, farm_db)
        monkeypatch.setattr(t, "_in_weekend_pre_close_window", lambda now=None: False)
        _force_signal(monkeypatch, t, "SELL")

        result = t.run_once()
        assert get_open_trades(t.account_id, farm_db), (
            f"outside the window the farm must keep trading, got {result}"
        )


class TestLiveControl:
    """dry_run=False → the rule never touches live accounts (VPS scope)."""

    def test_live_never_flattens(self, monkeypatch, tmp_path, farm_db):
        m5 = _write_m5(tmp_path)
        t = _make_trader(monkeypatch, tmp_path, farm_db, dry_run=False)
        monkeypatch.setattr(t, "_in_weekend_pre_close_window", lambda now=None: True)
        _insert_open_trade(farm_db, t)

        closed = t._monitor_positions({"M5": m5})
        assert not closed, "live mode must never apply the farm weekend rule"