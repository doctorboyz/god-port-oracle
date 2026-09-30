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