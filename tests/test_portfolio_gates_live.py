"""Integration (causal) tests for the two portfolio gates wired into
LiveTrader.run_once(): entry-hour window (4a1b) and freeze/cooldown (4a1c).

Causality in every test: the SAME trader and signal either pass or get
blocked purely because of the env window / DB state — flip the cause and
the block disappears.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.models import Signal, SignalType, TradingMode

from metty.core.db import (
    get_account_id_by_name,
    init_db,
    insert_account,
    set_portfolio_status,
)


def _make_m5(n_bars: int = 50) -> pd.DataFrame:
    """M5 candles; last bar = 2026-01-01 04:05 UTC (hour 4)."""
    idx = pd.date_range("2026-01-01 00:00", periods=n_bars, freq="5min", tz="UTC")
    return pd.DataFrame(
        {"open": 2300.0, "high": 2310.0, "low": 2295.0, "close": 2305.0, "volume": 100},
        index=idx,
    )


def _make_signal(signal_type: SignalType = SignalType.BUY) -> Signal:
    return Signal(
        symbol="XAUUSD",
        signal_type=signal_type,
        confidence=0.70,
        price=2300.0,
        timestamp=datetime.now(timezone.utc),
        timeframe="M5",
        indicators={},
        regime="trending",
        trading_mode=TradingMode.SWING,
        strategy_id="swing-P1",
    )


@pytest.fixture()
def p_trader(tmp_path, monkeypatch):
    """LiveTrader for account P1 against a real-schema per-account DB."""
    db_path = tmp_path / "oracle_p1.db"
    init_db(db_path)
    insert_account(name="P1", balance=100.0, leverage=2000,
                   bridge_host="mt5p1", bridge_port=8001,
                   signal_group="portfolio", db_path=db_path)

    # P-account config must be fully env-driven (no ACCOUNTS list dependency)
    monkeypatch.setenv("RISK_PER_TRADE_P1", "0.005")
    monkeypatch.setenv("MIN_CONFIDENCE_P1", "0.50")
    monkeypatch.setenv("MAX_POSITIONS_P1", "1")

    from metty.execution.live_trader import LiveTrader
    trader = LiveTrader(account="P1", dry_run=True, db_path=db_path)
    return trader


def _install_happy_path(trader, signal=None):
    """Patch collaborators so run_once reaches the gate checkpoints."""
    if signal is None:
        signal = _make_signal()
    candles = {"M5": _make_m5()}
    patchers, spies = [], {}

    def _obj(name, target, attr, **kw):
        p = patch.object(target, attr, **kw)
        patchers.append(p)
        spies[name] = p.start()

    _obj("fetch", trader, "_fetch_candles", return_value=candles)
    _obj("monitor", trader, "_monitor_positions", return_value=[])
    _obj("gen", trader, "_generate_signal", return_value=signal)
    _obj("existing", trader, "_check_existing_position", return_value=False)
    _obj("reject", trader, "_record_rejection", return_value=None)
    dd = MagicMock(); dd.check.return_value = (True, "ok")
    dd.state.daily_trades = 0
    dd.state.weekly_trades = 0
    p = patch.object(trader, "_drawdown_protector", dd)
    patchers.append(p); spies["dd"] = p.start()
    return patchers, spies, signal


def _teardown(patchers):
    for p in patchers:
        try:
            p.stop()
        except RuntimeError:
            pass


class TestLiveTraderPortfolioAccount:
    def test_p_account_constructs_and_resolves_account_id_from_db(self, p_trader):
        """P1's account_id comes from the DB (not the static A-D map)."""
        assert p_trader.account_id == get_account_id_by_name("P1", p_trader.db_path)

    def test_p_account_id_mismatch_fails_loud(self, tmp_path):
        """P-account with no DB row must raise, not fall back to another id
        (ISSUE M3 lesson: silent fallback routes trades to the wrong account)."""
        from metty.execution.live_trader import LiveTrader
        db_path = tmp_path / "empty.db"
        init_db(db_path)  # schema but no P-row
        with pytest.raises(ValueError, match="not found in accounts table"):
            LiveTrader(account="P9", dry_run=True, db_path=db_path)

    def test_unknown_account_still_rejected(self, tmp_path):
        from metty.execution.live_trader import LiveTrader
        with pytest.raises(ValueError, match="Unknown account"):
            LiveTrader(account="X1", dry_run=True, db_path=tmp_path / "x.db")


class TestEntryHourGateInRunOnce:
    def test_outside_window_blocks_actionable_signal(self, p_trader, monkeypatch):
        # Candle hour is 04:00 UTC — window says only 18-20 → block
        monkeypatch.setenv("ENTRY_HOURS_P1", "18,19,20")
        patchers, spies, _ = _install_happy_path(p_trader)
        try:
            result = p_trader.run_once()
            assert result["action"] == "hold"
            assert "entry-hour gate" in result["reason"]
            assert "outside entry window" in result["reason"]
            spies["reject"].assert_called_once()
            assert spies["reject"].call_args[0][1].startswith("entry_hour_blocked:")
        finally:
            _teardown(patchers)

    def test_inside_window_passes_the_gate(self, p_trader, monkeypatch):
        # Same candle, same signal — window includes hour 4 → gate passes
        monkeypatch.setenv("ENTRY_HOURS_P1", "4,18,19,20")
        patchers, spies, _ = _install_happy_path(p_trader)
        try:
            result = p_trader.run_once()
            assert "entry-hour gate" not in result["reason"]
        finally:
            _teardown(patchers)

    def test_blocked_hour_blocks_even_if_in_entry_window(self, p_trader, monkeypatch):
        # BLOCKED wins over ENTRY (blocked hours are the negative-EV veto)
        monkeypatch.setenv("ENTRY_HOURS_P1", "4")
        monkeypatch.setenv("BLOCKED_HOURS_P1", "4")
        patchers, spies, _ = _install_happy_path(p_trader)
        try:
            result = p_trader.run_once()
            assert result["action"] == "hold"
            assert "blocked hour 04" in result["reason"]
        finally:
            _teardown(patchers)

    def test_legacy_account_without_env_untouched(self, tmp_path, monkeypatch):
        """B with no ENTRY_HOURS env → no hour gate, cycle proceeds as before."""
        db_path = tmp_path / "legacy.db"
        init_db(db_path)
        from metty.execution.live_trader import LiveTrader
        trader = LiveTrader(account="B", dry_run=True, db_path=db_path)
        patchers, spies, _ = _install_happy_path(trader)
        try:
            result = trader.run_once()
            assert "entry-hour gate" not in result["reason"]
        finally:
            _teardown(patchers)


class TestPortfolioGateInRunOnce:
    def test_frozen_account_blocks_new_entries(self, p_trader):
        set_portfolio_status("P1", "frozen", "DD 20% from peak", p_trader.db_path)
        patchers, spies, _ = _install_happy_path(p_trader)
        try:
            result = p_trader.run_once()
            assert result["action"] == "hold"
            assert "portfolio gate" in result["reason"]
            assert "portfolio_status=frozen" in result["reason"]
            assert "DD 20% from peak" in result["reason"]
            spies["reject"].assert_called_once()
            assert spies["reject"].call_args[0][1].startswith("portfolio_gate:")
        finally:
            _teardown(patchers)

    def test_causal_unfreeze_lets_signal_through(self, p_trader):
        """Same trader, same signal, same DB — the ONLY change is the status
        row. Proves the DB state causes the block."""
        set_portfolio_status("P1", "frozen", "DD 20% from peak", p_trader.db_path)
        patchers, spies, _ = _install_happy_path(p_trader)
        try:
            assert "portfolio gate" in p_trader.run_once()["reason"]
        finally:
            _teardown(patchers)

        set_portfolio_status("P1", "running", "", p_trader.db_path)
        patchers, spies, _ = _install_happy_path(p_trader)
        try:
            result = p_trader.run_once()
            assert "portfolio gate" not in result["reason"]
        finally:
            _teardown(patchers)

    def test_gate_blocks_after_container_restart(self, p_trader):
        """Freeze is written, then a FRESH LiveTrader (simulated restart) is
        constructed — the block must persist (in-memory DP resets, DB doesn't)."""
        set_portfolio_status("P1", "frozen", "DD 20% from peak", p_trader.db_path)

        from metty.execution.live_trader import LiveTrader
        fresh = LiveTrader(account="P1", dry_run=True, db_path=p_trader.db_path)
        patchers, spies, _ = _install_happy_path(fresh)
        try:
            result = fresh.run_once()
            assert result["action"] == "hold"
            assert "portfolio_status=frozen" in result["reason"]
        finally:
            _teardown(patchers)

    def test_cooldown_pauses_new_entries(self, p_trader):
        from metty.core.db import set_cooldown
        future = (datetime.now(timezone.utc) + timedelta(hours=20)).isoformat()
        set_cooldown("P1", future, "3 consecutive losses", p_trader.db_path)
        patchers, spies, _ = _install_happy_path(p_trader)
        try:
            result = p_trader.run_once()
            assert result["action"] == "hold"
            assert "cooldown" in result["reason"]
        finally:
            _teardown(patchers)

    def test_frozen_account_still_monitors_positions(self, p_trader):
        """Freeze blocks NEW entries, but _monitor_positions must still run
        every cycle — existing positions close via SL/TP/max-holding."""
        set_portfolio_status("P1", "frozen", "DD 20% from peak", p_trader.db_path)
        patchers, spies, _ = _install_happy_path(p_trader)
        try:
            p_trader.run_once()
            spies["monitor"].assert_called_once()
        finally:
            _teardown(patchers)