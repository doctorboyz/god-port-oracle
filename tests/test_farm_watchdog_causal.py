"""Causal proof test: watchdog must catch a stale feed (ISSUE-097).

The farm's MAX_CANDLE_AGE_SECONDS guard blocks ENTRIES on stale data but
alerts no one — a multi-hour Yahoo outage reads as "0 trades, all quiet"
in the league table. scripts/farm_watchdog.py is the durable launchd-side
alert channel (runs every 15 min with no agent session, ISSUE-094/084).

Causal proof
------------
M5 CSV whose newest bar is 3h old → check_feed flags "stale". Controls:
5-min-old bar (normal GC=F delay) → healthy; missing file → flagged;
alert writer accumulates entries into one file per day.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import farm_watchdog as wd  # noqa: E402


def _write_m5(path: Path, last_bar_utc: datetime, n: int = 5) -> None:
    lines = []
    for i in range(n):
        ts = last_bar_utc - timedelta(minutes=5 * i)
        lines.append(f"{ts.strftime('%Y-%m-%d %H:%M:%S')},4200,4210,4190,4205,100")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(reversed(lines)) + "\n")


class TestWatchdogFeedCausal:
    """Stale feed must produce an alert; fresh feed must stay silent."""

    def test_stale_feed_flagged(self, monkeypatch, tmp_path):
        feed = tmp_path / "XAUUSD_M5.csv"
        now = datetime.now(timezone.utc)
        _write_m5(feed, last_bar_utc=now.replace(tzinfo=None) - timedelta(hours=3))
        monkeypatch.setattr(wd, "FEED_M5", feed)

        problem = wd.check_feed(max_age_min=30.0, now=now)
        assert problem is not None and "stale" in problem, (
            "3h-old feed must be flagged — this is the whole point of the watchdog"
        )
        assert "180" in problem, f"age in minutes should be in the message: {problem}"

    def test_fresh_feed_healthy(self, monkeypatch, tmp_path):
        # Control: 5-min delay is normal for GC=F — must NOT alert
        feed = tmp_path / "XAUUSD_M5.csv"
        now = datetime.now(timezone.utc)
        _write_m5(feed, last_bar_utc=now.replace(tzinfo=None) - timedelta(minutes=5))
        monkeypatch.setattr(wd, "FEED_M5", feed)

        assert wd.check_feed(max_age_min=30.0, now=now) is None

    def test_missing_feed_flagged(self, monkeypatch, tmp_path):
        monkeypatch.setattr(wd, "FEED_M5", tmp_path / "does_not_exist.csv")
        now = datetime.now(timezone.utc)

        problem = wd.check_feed(max_age_min=30.0, now=now)
        assert problem is not None, "missing feed must be flagged, not ignored"

    def test_alert_file_accumulates(self, monkeypatch, tmp_path):
        # Two passes same day → one file, two timestamped entries
        monkeypatch.setattr(wd, "OUTBOX", tmp_path)
        wd._write_alert(["feed stale: 180 min"])
        wd._write_alert(["container paper-fetcher NOT RUNNING"])

        files = list(tmp_path.glob("farm_alert_*.md"))
        assert len(files) == 1, "one alert file per day"
        content = files[0].read_text()
        assert "feed stale" in content and "NOT RUNNING" in content
        assert content.count("**") >= 4, "both entries must be in the file"