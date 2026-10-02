"""Causal proof test: brokerless farm must not cut every entry on spread.

Hypothesis
----------
The paper farm is brokerless (MT5_BRIDGE_MAX_RETRIES=0, no MT5 service).
_get_current_spread() therefore always returns None, and run_once skips
EVERY cycle that survives all other gates with
"spread unavailable (MT5 disconnected?)" (live_trader.py:2341). On
2026-10-02 the canary variant P20 (gate-off) fired 7 real signals — all
7 died on exactly this skip. Every other variant would die the same way
inside its entry window → farm stays at 0 trades forever, defeating its
purpose.

Fix: MOCK_SPREAD_POINTS{_<ACCOUNT>} (points). When set, return it
WITHOUT touching the bridge. Value choice is deliberate (see
test_mr_cost_mult_consistent_with_mock): 20 points = $0.20 passes
SWING_MAX_SPREAD=30 and keeps MR_COST_MULT=3.0 meaningful
(TP must cover >= 3 x $0.20 = $0.60 — trivially true for ATR-scaled MR
TPs, so it is NOT the next blocker). Unset → legacy bridge path, so
live/VPS behavior is unchanged.

Causal proof
------------
MOCK_SPREAD_POINTS=20 + bridge down + forced signal → run_once reaches
the spread gate with 20.0 and does NOT return the spread skip.
Controls: env unset → 'spread unavailable' skip preserved (live path
unchanged); wide mock (50 > SWING_MAX_SPREAD=30) → 'spread_too_wide'
hold (the mock does not blind the gate); account-scoped env beats the
global one; TradeBlocker cost gate is meaningful — not inert — at the
chosen mock value.

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

from broky.risk.trade_blocker import BlockInput, TradeBlocker  # noqa: E402
from metty.core.db import init_db, insert_account  # noqa: E402
from metty.execution.live_trader import LiveTrader, RiskConfig  # noqa: E402
from shared.models import Signal, SignalType  # noqa: E402

_M5_CSV = "XAUUSD_M5.csv"


@pytest.fixture()
def p11_db(tmp_path):
    db = tmp_path / "spread_test.db"
    init_db(db)
    insert_account(
        name="P11", balance=100.0, leverage=2000,
        bridge_host="none", bridge_port=8001, signal_group="portfolio",
        db_path=db,
    )
    return db


def _write_m5(data_dir: Path) -> None:
    """Fresh M5 CSV (last bar 5 min old — passes the staleness guard)."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    n = 120
    idx = pd.date_range(now - timedelta(minutes=5 * (n - 1)), periods=n, freq="5min")
    closes = pd.Series([4000.0 + i * 0.5 for i in range(n)], index=idx)
    df = pd.DataFrame({
        "open": closes, "high": closes + 1.0, "low": closes - 1.0,
        "close": closes, "volume": [100.0] * n,
    })
    df.index.name = "date"
    df.to_csv(data_dir / _M5_CSV, header=False, float_format="%.3f")


def _make_trader(monkeypatch, tmp_path, db, data_dir, mock_points: str | None,
                 max_spread: str | None = None):
    """P11 paper trader, bridge forced down, feed at data_dir.

    Mirrors the farm container (ACCOUNTS=P11, no ML, bridge down) plus the
    new mock-spread knob. Bridge calls fail fast on the nonexistent host,
    exactly like the brokerless farm.
    """
    monkeypatch.setenv("ACCOUNTS", "P11")
    monkeypatch.setenv("MT5_BRIDGE_MAX_RETRIES", "1")
    monkeypatch.setenv("MT5_BRIDGE_RETRY_DELAY", "0")
    monkeypatch.setenv("INITIAL_EQUITY_P11", "100")
    monkeypatch.setenv("ML_FILTER_ENABLED", "0")
    if mock_points is not None:
        monkeypatch.setenv("MOCK_SPREAD_POINTS", mock_points)
    if max_spread is not None:
        monkeypatch.setenv("SWING_MAX_SPREAD_P11", max_spread)
    t = LiveTrader(
        account="P11", db_path=db, data_dir=data_dir, dry_run=True,
        risk_config=RiskConfig(risk_per_trade=0.01),
    )
    from metty.bridge.client import MT5Bridge
    monkeypatch.setattr(MT5Bridge, "fetch_account_info_sync", lambda self: None)
    return t


def _force_signal(monkeypatch, t: LiveTrader) -> None:
    """Deterministic actionable signal so the cycle reaches the spread gate.

    SELL conf=0.65 (clears BUY filter n/a + min 0.55), trend_alignment=+1
    (not counter-trend → 4a1 gate passes), regime=ranging MR style.
    Entry-hour gate is a no-op without ENTRY_HOURS_P11 env. The calendar
    fetch is stubbed to empty to keep the test offline-deterministic.
    """
    sig = Signal(
        signal_type=SignalType.SELL, confidence=0.65, price=4000.0,
        timestamp=datetime.now(timezone.utc),
        indicators={"trend_alignment": 1}, regime="ranging",
    )
    monkeypatch.setattr(t, "_generate_signal", lambda candles: sig)
    monkeypatch.setattr(t, "_get_calendar", lambda: [])


class TestSpreadMockFarmCausal:
    """Farm mode (mock spread) → no 'spread unavailable' skip; controls hold."""

    def test_mock_spread_passes_gate(self, monkeypatch, tmp_path, p11_db):
        # Arrange: farm mode — MOCK_SPREAD_POINTS=20, bridge down
        _write_m5(tmp_path)
        t = _make_trader(monkeypatch, tmp_path, p11_db, tmp_path, "20")
        _force_signal(monkeypatch, t)

        seen: dict = {}
        _orig = t._spread_gate_ok

        def _spy(spread_points):
            seen["spread"] = spread_points
            return _orig(spread_points)

        monkeypatch.setattr(t, "_spread_gate_ok", _spy)

        # Act
        result = t.run_once()

        # Assert: cycle passed the None gate, spread gate saw the mock value
        assert result.get("reason") != "spread unavailable (MT5 disconnected?)", (
            f"farm mode must not skip on spread, got {result}"
        )
        assert "spread" in seen, (
            f"cycle must REACH the spread gate in farm mode, got {result}"
        )
        assert seen["spread"] == 20.0, (
            f"spread gate must see the mock value 20.0, saw {seen.get('spread')}"
        )

    def test_env_unset_keeps_legacy_skip(self, monkeypatch, tmp_path, p11_db):
        # Control: env unset + bridge down → live/VPS behavior preserved
        _write_m5(tmp_path)
        t = _make_trader(monkeypatch, tmp_path, p11_db, tmp_path, None)
        _force_signal(monkeypatch, t)

        result = t.run_once()
        assert result.get("reason") == "spread unavailable (MT5 disconnected?)", (
            f"unset env must keep the legacy bridge-path skip, got {result}"
        )

    def test_mock_never_calls_bridge(self, monkeypatch, tmp_path, p11_db):
        # The knob must not touch MT5 at all (farm has no MT5 service)
        _write_m5(tmp_path)
        t = _make_trader(monkeypatch, tmp_path, p11_db, tmp_path, "20")

        from metty.bridge.client import MT5Bridge

        def _boom(self, symbol):  # noqa: ANN001
            raise AssertionError("MOCK_SPREAD_POINTS set — bridge must not be called")

        monkeypatch.setattr(MT5Bridge, "get_spread_sync", _boom)

        assert t._get_current_spread() == 20.0

    def test_account_scoped_mock_wins(self, monkeypatch, tmp_path, p11_db):
        # Per-account override beats the global knob (variant sensitivity)
        _write_m5(tmp_path)
        t = _make_trader(monkeypatch, tmp_path, p11_db, tmp_path, "20")
        monkeypatch.setenv("MOCK_SPREAD_POINTS_P11", "35")

        assert t._get_current_spread() == 35.0

    def test_wide_mock_still_holds(self, monkeypatch, tmp_path, p11_db):
        # Control: the mock does not blind the max-spread gate —
        # 50 > SWING_MAX_SPREAD=30 → hold with spread_too_wide
        _write_m5(tmp_path)
        t = _make_trader(monkeypatch, tmp_path, p11_db, tmp_path, "50",
                         max_spread="30")
        _force_signal(monkeypatch, t)

        result = t.run_once()
        assert "spread_too_wide" in (result.get("reason") or ""), (
            f"a mock wider than SWING_MAX_SPREAD must hold the entry, got {result}"
        )


class TestMrCostMultConsistency:
    """MR_COST_MULT must be meaningful — not inert — at the chosen mock value.

    Hermes point 2 (2026-10-02): verify the TP >= N x spread cost gate is
    not the next farm blocker. Mock 20 points = $0.20 spread; farm runs
    MR_COST_MULT=3.0 → TP must cover >= $0.60. ATR-scaled MR TPs are
    dollars wide, so it passes real trades while still cutting a
    degenerate sub-cost TP — the gate keeps its purpose.
    """

    def _blocker(self) -> TradeBlocker:
        return TradeBlocker(
            daily_trade_count_limit=20, weekly_trade_count_limit=80,
            hard_max_lots=0.50, tp_cost_mult=3.0,
        )

    def _inp(self, tp: float) -> BlockInput:
        return BlockInput(
            open_positions=0, max_positions=1,
            lots=0.01, risk_pct=0.005,
            sl_distance_pct=0.10, sl_distance_price=4.0,
            tp_distance_price=tp, spread_price=0.20,  # mock 20 points
            equity=100.0, margin_required=2.0, free_margin=98.0,
        )

    def test_tp_below_cost_coverage_blocked(self):
        verdict = self._blocker().check(self._inp(tp=0.50))
        assert verdict.blocked and verdict.block_name == "cost_coverage", (
            f"TP $0.50 < 3 x $0.20 must block (friction eats the edge), got {verdict}"
        )

    def test_realistic_mr_tp_passes(self):
        # $0.70 (and any real ATR-scaled TP far above it) clears the gate
        verdict = self._blocker().check(self._inp(tp=0.70))
        assert not verdict.blocked, (
            f"TP $0.70 >= 3 x $0.20 must pass, got {verdict}"
        )