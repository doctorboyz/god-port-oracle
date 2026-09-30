"""Causal proof test: stale CSV feed must block new entries, keep monitoring.

Hypothesis
----------
The brokerless paper farm trades whatever the fetcher last wrote. A failed
fetch intentionally keeps the previous CSV ("stale beats none"), but nothing
checks bar AGE: if Yahoo is rate-limited for hours, every engine keeps
evaluating the SAME old M5 bar every cycle — entries (and the paper equity
mark) run against a price that is hours old, and the league table fills with
trades the real market never offered.

Fix: MAX_CANDLE_AGE_SECONDS{_<ACCOUNT>} (0/unset = off, legacy unchanged).
When the last closed M5 bar is older than the limit, run_once must skip the
SIGNAL path but still run _monitor_positions (open positions must not be
abandoned just because the feed froze — time stops/SL/TP still evaluate
against the best data we have).

Causal proof
------------
MAX_CANDLE_AGE_SECONDS_P11=1800 + M5 CSV whose last bar is 3h old →
run_once returns skip with "stale" reason AND _monitor_positions ran.
Controls: fresh bar (5 min) → no stale skip; env unset → no stale skip
(legacy behavior preserved).

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
    get_account_id_by_name,
    init_db,
    insert_account,
)
from metty.execution.live_trader import LiveTrader, RiskConfig  # noqa: E402

_M5_CSV = "XAUUSD_M5.csv"
_TS = datetime.now(timezone.utc).isoformat()


@pytest.fixture()
def p11_db(tmp_path):
    db = tmp_path / "stale_test.db"
    init_db(db)
    insert_account(
        name="P11", balance=100.0, leverage=2000,
        bridge_host="none", bridge_port=8001, signal_group="portfolio",
        db_path=db,
    )
    return db


def _write_m5(data_dir: Path, last_bar_utc: datetime, n: int = 120) -> None:
    """M5 CSV whose newest bar opens at last_bar_utc (premium format, no header)."""
    idx = pd.date_range(last_bar_utc - timedelta(minutes=5 * (n - 1)), periods=n, freq="5min")
    closes = pd.Series([4000.0 + i * 0.5 for i in range(n)], index=idx)
    df = pd.DataFrame({
        "open": closes, "high": closes + 1.0, "low": closes - 1.0,
        "close": closes, "volume": [100.0] * n,
    })
    df.index.name = "date"
    df.to_csv(data_dir / _M5_CSV, header=False, float_format="%.3f")


def _make_trader(monkeypatch, tmp_path, db, data_dir, stale_limit: str | None):
    """P11 paper trader, bridge forced down, feed at data_dir.

    ACCOUNTS=P11 mirrors the farm container (get_bridge_config must resolve;
    the bridge call itself fails on the nonexistent mt5p11 host and the
    trader falls back to the CSV feed).
    """
    monkeypatch.setenv("ACCOUNTS", "P11")
    # Fail the bridge fast, same as the farm compose (3×5s retries would hang
    # the test for minutes on the nonexistent mt5p11 host)
    monkeypatch.setenv("MT5_BRIDGE_MAX_RETRIES", "1")
    monkeypatch.setenv("MT5_BRIDGE_RETRY_DELAY", "0")
    monkeypatch.setenv("INITIAL_EQUITY_P11", "100")
    if stale_limit is not None:
        monkeypatch.setenv("MAX_CANDLE_AGE_SECONDS_P11", stale_limit)
    t = LiveTrader(
        account="P11", db_path=db, data_dir=data_dir, dry_run=True,
        risk_config=RiskConfig(risk_per_trade=0.01),
    )
    from metty.bridge.client import MT5Bridge
    monkeypatch.setattr(MT5Bridge, "fetch_account_info_sync", lambda self: None)
    return t


class TestFeedStalenessCausal:
    """Stale feed + age limit → skip entries, keep monitoring; controls pass."""

    def test_stale_feed_skips_entries_but_monitors(self, monkeypatch, tmp_path, p11_db):
        # Arrange: last M5 bar 3h old, limit 1800s (30 min)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        _write_m5(tmp_path, last_bar_utc=now - timedelta(hours=3))
        t = _make_trader(monkeypatch, tmp_path, p11_db, tmp_path, "1800")

        calls = {"n": 0}

        def _spy(candles):
            calls["n"] += 1
            return []

        monkeypatch.setattr(t, "_monitor_positions", _spy)

        # Act
        result = t.run_once()

        # Assert: entries blocked with a stale reason; monitoring still ran
        assert "stale" in result.get("reason", ""), (
            f"3h-old feed must skip entries with a stale reason, got {result}"
        )
        assert calls["n"] == 1, "open positions must still be monitored on stale feed"

    def test_fresh_feed_not_blocked(self, monkeypatch, tmp_path, p11_db):
        # Control: bar 5 min old (normal GC=F delay) → no stale skip
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        _write_m5(tmp_path, last_bar_utc=now - timedelta(minutes=5))
        t = _make_trader(monkeypatch, tmp_path, p11_db, tmp_path, "1800")

        result = t.run_once()
        assert "stale" not in result.get("reason", ""), (
            f"fresh feed must not be blocked as stale, got {result}"
        )

    def test_env_unset_stale_feed_ignored(self, monkeypatch, tmp_path, p11_db):
        # Control: no env → legacy behavior, stale bar processed normally
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        _write_m5(tmp_path, last_bar_utc=now - timedelta(hours=3))
        t = _make_trader(monkeypatch, tmp_path, p11_db, tmp_path, None)

        result = t.run_once()
        assert "stale" not in result.get("reason", ""), (
            f"unset limit must disable the guard (legacy), got {result}"
        )