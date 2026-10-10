"""Causal proof test: the margin gate must read the leverage the farm actually
configures (ISSUE-100 class — MT5_LEVERAGE_* silent env no-op).

Hypothesis
----------
live_trader's margin estimate reads env MT5_LEVERAGE_{account} (default 100),
but every compose file sets LEVERAGE_{account} — the registry's env name
(docker-compose.paper-farm.yml: LEVERAGE_P11..P20=2000, docker-compose.vps.yml:
LEVERAGE_D=500, farm_variants.yml: LEVERAGE "2000"). Nothing anywhere sets
MT5_LEVERAGE_*, so the read is a silent no-op pinned at 1:100.

The incident (farm oracle-engine-farm1, 2026-10-06..09): equity ~$90, lot
floored at 0.01 → margin_required = 0.01 x 100 oz x $4220 / 100 = $42.20
> 0.8 x free_margin ($47.80) → trade_blocker:margin_safety blocked ~306 real
conf=0.65 signals that had passed every other gate. At the configured 1:2000
the same trade needs $2.11 — the gate passes.

Fix: ONE helper get_account_leverage() in metty.core.account_registry reading
LEVERAGE_{name} (the name compose + the registry itself already use), wired
into BOTH live_trader call sites (entry gate 6b + scale-in margin estimate)
AND the ported gate in m5_scalp_trader.

Causal proof
------------
1. Incident replay: LEVERAGE_P11=2000 + equity 90 + signal @4220 → the trade
   MUST open. RED now: margin_safety hold — the exact ~306 blocked signals.
2. Helper parity: get_account_leverage() reads the same name the registry
   seeds into the DB (P11 → 2000). RED now: helper doesn't exist.
3. Controls (must keep passing — the fix must not blind the gate):
   - LEVERAGE_P11=100 explicitly → still blocked (1:100 really is 1:100)
   - no env at all → legacy default 100 → still blocked
   - malformed env → safe fallback to default → still blocked

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

from metty.core.db import get_open_trades, init_db, insert_account  # noqa: E402
from metty.execution.live_trader import CONTRACT_SIZE, LiveTrader, RiskConfig  # noqa: E402
from shared.models import Signal, SignalType  # noqa: E402

_M5_CSV = "XAUUSD_M5.csv"
# The incident scenario, exactly as Hermes measured from the real farm logs.
PRICE = 4220.0      # gold on 2026-10-06..09
EQUITY = 90.0       # farm equity after the opening days' losses
LOT = 0.01          # risk-based size floors to min lot
# margin at the buggy 1:100 default vs the configured 1:2000:
MARGIN_100 = LOT * CONTRACT_SIZE * PRICE / 100    # $42.20 — blocks
MARGIN_2000 = LOT * CONTRACT_SIZE * PRICE / 2000   # $2.11  — passes


@pytest.fixture()
def farm_db(tmp_path):
    db = tmp_path / "leverage_env_test.db"
    init_db(db)
    insert_account(
        name="P11", balance=EQUITY, leverage=2000,
        bridge_host="none", bridge_port=8001, signal_group="portfolio",
        db_path=db,
    )
    return db


def _write_m5(data_dir: Path, last_close: float = PRICE) -> pd.DataFrame:
    """M5 CSV whose ATR keeps risk lots at the 0.01 floor (the farm's reality)."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    n = 120
    idx = pd.date_range(now - timedelta(minutes=5 * (n - 1)), periods=n, freq="5min")
    closes = pd.Series([PRICE - 30.0 + i * 0.25 for i in range(n)], index=idx)
    closes.iloc[-1] = last_close
    df = pd.DataFrame({
        "open": closes, "high": closes + 0.05, "low": closes - 0.05,
        "close": closes, "volume": [100.0] * n,
    })
    df.index.name = "date"
    df.to_csv(data_dir / _M5_CSV, header=False, float_format="%.3f")
    return df


def _make_trader(monkeypatch, tmp_path, db,
                 leverage: str | None = None,
                 leverage_name: str = "LEVERAGE_P11") -> LiveTrader:
    """Farm-shaped trader. `leverage` sets the env under `leverage_name`
    (default: the registry/compose name LEVERAGE_P11)."""
    for var in ("LEVERAGE_P11", "MT5_LEVERAGE_P11", "MT5_LEVERAGE", "LEVERAGE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ACCOUNTS", "P11")
    monkeypatch.setenv("MT5_BRIDGE_MAX_RETRIES", "1")
    monkeypatch.setenv("MT5_BRIDGE_RETRY_DELAY", "0")
    monkeypatch.setenv("INITIAL_EQUITY_P11", str(EQUITY))
    monkeypatch.setenv("INITIAL_BALANCE_P11", str(EQUITY))
    monkeypatch.setenv("ML_FILTER_ENABLED", "0")
    monkeypatch.setenv("MOCK_SPREAD_POINTS", "20")
    if leverage is not None:
        monkeypatch.setenv(leverage_name, leverage)
    t = LiveTrader(
        account="P11", db_path=db, data_dir=tmp_path, dry_run=True,
        risk_config=RiskConfig(risk_per_trade=0.01),
    )
    from metty.bridge.client import MT5Bridge
    monkeypatch.setattr(MT5Bridge, "fetch_account_info_sync", lambda self: None)
    # Entry must be deterministic on any weekday the suite runs on.
    monkeypatch.setattr(t, "_in_weekend_pre_close_window", lambda now=None: False)
    return t


def _force_signal(monkeypatch, t: LiveTrader, direction: str = "SELL") -> None:
    """conf=0.65 actionable signal at the incident price — the exact signal
    class margin_safety blocked ~306 times on the farm."""
    sig = Signal(
        signal_type=SignalType.BUY if direction == "BUY" else SignalType.SELL,
        confidence=0.65, price=PRICE,
        timestamp=datetime.now(timezone.utc),
        indicators={"trend_alignment": 1}, regime="ranging",
    )
    monkeypatch.setattr(t, "_generate_signal", lambda candles: sig)
    monkeypatch.setattr(t, "_get_calendar", lambda: [])


class TestIncidentReplay:
    """The 306 blocked signals: same scenario, leverage finally read."""

    def test_farm_leverage_2000_opens_the_previously_blocked_trade(
        self, monkeypatch, tmp_path, farm_db
    ):
        _write_m5(tmp_path)
        # docker-compose.paper-farm.yml sets exactly this for P11..P20.
        t = _make_trader(monkeypatch, tmp_path, farm_db, leverage="2000")
        _force_signal(monkeypatch, t, "SELL")

        result = t.run_once()
        trades = get_open_trades(t.account_id, farm_db)
        assert trades, (
            "LEVERAGE_P11=2000 (what compose actually sets) must let the real "
            f"conf=0.65 signal through the margin gate — got {result} "
            "(pre-fix the gate reads MT5_LEVERAGE_* default 100 → margin "
            f"${MARGIN_100:.2f} vs free margin ~${EQUITY - MARGIN_100:.2f} → blocked)"
        )
        assert trades[0]["lot_size"] == pytest.approx(LOT)

    def test_blocked_hold_is_the_margin_gate_reason(
        self, monkeypatch, tmp_path, farm_db
    ):
        """What the pre-fix code does with this exact scenario — replayed as a
        documented mechanism, so the RED state is pinned in the transcript."""
        _write_m5(tmp_path)
        # No LEVERAGE env at all → pre-fix AND post-fix: 1:100 default → blocked.
        t = _make_trader(monkeypatch, tmp_path, farm_db, leverage="100")
        _force_signal(monkeypatch, t, "SELL")

        result = t.run_once()
        assert not get_open_trades(t.account_id, farm_db)
        assert "margin" in (result.get("reason") or "").lower(), (
            f"1:100 leverage + equity ${EQUITY} + price ${PRICE} must block "
            f"on margin_safety, got {result}"
        )


class TestMarginMath:
    """The numbers Hermes reported, pinned as the mechanism."""

    def test_margin_required_drops_from_42_to_2_dollars(self):
        # At the buggy 1:100 default the gate demands $42.20; at the
        # configured 1:2000 it demands $2.11 — both from the same lot/price.
        assert MARGIN_100 == pytest.approx(42.20)
        assert MARGIN_2000 == pytest.approx(2.11)
        # Blocking condition at 1:100: 42.20 > 0.8 * (90 - 42.20) = 38.24.
        assert MARGIN_100 > 0.8 * (EQUITY - MARGIN_100)
        # Passing condition at 1:2000: 2.11 << 0.8 * (90 - 2.11) = 70.31.
        assert MARGIN_2000 < 0.8 * (EQUITY - MARGIN_2000)

    def test_helper_reads_the_registry_env_name(self, monkeypatch, tmp_path, farm_db):
        from metty.core.account_registry import get_account_leverage
        monkeypatch.delenv("MT5_LEVERAGE_P11", raising=False)
        monkeypatch.delenv("MT5_LEVERAGE", raising=False)
        monkeypatch.setenv("LEVERAGE_P11", "2000")
        assert get_account_leverage("P11") == 2000, (
            "the margin gate must read LEVERAGE_<NAME> — the one env name the "
            "registry and every compose file already use"
        )

    def test_helper_parity_with_registry_seeding(self, monkeypatch, tmp_path, farm_db):
        """get_account_leverage() and get_account_config() must agree — one
        env name, one meaning, no second head to drift out of sync."""
        from metty.core.account_registry import get_account_config, get_account_leverage
        monkeypatch.setenv("ACCOUNTS", "P11")
        monkeypatch.setenv("LEVERAGE_P11", "2000")
        assert get_account_leverage("P11") == get_account_config("P11").leverage

    def test_helper_default_and_malformed_fallback(self, monkeypatch):
        from metty.core.account_registry import get_account_leverage
        monkeypatch.delenv("LEVERAGE_P12", raising=False)
        assert get_account_leverage("P12") == 100, (
            "legacy default 1:100 when nothing is set (unchanged behavior)"
        )
        monkeypatch.setenv("LEVERAGE_P12", "banana")
        assert get_account_leverage("P12") == 100, (
            "malformed env must fall back to the default, never crash the gate"
        )


class TestGateStaysArmed:
    """Controls: the fix must make the gate read leverage, not ignore it."""

    def test_explicit_1_100_still_blocks(self, monkeypatch, tmp_path, farm_db):
        _write_m5(tmp_path)
        t = _make_trader(monkeypatch, tmp_path, farm_db, leverage="100")
        _force_signal(monkeypatch, t, "SELL")

        result = t.run_once()
        assert not get_open_trades(t.account_id, farm_db), (
            f"LEVERAGE_P11=100 explicitly must still block — got {result}"
        )

    def test_no_env_still_blocks_at_legacy_default(
        self, monkeypatch, tmp_path, farm_db
    ):
        _write_m5(tmp_path)
        t = _make_trader(monkeypatch, tmp_path, farm_db, leverage=None)
        _force_signal(monkeypatch, t, "SELL")

        result = t.run_once()
        assert not get_open_trades(t.account_id, farm_db), (
            "no leverage configured → 1:100 default → margin_safety must hold "
            f"(gate behavior unchanged where nothing is set), got {result}"
        )