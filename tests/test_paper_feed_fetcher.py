"""Unit tests for the brokerless paper-feed fetcher (no network — fetch mocked).

Covers the four invariants the farm depends on:
1. Only closed bars are written (never act on a forming bar).
2. Merge-dedup: re-fetching overlapping data never duplicates timestamps.
3. A failed fetch keeps the previous CSV (stale beats empty).
4. Nothing new → no rewrite (idempotent polling).
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import paper_feed_fetcher as pff  # noqa: E402


def _bars(start: str, n: int, freq="5min", base=4200.0):
    idx = pd.date_range(start, periods=n, freq=freq)
    closes = pd.Series([base + i * 0.5 for i in range(n)], index=idx)
    return pd.DataFrame(
        {"open": closes, "high": closes + 1, "low": closes - 1,
         "close": closes, "volume": [100.0] * n},
        index=idx,
    )


def _read(feed_dir: Path, tf="M5") -> pd.DataFrame:
    return pd.read_csv(
        feed_dir / f"XAUUSD_{tf}.csv", header=None,
        names=["date", "open", "high", "low", "close", "volume"],
        parse_dates=["date"],
    ).set_index("date")


class TestPaperFeedFetcher:
    def test_unclosed_bar_dropped(self, tmp_path, monkeypatch):
        # Last bar opens 00:58... only 3 closed bars at "now" 01:00
        fetched = _bars("2026-09-30 00:45", n=4)  # 00:45..01:00
        monkeypatch.setattr(pff, "_now_utc", lambda: datetime(2026, 9, 30, 1, 0))
        monkeypatch.setattr(pff, "_fetch_with_retry", lambda tf, period: fetched)

        changed = pff.update_timeframe(tmp_path, "M5", seed=True)
        assert changed is True
        out = _read(tmp_path)
        assert list(out.index) == [
            pd.Timestamp("2026-09-30 00:45"),
            pd.Timestamp("2026-09-30 00:50"),
            pd.Timestamp("2026-09-30 00:55"),
        ], "forming bar (00:58+) must never be written"

    def test_merge_dedup_last_write_wins(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pff, "_now_utc", lambda: datetime(2026, 9, 30, 12, 0))
        day1 = _bars("2026-09-29 22:00", n=3, base=4200.0)
        monkeypatch.setattr(pff, "_fetch_with_retry", lambda tf, period: day1)
        assert pff.update_timeframe(tmp_path, "M5", seed=True) is True

        # Overlapping fetch: same timestamps, corrected close values + new bar
        overlap = _bars("2026-09-29 22:00", n=5, base=4210.0)
        monkeypatch.setattr(pff, "_fetch_with_retry", lambda tf, period: overlap)
        assert pff.update_timeframe(tmp_path, "M5", seed=False) is True

        out = _read(tmp_path)
        assert out.index.duplicated().sum() == 0, "duplicate timestamps corrupt the feed"
        assert len(out) == 5
        # New values win over stale ones
        assert out["close"].iloc[0] == 4210.0

    def test_failed_fetch_keeps_previous(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pff, "_now_utc", lambda: datetime(2026, 9, 30, 12, 0))
        first = _bars("2026-09-29 22:00", n=3)
        monkeypatch.setattr(pff, "_fetch_with_retry", lambda tf, period: first)
        assert pff.update_timeframe(tmp_path, "M5", seed=True) is True
        before = _read(tmp_path)

        def _fail(**kwargs):
            raise ConnectionError("network down")

        # Patch the transport, not the retry wrapper — the real path retries
        # 3× (sleep patched to 0) and lands in the keep-previous branch.
        monkeypatch.setattr(pff, "fetch_xauusd", _fail)
        monkeypatch.setattr(pff, "FETCH_RETRY_SLEEP", 0)
        assert pff.update_timeframe(tmp_path, "M5", seed=False) is False
        after = _read(tmp_path)
        assert len(after) == len(before), "failed fetch must keep the previous CSV"

    def test_no_new_bars_no_rewrite(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pff, "_now_utc", lambda: datetime(2026, 9, 30, 12, 0))
        same = _bars("2026-09-29 22:00", n=3)
        monkeypatch.setattr(pff, "_fetch_with_retry", lambda tf, period: same)
        assert pff.update_timeframe(tmp_path, "M5", seed=True) is True
        mtime_first = (tmp_path / "XAUUSD_M5.csv").stat().st_mtime_ns
        assert pff.update_timeframe(tmp_path, "M5", seed=False) is False
        mtime_second = (tmp_path / "XAUUSD_M5.csv").stat().st_mtime_ns
        assert mtime_first == mtime_second, "unchanged data must not rewrite the file"

    def test_h1_unclosed_bar_age_threshold(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pff, "_now_utc", lambda: datetime(2026, 9, 30, 1, 0))
        fetched = _bars("2026-09-30 00:00", n=2, freq="h", base=4200.0)  # 00:00, 01:00
        monkeypatch.setattr(pff, "_fetch_with_retry", lambda tf, period: fetched)

        pff.update_timeframe(tmp_path, "H1", seed=True)
        out = _read(tmp_path, "H1")
        assert list(out.index) == [pd.Timestamp("2026-09-30 00:00")], (
            "H1 bar opened 01:00 is still forming at 01:00 and must be dropped"
        )

    # --- 2026-10-03 incident: farm blind all weekend (feed stale 40h) ---

    def _weekend_provider(self):
        """Simulates Yahoo's observed weekend behavior: bars come back only
        when the requested period window still covers the last (Friday)
        session; a window that has slid past it returns EMPTY."""
        friday = _bars("2026-10-02 19:00", n=24, base=4170.0)  # 19:00..20:55 UTC
        window_days = {"1d": 1, "5d": 5, "10d": 10, "60d": 60, "1y": 365, "2y": 730}

        def provider(tf, period):
            now = datetime(2026, 10, 4, 13, 0)  # Sunday afternoon
            if friday.index[0] >= now - pd.Timedelta(days=window_days[period]):
                return friday
            return pd.DataFrame()  # Yahoo-observed: empty response
        return provider

    def test_m5_delta_window_spans_weekend_closure(self, tmp_path, monkeypatch):
        """The M5 delta window must still cover Friday's session on Sunday,
        or the provider returns empty and the feed goes stale all weekend."""
        monkeypatch.setattr(pff, "_now_utc", lambda: datetime(2026, 10, 4, 13, 0))
        monkeypatch.setattr(pff, "_fetch_with_retry", self._weekend_provider())

        assert pff.update_timeframe(tmp_path, "M5", seed=False) is True, (
            "delta window no longer covers the last closed session — "
            "the farm goes blind from Saturday until Sunday reopen"
        )
        out = _read(tmp_path)
        assert out.index[-1] == pd.Timestamp("2026-10-02 20:55"), (
            "Friday's last closed bar must reach the feed"
        )

    def test_one_day_window_blinds_farm_over_weekend(self, tmp_path, monkeypatch):
        """Replays the 2026-10-03 incident: with a 1d delta window, Yahoo
        returned empty from ~24h after Friday's close and the fetcher
        (correctly) kept the stale CSV — nobody could fetch anything."""
        monkeypatch.setattr(pff, "_now_utc", lambda: datetime(2026, 10, 4, 13, 0))
        monkeypatch.setattr(pff, "_fetch_with_retry", self._weekend_provider())
        monkeypatch.setitem(pff.DELTA_PERIOD, "M5", "1d")

        assert pff.update_timeframe(tmp_path, "M5", seed=False) is False, (
            "1d window slid past Friday's session — provider went empty"
        )
        assert not (tmp_path / "XAUUSD_M5.csv").exists(), "no bars ever reached the feed"

class TestBridgeFeedSource:
    """Bridge mode (2026-10-30): Exness XAUUSD realtime from the VPS bridge.

    The invariants beyond the four above:
    5. Era switch replaces history wholesale — GC=F futures and XAUUSD spot
       differ by dollars of basis; one merged file would plant a fake gap
       that corrupts Boll/ATR/ADX across the whole farm.
    6. Bridge down NEVER falls back to yfinance (same anti-mix reason) —
       retry only, engines' stale guard blocks trading meanwhile.
    """

    NOW = datetime(2026, 10, 30, 12, 0)

    def _bridge_history(self, start="2026-10-25 00:00", n=1440, base=3990.0):
        return _bars(start, n=n, base=base)

    def _seed_yfinance_era(self, tmp_path, tf="M5"):
        """Futures-scale history on disk, no era marker = legacy yfinance."""
        old = _bars("2026-10-01 00:00", n=100, base=3960.0)
        pff._write_atomic(tmp_path, tf, old)
        return old

    def test_era_switch_replaces_yfinance_history(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pff, "_now_utc", lambda: self.NOW)
        self._seed_yfinance_era(tmp_path)
        history = self._bridge_history()
        monkeypatch.setattr(pff, "_bridge_fetch", lambda tf, count: history)

        assert pff.update_timeframe(tmp_path, "M5", seed=True, source="bridge") is True
        out = _read(tmp_path)
        assert out.index[0] == history.index[0], "old yfinance bars must be GONE"
        assert len(out) == len(history)
        assert pff._read_era(tmp_path, "M5") == "bridge"

    def test_seed_requests_full_history_depth(self, tmp_path, monkeypatch):
        """Era-switch seed must pull >= 5 days of M5 (task bar) — the farm's
       league stats need continuity, not a 300-bar delta."""
        asked = {}
        monkeypatch.setattr(pff, "_now_utc", lambda: self.NOW)

        def _recorder(tf, count):
            asked["count"] = count
            return self._bridge_history()

        monkeypatch.setattr(pff, "_bridge_fetch", _recorder)
        pff.update_timeframe(tmp_path, "M5", seed=True, source="bridge")
        assert asked["count"] == pff.BRIDGE_SEED_COUNT["M5"]
        assert asked["count"] >= 1440, "5 days of M5 = 1440 bars minimum"

    def test_bridge_down_keeps_history_and_never_calls_yfinance(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pff, "_now_utc", lambda: self.NOW)
        old = self._seed_yfinance_era(tmp_path)
        monkeypatch.setattr(pff, "FETCH_RETRY_SLEEP", 0)
        monkeypatch.setattr(pff, "_bridge_fetch", lambda tf, count: None)

        def _no_yfinance(tf, period):
            raise AssertionError("bridge mode must NEVER fall back to yfinance")

        monkeypatch.setattr(pff, "_fetch_with_retry", _no_yfinance)
        assert pff.update_timeframe(tmp_path, "M5", seed=True, source="bridge") is False
        after = _read(tmp_path)
        assert len(after) == len(old), "failed bridge fetch must keep the previous CSV"
        assert pff._read_era(tmp_path, "M5") == "yfinance", (
            "era marker must NOT move on a failed fetch — the file is still "
            "yfinance-era data, and a moved marker would let later bridge "
            "bars merge onto it"
        )

    def test_bridge_down_after_switch_keeps_bridge_history(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pff, "_now_utc", lambda: self.NOW)
        history = self._bridge_history()
        monkeypatch.setattr(pff, "_bridge_fetch", lambda tf, count: history)
        pff.update_timeframe(tmp_path, "M5", seed=True, source="bridge")
        monkeypatch.setattr(pff, "FETCH_RETRY_SLEEP", 0)
        monkeypatch.setattr(pff, "_bridge_fetch", lambda tf, count: None)
        monkeypatch.setattr(pff, "_fetch_with_retry",
                            lambda tf, period: (_ for _ in ()).throw(
                                AssertionError("no yfinance fallback")))

        assert pff.update_timeframe(tmp_path, "M5", seed=False, source="bridge") is False
        assert len(_read(tmp_path)) == len(history), "stale beats empty"

    def test_delta_never_merges_across_era(self, tmp_path, monkeypatch):
        """A bridge delta arriving while the file is still yfinance-era must
        refuse to write — 300 bars on a futures-scale history IS the mix."""
        monkeypatch.setattr(pff, "_now_utc", lambda: self.NOW)
        old = self._seed_yfinance_era(tmp_path)
        delta = _bars("2026-10-30 10:00", n=5, base=3990.0)
        monkeypatch.setattr(pff, "_bridge_fetch", lambda tf, count: delta)

        assert pff.update_timeframe(tmp_path, "M5", seed=False, source="bridge") is False
        assert len(_read(tmp_path)) == len(old), "cross-era merge must be refused"
        assert pff._read_era(tmp_path, "M5") == "yfinance"

    def test_incremental_merge_within_era(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pff, "_now_utc", lambda: self.NOW)
        history = self._bridge_history()
        monkeypatch.setattr(pff, "_bridge_fetch", lambda tf, count: history)
        pff.update_timeframe(tmp_path, "M5", seed=True, source="bridge")

        newer = _bars(history.index[-1], n=6, base=4000.0)  # overlap + 5 new
        monkeypatch.setattr(pff, "_bridge_fetch", lambda tf, count: newer)
        assert pff.update_timeframe(tmp_path, "M5", seed=False, source="bridge") is True
        out = _read(tmp_path)
        assert out.index.duplicated().sum() == 0
        assert len(out) == len(history) + 5
        assert out.index[-1] == newer.index[-1]

    def test_unclosed_bridge_bar_dropped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pff, "_now_utc", lambda: datetime(2026, 10, 30, 12, 3))
        # last bar opens 12:00 — still forming at 12:03
        fetched = _bars("2026-10-30 11:50", n=4)  # 11:50..12:05
        monkeypatch.setattr(pff, "_bridge_fetch", lambda tf, count: fetched)
        pff.update_timeframe(tmp_path, "M5", seed=True, source="bridge")
        out = _read(tmp_path)
        assert out.index[-1] == pd.Timestamp("2026-10-30 11:55"), (
            "forming bridge bar must never be written — paper acts on closed bars only"
        )

    def test_yfinance_mode_unchanged_with_no_marker(self, tmp_path, monkeypatch):
        """Legacy mode regression guard: with no era marker and yfinance
        source, seeding still MERGES (no era switch, no rewrite churn)."""
        monkeypatch.setattr(pff, "_now_utc", lambda: self.NOW)
        old = self._seed_yfinance_era(tmp_path)
        fresh = _bars("2026-10-29 22:00", n=50, base=3961.0)
        monkeypatch.setattr(pff, "_fetch_with_retry", lambda tf, period: fresh)
        assert pff.update_timeframe(tmp_path, "M5", seed=True, source="yfinance") is True
        out = _read(tmp_path)
        assert len(out) == len(old) + len(fresh), (
            "within-era seeding must still merge-dedup, not replace"
        )


class TestBridgeSharedTerminalCausal:
    """Causal proof: mt5b is a SHARED terminal — the demo engine's per-cycle
    root.shutdown() kills the process-global MT5 handle while this fetcher
    holds its persistent connection. The symptom (seen live 2026-10-30
    10:02 UTC): every fetch returns no data and never recovers, because the
    rpyc connection itself is still alive so nothing reconnects.

    Hypothesis: BridgeFeed.fetch must re-initialize the terminal (idempotent,
    READ path only — the fetcher itself never calls shutdown) when the copy
    returns no data, then retry once.
    """

    class _FakeRoot:
        """Bridge service stand-in: copy_rates works only while 'initialized'."""

        def __init__(self):
            self.initialized = True
            self.copy_calls = 0

        def initialize(self):
            self.initialized = True
            return True

        def symbol_select(self, name, enable=True):
            return True

        def symbol_info(self, name):
            return {"name": name}

        def copy_rates_from_pos_json(self, symbol, tf, pos, count):
            if not self.initialized:
                # The REAL server (docker/mt5/mt5_bridge_server.py) json-dumps
                # whatever MT5 returned — a shut-down module yields an EMPTY
                # ARRAY, i.e. the truthy string "[]", not None. That is the
                # exact trap that kept the first fix from firing live.
                return "[]"
            self.copy_calls += 1
            import json
            return json.dumps([
                {"time": 1799000000 + i * 300, "open": 3990.0, "high": 3991.0,
                 "low": 3989.0, "close": 3990.5, "tick_volume": 100.0,
                 "spread": 20, "real_volume": 0.0}
                for i in range(count)
            ])

    class _FakeConn:
        def __init__(self, root):
            self.root = root

        def close(self):
            pass

    def _feed(self):
        root = self._FakeRoot()
        feed = pff.BridgeFeed("fake", 1)
        feed._conn = self._FakeConn(root)
        feed._resolved = "XAUUSD"
        return feed, root

    def test_foreign_shutdown_is_healed_by_reinitialize(self):
        import pandas as pd
        feed, root = self._feed()
        assert feed.fetch("M5", 5) is not None, "precondition: healthy fetch"

        root.initialized = False  # the demo engine's cycle just called shutdown()
        df = feed.fetch("M5", 5)
        assert df is not None and not df.empty, (
            "RED before the fix: a foreign mt5.shutdown() killed the shared "
            "handle and the fetcher never recovered — the farm feed froze"
        )
        assert root.initialized is True, "fetch must re-initialize the terminal"
        assert root.copy_calls == 2, "retry after re-init must actually copy"

    def test_reinit_failure_drops_connection(self):
        feed, root = self._feed()
        root.initialized = False
        root.initialize = lambda: False  # terminal unreachable
        assert feed.fetch("M5", 5) is None
        assert feed._conn is None, "a dead terminal must drop the connection"
