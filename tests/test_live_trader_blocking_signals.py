"""Integration tests for LiveTrader.run_once() blocking signals.

LiveTrader's run_once() has 13 blocking checkpoints in fixed order. Each
returns either {"action": "hold", "reason": ...} (rejection recorded) or
{"action": "skip", "reason": ...} (MT5 disconnection — no rejection). This
file exhaustively covers each checkpoint so silent regressions in the
guard order are caught.

Checkpoint order (live_trader.py:1244-1444):
  1.  HOLD signal                          → hold, no rejection
  2.  buy_low_confidence                   → hold + rejection
  3.  equity unavailable                   → skip, no rejection
  4.  drawdown:{reason}                    → hold + rejection
  5.  position_limit                       → hold + rejection
  6.  existing_position                    → hold + rejection
  7.  circuit_breaker:{reason}             → hold + rejection (learning_mode off)
  8.  cooldown                             → hold + rejection (learning_mode off)
  9.  calendar_avoid                       → hold + rejection (learning_mode off)
  10. spread unavailable                   → skip, no rejection
  11. ml_filter_circuit_break:{N}_fails    → hold + rejection
  12. ml_filter:{reason}                   → hold + rejection (multiplier == 0)
  13. ml_lot_too_small:{lots}              → hold + rejection (lots < 0.01)

Each test installs a "happy path" that lets the cycle reach the target
checkpoint, then flips one collaborator to trigger the block. Assertions
verify both the returned dict and that _record_rejection was (or was not)
called with the expected reason substring.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from shared.models import Signal, SignalType, TradingMode


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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


def _create_test_db(db_path: str) -> None:
    """Create an empty live_trades DB with the real schema."""
    conn = sqlite3.connect(db_path)
    conn.execute(f"CREATE TABLE IF NOT EXISTS live_trades ({LIVE_TRADES_COLUMNS})")
    conn.commit()
    conn.close()


def _make_m5(n_bars: int = 50) -> pd.DataFrame:
    """Minimal M5 dataframe with a UTC DatetimeIndex.

    Only requirement from run_once() is m5.index[-1] supports to_pydatetime /
    isoformat. _generate_signal is mocked so candle content is irrelevant.
    """
    idx = pd.date_range("2026-01-01 00:00", periods=n_bars, freq="5min", tz="UTC")
    return pd.DataFrame(
        {"open": 2300.0, "high": 2310.0, "low": 2295.0, "close": 2305.0, "volume": 100},
        index=idx,
    )


def _make_candles() -> dict[str, pd.DataFrame]:
    return {"M5": _make_m5()}


def _make_signal(
    signal_type: SignalType = SignalType.BUY,
    confidence: float = 0.70,
    price: float = 2300.0,
) -> Signal:
    return Signal(
        symbol="XAUUSD",
        signal_type=signal_type,
        confidence=confidence,
        price=price,
        timestamp=datetime.now(timezone.utc),
        timeframe="M5",
        indicators={},
        regime="trending",
        trading_mode=TradingMode.SWING,
        strategy_id="swing-B",
    )


def _make_trader(db_path: str, account: str = "B"):
    from metty.execution.live_trader import LiveTrader
    return LiveTrader(account=account, dry_run=True, db_path=db_path)


@pytest.fixture
def trader(tmp_path):
    db_path = str(tmp_path / "test.db")
    _create_test_db(db_path)
    return _make_trader(db_path, account="B")


def _install_happy_path(trader, signal: Signal | None = None):
    """Patch all collaborators so run_once() reaches the ML section unblocked.

    Returns (patchers, spies, signal):
      patchers: {key: _patch} — call .stop() in teardown
      spies:    {key: MagicMock} — the active mock for assertions
    """
    if signal is None:
        signal = _make_signal(SignalType.BUY, confidence=0.70)

    candles = _make_candles()
    patchers: dict[str, object] = {}
    spies: dict[str, MagicMock] = {}

    def _obj(name, target, attr, **kw):
        p = patch.object(target, attr, **kw)
        patchers[name] = p
        spies[name] = p.start()

    def _path(name, target, **kw):
        p = patch(target, **kw)
        patchers[name] = p
        spies[name] = p.start()

    # Preamble
    _obj("fetch", trader, "_fetch_candles", return_value=candles)
    _obj("monitor", trader, "_monitor_positions", return_value=[])
    _obj("gen", trader, "_generate_signal", return_value=signal)

    # Risk checks (all pass)
    _obj("equity", trader, "_get_equity", return_value=1000.0)
    dd = MagicMock(); dd.check.return_value = (True, "ok")
    p = patch.object(trader, "_drawdown_protector", dd)
    patchers["dd"] = p; spies["dd"] = p.start()
    _path("open_trades", "metty.execution.live_trader.get_open_trades", return_value=[])
    _obj("existing", trader, "_check_existing_position", return_value=False)
    cb = MagicMock(); cb.can_open_trade.return_value = (True, "ok")
    p = patch.object(trader, "circuit_breaker", cb)
    patchers["cb"] = p; spies["cb"] = p.start()
    _obj("cooldown", trader, "_check_cooldown", return_value=False)
    _path("calendar", "metty.execution.live_trader.should_avoid_trading", return_value=False)
    _obj("spread", trader, "_get_current_spread", return_value=0.5)
    # ML disabled by default → no ML block
    _obj("ml_enabled", trader, "_ml_enabled", return_value=False)
    # Rejection spy — default no-op so DB writes don't fail
    _obj("reject", trader, "_record_rejection", return_value=None)

    return patchers, spies, signal


def _teardown(patchers: dict[str, object]) -> None:
    for p in patchers.values():
        try:
            p.stop()
        except RuntimeError:
            pass


def _replace(patchers, spies, name, new_patch):
    """Stop an existing patcher and install a new one in its place."""
    if name in patchers:
        patchers[name].stop()
    patchers[name] = new_patch
    spies[name] = new_patch.start()


# ---------------------------------------------------------------------------
# 1. HOLD signal
# ---------------------------------------------------------------------------

class TestHoldsignal:
    def test_hold_signal_returns_hold_no_rejection(self, trader):
        """SignalType.HOLD → hold + 'no signal' reason, no _record_rejection call."""
        hold_signal = _make_signal(SignalType.HOLD, confidence=0.20)
        patchers, spies, _ = _install_happy_path(trader, hold_signal)
        try:
            result = trader.run_once()
            assert result["action"] == "hold"
            assert "no signal" in result["reason"]
            assert "conf=0.20" in result["reason"]
            spies["reject"].assert_not_called()
        finally:
            _teardown(patchers)


# ---------------------------------------------------------------------------
# 2. buy_low_confidence
# ---------------------------------------------------------------------------

class TestBuyLowConfidence:
    def test_buy_below_min_confidence_blocks_and_records(self, trader):
        """BUY confidence < _buy_min_confidence (B default 0.45) → block + rejection."""
        low_signal = _make_signal(SignalType.BUY, confidence=0.30)
        patchers, spies, _ = _install_happy_path(trader, low_signal)
        try:
            result = trader.run_once()
            assert result["action"] == "hold"
            assert "BUY confidence too low" in result["reason"]
            assert "0.30" in result["reason"]
            spies["reject"].assert_called_once()
            reason = spies["reject"].call_args[0][1]
            assert reason.startswith("buy_low_confidence:")
            assert "0.30" in reason and "0.45" in reason
        finally:
            _teardown(patchers)

    def test_sell_below_buy_min_confidence_does_not_trigger(self, trader):
        """SELL signals are not subject to the BUY confidence filter."""
        sell_signal = _make_signal(SignalType.SELL, confidence=0.30)
        patchers, spies, _ = _install_happy_path(trader, sell_signal)
        try:
            result = trader.run_once()
            # Rejection must NOT mention buy_low_confidence
            if spies["reject"].called:
                for call in spies["reject"].call_args_list:
                    assert not call[0][1].startswith("buy_low_confidence")
        finally:
            _teardown(patchers)


# ---------------------------------------------------------------------------
# 3. equity unavailable
# ---------------------------------------------------------------------------

class TestEquityUnavailable:
    def test_equity_none_returns_skip_no_rejection(self, trader):
        """_get_equity() returns None → skip, no _record_rejection."""
        patchers, spies, _ = _install_happy_path(trader)
        _replace(patchers, spies, "equity",
                 patch.object(trader, "_get_equity", return_value=None))
        try:
            result = trader.run_once()
            assert result["action"] == "skip"
            assert "equity unavailable" in result["reason"]
            spies["reject"].assert_not_called()
        finally:
            _teardown(patchers)


# ---------------------------------------------------------------------------
# 4. drawdown protection
# ---------------------------------------------------------------------------

class TestDrawdownBlock:
    def test_drawdown_block_returns_hold_and_records(self, trader):
        """DrawdownProtector.check returns (False, reason) → hold + rejection."""
        patchers, spies, _ = _install_happy_path(trader)
        dd = MagicMock()
        dd.check.return_value = (False, "Daily drawdown limit exceeded")
        dd.sync_pnl_from_db = MagicMock()
        _replace(patchers, spies, "dd",
                 patch.object(trader, "_drawdown_protector", dd))
        try:
            result = trader.run_once()
            assert result["action"] == "hold"
            assert "drawdown protection" in result["reason"]
            assert "Daily drawdown" in result["reason"]
            spies["reject"].assert_called_once()
            reason = spies["reject"].call_args[0][1]
            assert reason.startswith("drawdown:")
            assert "Daily drawdown" in reason
        finally:
            _teardown(patchers)


# ---------------------------------------------------------------------------
# 5. position_limit
# ---------------------------------------------------------------------------

class TestPositionLimit:
    def test_open_trades_at_dynamic_max_blocks_and_records(self, trader):
        """len(open_trades) >= dynamic_max → hold + rejection 'position_limit'."""
        patchers, spies, _ = _install_happy_path(trader)
        # dynamic_max = max(1, equity // equity_per_position) capped at max_positions_cap
        # equity=1000.0, default equity_per_position=200 → 5; cap=5 → dynamic_max=5.
        fake_trades = [{"id": i} for i in range(5)]
        _replace(patchers, spies, "open_trades",
                 patch("metty.execution.live_trader.get_open_trades",
                       return_value=fake_trades))
        try:
            result = trader.run_once()
            assert result["action"] == "hold"
            assert "position limit" in result["reason"]
            spies["reject"].assert_called_once()
            assert spies["reject"].call_args[0][1] == "position_limit"
        finally:
            _teardown(patchers)


# ---------------------------------------------------------------------------
# 6. existing_position
# ---------------------------------------------------------------------------

class TestExistingPosition:
    def test_existing_position_blocks_and_records(self, trader):
        """_check_existing_position True → hold + rejection 'existing_position'."""
        patchers, spies, _ = _install_happy_path(trader)
        _replace(patchers, spies, "existing",
                 patch.object(trader, "_check_existing_position", return_value=True))
        try:
            result = trader.run_once()
            assert result["action"] == "hold"
            assert "position already open" in result["reason"]
            spies["reject"].assert_called_once()
            assert spies["reject"].call_args[0][1] == "existing_position"
        finally:
            _teardown(patchers)


# ---------------------------------------------------------------------------
# 7. circuit_breaker
# ---------------------------------------------------------------------------

class TestCircuitBreaker:
    def test_circuit_breaker_block_returns_hold_and_records(self, trader):
        """circuit_breaker.can_open_trade False → hold + rejection 'circuit_breaker:'."""
        # learning_mode default False — required to reach this checkpoint
        assert trader.learning_mode is False
        patchers, spies, _ = _install_happy_path(trader)
        cb = MagicMock()
        cb.can_open_trade.return_value = (False, "consecutive losses (3)")
        cb.state = MagicMock(consecutive_losses=3, daily_loss_pct=0.0)
        _replace(patchers, spies, "cb",
                 patch.object(trader, "circuit_breaker", cb))
        try:
            result = trader.run_once()
            assert result["action"] == "hold"
            assert "circuit breaker" in result["reason"]
            assert "consecutive losses" in result["reason"]
            spies["reject"].assert_called_once()
            reason = spies["reject"].call_args[0][1]
            assert reason.startswith("circuit_breaker:")
            assert "consecutive losses" in reason
        finally:
            _teardown(patchers)


# ---------------------------------------------------------------------------
# 8. cooldown
# ---------------------------------------------------------------------------

class TestCooldown:
    def test_cooldown_blocks_and_records(self, trader):
        """_check_cooldown True → hold + rejection 'cooldown'."""
        patchers, spies, _ = _install_happy_path(trader)
        _replace(patchers, spies, "cooldown",
                 patch.object(trader, "_check_cooldown", return_value=True))
        try:
            result = trader.run_once()
            assert result["action"] == "hold"
            assert "cooldown" in result["reason"]
            spies["reject"].assert_called_once()
            assert spies["reject"].call_args[0][1] == "cooldown"
        finally:
            _teardown(patchers)


# ---------------------------------------------------------------------------
# 9. calendar_avoid
# ---------------------------------------------------------------------------

class TestCalendarAvoid:
    def test_high_impact_news_blocks_and_records(self, trader):
        """should_avoid_trading True → hold + rejection 'calendar_avoid'."""
        patchers, spies, _ = _install_happy_path(trader)
        _replace(patchers, spies, "calendar",
                 patch("metty.execution.live_trader.should_avoid_trading",
                       return_value=True))
        try:
            result = trader.run_once()
            assert result["action"] == "hold"
            assert "high-impact news" in result["reason"]
            spies["reject"].assert_called_once()
            assert spies["reject"].call_args[0][1] == "calendar_avoid"
        finally:
            _teardown(patchers)


# ---------------------------------------------------------------------------
# 10. spread unavailable
# ---------------------------------------------------------------------------

class TestSpreadUnavailable:
    def test_spread_none_returns_skip_no_rejection(self, trader):
        """_get_current_spread None → skip, no _record_rejection."""
        patchers, spies, _ = _install_happy_path(trader)
        _replace(patchers, spies, "spread",
                 patch.object(trader, "_get_current_spread", return_value=None))
        try:
            result = trader.run_once()
            assert result["action"] == "skip"
            assert "spread unavailable" in result["reason"]
            spies["reject"].assert_not_called()
        finally:
            _teardown(patchers)


# ---------------------------------------------------------------------------
# 11. ml_filter_circuit_break
# ---------------------------------------------------------------------------

class TestMLFilterCircuitBreak:
    def test_ml_consecutive_failures_blocks_and_records(self, trader):
        """_ml_fail_count >= ML_MAX_CONSECUTIVE_FAILS → hold + rejection."""
        from metty.execution.live_trader import ML_MAX_CONSECUTIVE_FAILS
        patchers, spies, _ = _install_happy_path(trader)
        # Enable ML path and set fail count over the limit
        _replace(patchers, spies, "ml_enabled",
                 patch.object(trader, "_ml_enabled", return_value=True))
        trader._ml_fail_count = ML_MAX_CONSECUTIVE_FAILS + 1
        trader._ml_predictor = MagicMock()  # present but should not be called
        try:
            result = trader.run_once()
            assert result["action"] == "hold"
            assert "ML circuit breaker" in result["reason"]
            assert str(ML_MAX_CONSECUTIVE_FAILS + 1) in result["reason"]
            spies["reject"].assert_called_once()
            reason = spies["reject"].call_args[0][1]
            assert reason.startswith("ml_filter_circuit_break:")
            assert "fails" in reason
        finally:
            _teardown(patchers)


# ---------------------------------------------------------------------------
# 12. ml_filter block (multiplier == 0)
# ---------------------------------------------------------------------------

class TestMLFilterBlock:
    def test_ml_multiplier_zero_blocks_and_records(self, trader):
        """get_risk_multiplier returns 0.0 → hold + rejection 'ml_filter:'."""
        patchers, spies, _ = _install_happy_path(trader)
        _replace(patchers, spies, "ml_enabled",
                 patch.object(trader, "_ml_enabled", return_value=True))
        predictor = MagicMock()
        predictor.get_risk_multiplier.return_value = (
            0.0, "loss_proba 0.92 > 0.45", 0.92, "V4+v6-OR",
        )
        trader._ml_predictor = predictor
        with patch(
            "broky.ml.trade_outcome_predictor.compute_features_from_candles",
            return_value={"regime": "trending"},
        ):
            try:
                result = trader.run_once()
                assert result["action"] == "hold"
                assert "loss_proba" in result["reason"]
                spies["reject"].assert_called_once()
                reason = spies["reject"].call_args[0][1]
                assert reason.startswith("ml_filter:")
                assert "loss_proba" in reason
            finally:
                _teardown(patchers)


# ---------------------------------------------------------------------------
# 13. ml_lot_too_small
# ---------------------------------------------------------------------------

class TestMLLotTooSmall:
    def test_lot_below_min_blocks_and_records(self, trader):
        """ml_risk_multiplier shrinks lots below 0.01 → hold + rejection."""
        patchers, spies, _ = _install_happy_path(trader)
        _replace(patchers, spies, "ml_enabled",
                 patch.object(trader, "_ml_enabled", return_value=True))
        predictor = MagicMock()
        # Tiny multiplier so lots * multiplier < 0.01
        predictor.get_risk_multiplier.return_value = (
            0.001, "risk-scaled down", 0.80, "V4+v6-OR",
        )
        trader._ml_predictor = predictor
        with patch(
            "broky.ml.trade_outcome_predictor.compute_features_from_candles",
            return_value={"regime": "trending"},
        ):
            try:
                result = trader.run_once()
                assert result["action"] == "hold"
                assert "lot too small" in result["reason"]
                spies["reject"].assert_called_once()
                reason = spies["reject"].call_args[0][1]
                assert reason.startswith("ml_lot_too_small:")
            finally:
                _teardown(patchers)


# ---------------------------------------------------------------------------
# Order verification — guards must fire in documented order
# ---------------------------------------------------------------------------

class TestBlockingOrder:
    """Smoke test: when two blocks are active, the EARLIER one wins.

    Confirms the guard ordering documented at the top of this file hasn't
    drifted. Prevents a refactor from silently reordering checks.
    """

    def test_drawdown_beats_position_limit(self, trader):
        """Drawdown check is before position_limit — drawdown should win."""
        patchers, spies, _ = _install_happy_path(trader)
        dd = MagicMock()
        dd.check.return_value = (False, "Daily drawdown limit exceeded")
        dd.sync_pnl_from_db = MagicMock()
        _replace(patchers, spies, "dd",
                 patch.object(trader, "_drawdown_protector", dd))
        _replace(patchers, spies, "open_trades",
                 patch("metty.execution.live_trader.get_open_trades",
                       return_value=[{"id": i} for i in range(5)]))
        try:
            result = trader.run_once()
            assert "drawdown protection" in result["reason"]
            assert spies["reject"].call_args[0][1].startswith("drawdown:")
        finally:
            _teardown(patchers)

    def test_position_limit_beats_existing_position(self, trader):
        """position_limit is checked before existing_position."""
        patchers, spies, _ = _install_happy_path(trader)
        _replace(patchers, spies, "open_trades",
                 patch("metty.execution.live_trader.get_open_trades",
                       return_value=[{"id": i} for i in range(5)]))
        _replace(patchers, spies, "existing",
                 patch.object(trader, "_check_existing_position", return_value=True))
        try:
            result = trader.run_once()
            assert "position limit" in result["reason"]
            assert spies["reject"].call_args[0][1] == "position_limit"
        finally:
            _teardown(patchers)

    def test_existing_position_beats_circuit_breaker(self, trader):
        """existing_position is checked before circuit_breaker."""
        patchers, spies, _ = _install_happy_path(trader)
        _replace(patchers, spies, "existing",
                 patch.object(trader, "_check_existing_position", return_value=True))
        cb = MagicMock()
        cb.can_open_trade.return_value = (False, "consecutive losses (3)")
        cb.state = MagicMock(consecutive_losses=3, daily_loss_pct=0.0)
        _replace(patchers, spies, "cb",
                 patch.object(trader, "circuit_breaker", cb))
        try:
            result = trader.run_once()
            assert "position already open" in result["reason"]
            assert spies["reject"].call_args[0][1] == "existing_position"
        finally:
            _teardown(patchers)