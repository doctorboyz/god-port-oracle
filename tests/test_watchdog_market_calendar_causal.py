"""Causal proof test: watchdog must not feed-stale-ALERT while the market is
simply closed (ISSUE-104), and its own tooling failures must not be written
as farm alerts.

Hypothesis
----------
Two false-positive classes poisoned the alert channel:

1. Calendar noise: gold futures stop printing M5 bars every weekend and every
   day 16:00-17:00 Chicago time, but scripts/farm_watchdog.py's check_feed
   compared bar age to a wall-clock limit unconditionally — 300+ "feed stale"
   alerts accumulated that were just the calendar (evidence 2026-10: 95
   alerts on Saturday the 10th alone, 3-4 every weekday at the break).
2. Self-noise (the ISSUE-104 original complaint): 'docker ps failed' /
   'docker logs failed' — the watchdog's own tooling breaking — was written
   to ψ/outbox/farm_alert_*.md as if it were a farm problem, training readers
   to distrust alert files.

Causal proof
------------
- Weekend + stale-by-hours feed → check_feed must return None (skipped).
  RED before the fix: it returned "feed stale: ...".
- Weekday maintenance break + stale feed → None. RED before the fix.
- Weekday market open + stale feed → still flagged (control: the real
  ISSUE-097 detection must survive).
- Sunday pre-open (21:xx UTC) stale → None; Sunday post-open (23:xx UTC)
  stale → flagged (control).
- Market calendar itself: boundary checks against the real feed's evidence
  (Friday 20:55 last bar, Sunday 22:00 first bar, in CDT).
- docker ps/logs failure → message carries SELF-ERROR and main() routes it
  to watchdog.log only — no farm_alert file, no macOS ping, no state write.
  RED before the fix: written to the alert file as a farm problem.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shared.market_calendar import market_open  # noqa: E402
from scripts import farm_watchdog as wd  # noqa: E402

UTC = timezone.utc


def _write_m5(path: Path, last_bar_utc: datetime, n: int = 5) -> None:
    lines = []
    for i in range(n):
        ts = last_bar_utc - timedelta(minutes=5 * i)
        lines.append(f"{ts.strftime('%Y-%m-%d %H:%M:%S')},4200,4210,4190,4205,100")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(reversed(lines)) + "\n")


def _stale_feed(monkeypatch, tmp_path, now: datetime) -> None:
    """Feed whose newest bar is 9h old at the SIMULATED now — stale under any
    30-min limit. The bar must anchor to the simulated clock, not the real
    one, or the age goes negative and tests pass for the wrong reason."""
    feed = tmp_path / "XAUUSD_M5.csv"
    _write_m5(feed, last_bar_utc=now.replace(tzinfo=None) - timedelta(hours=9))
    monkeypatch.setattr(wd, "FEED_M5", feed)


# ─── The calendar itself (verified against the farm's real feed, CDT) ─────


class TestMarketCalendar:
    """shared/market_calendar.py boundaries — evidence: 2026-10 real bars."""

    def test_friday_last_half_hour_open_then_closed(self):
        # Real feed 2026-10-09: last bar stamps 20:55 → 20:59 open, 21:00 closed
        assert market_open(datetime(2026, 10, 9, 20, 55, tzinfo=UTC)) is True
        assert market_open(datetime(2026, 10, 9, 21, 0, tzinfo=UTC)) is False

    def test_saturday_closed_all_day(self):
        assert market_open(datetime(2026, 10, 10, 0, 0, tzinfo=UTC)) is False
        assert market_open(datetime(2026, 10, 10, 12, 0, tzinfo=UTC)) is False
        assert market_open(datetime(2026, 10, 10, 23, 59, tzinfo=UTC)) is False

    def test_sunday_first_bar_then_open(self):
        # Real feed 2026-10-04: first Sunday bar stamps 22:00 (CDT open)
        assert market_open(datetime(2026, 10, 4, 21, 55, tzinfo=UTC)) is False
        assert market_open(datetime(2026, 10, 4, 22, 0, tzinfo=UTC)) is True

    def test_weekday_maintenance_break(self):
        # Real watchdog evidence 2026-10-05: bars stop 21:00 UTC, resume 22:00
        assert market_open(datetime(2026, 10, 5, 20, 59, tzinfo=UTC)) is True
        assert market_open(datetime(2026, 10, 5, 21, 0, tzinfo=UTC)) is False
        assert market_open(datetime(2026, 10, 5, 21, 59, tzinfo=UTC)) is False
        assert market_open(datetime(2026, 10, 5, 22, 0, tzinfo=UTC)) is True

    def test_mid_week_normal_hours_open(self):
        assert market_open(datetime(2026, 10, 7, 12, 0, tzinfo=UTC)) is True

    def test_naive_datetime_treated_as_utc(self):
        assert market_open(datetime(2026, 10, 7, 12, 0)) is True
        assert market_open(datetime(2026, 10, 10, 12, 0)) is False


# ─── Incident replay: calendar noise must vanish, real detection must stay ─


class TestFeedGateCausal:
    """check_feed must skip when the market is closed, flag when it isn't."""

    def test_saturday_stale_feed_not_flagged(self, monkeypatch, tmp_path):
        # The 95-alert day (2026-10-10): stale-by-hours feed on a closed market
        _stale_feed(monkeypatch, tmp_path, now=datetime(2026, 10, 10, 12, 0, tzinfo=UTC))
        problem = wd.check_feed(
            max_age_min=30.0, now=datetime(2026, 10, 10, 12, 0, tzinfo=UTC))
        assert problem is None, (
            f"market closed Saturday — a stale feed is the calendar, not a "
            f"farm problem. Got: {problem!r}"
        )

    def test_sunday_morning_stale_feed_not_flagged(self, monkeypatch, tmp_path):
        # Sunday before the 22:00 UTC open: same closed-market situation
        _stale_feed(monkeypatch, tmp_path, now=datetime(2026, 10, 11, 6, 0, tzinfo=UTC))
        problem = wd.check_feed(
            max_age_min=30.0, now=datetime(2026, 10, 11, 6, 0, tzinfo=UTC))
        assert problem is None, f"Sunday pre-open must not feed-alert. Got: {problem!r}"

    def test_sunday_pre_open_evening_not_flagged(self, monkeypatch, tmp_path):
        _stale_feed(monkeypatch, tmp_path, now=datetime(2026, 10, 11, 21, 30, tzinfo=UTC))
        problem = wd.check_feed(
            max_age_min=30.0, now=datetime(2026, 10, 11, 21, 30, tzinfo=UTC))
        assert problem is None, f"21:30 UTC Sunday = pre-open CDT. Got: {problem!r}"

    def test_sunday_post_open_stale_still_flagged(self, monkeypatch, tmp_path):
        # Control: after the real Sunday open, a stale feed IS a problem
        _stale_feed(monkeypatch, tmp_path, now=datetime(2026, 10, 11, 23, 30, tzinfo=UTC))
        problem = wd.check_feed(
            max_age_min=30.0, now=datetime(2026, 10, 11, 23, 30, tzinfo=UTC))
        assert problem is not None and "stale" in problem, (
            "post-open Sunday + genuinely stale feed must still alert — "
            "the gate must not swallow the real ISSUE-097 detection"
        )

    def test_weekday_maintenance_break_not_flagged(self, monkeypatch, tmp_path):
        # The 3-4 alerts every weekday 21:39-22:09 UTC: the daily break
        _stale_feed(monkeypatch, tmp_path, now=datetime(2026, 10, 5, 21, 45, tzinfo=UTC))
        problem = wd.check_feed(
            max_age_min=30.0, now=datetime(2026, 10, 5, 21, 45, tzinfo=UTC))
        assert problem is None, f"daily maintenance break must not alert. Got: {problem!r}"

    def test_friday_after_close_not_flagged(self, monkeypatch, tmp_path):
        _stale_feed(monkeypatch, tmp_path, now=datetime(2026, 10, 9, 21, 30, tzinfo=UTC))
        problem = wd.check_feed(
            max_age_min=30.0, now=datetime(2026, 10, 9, 21, 30, tzinfo=UTC))
        assert problem is None, f"Friday post-close must not alert. Got: {problem!r}"

    def test_weekday_open_stale_still_flagged(self, monkeypatch, tmp_path):
        # Control: market open + genuinely stale feed → still flagged
        _stale_feed(monkeypatch, tmp_path, now=datetime(2026, 10, 7, 12, 0, tzinfo=UTC))
        problem = wd.check_feed(
            max_age_min=30.0, now=datetime(2026, 10, 7, 12, 0, tzinfo=UTC))
        assert problem is not None and "stale" in problem, (
            "weekday market-open + stale feed is the watchdog's whole purpose"
        )

    def test_missing_feed_closed_market_not_flagged(self, monkeypatch, tmp_path):
        monkeypatch.setattr(wd, "FEED_M5", tmp_path / "does_not_exist.csv")
        problem = wd.check_feed(
            max_age_min=30.0, now=datetime(2026, 10, 10, 12, 0, tzinfo=UTC))
        assert problem is None, (
            "missing feed on a closed market is undecidable — the last "
            "market-open pass owns it. Got: {problem!r}"
        )


# ─── ISSUE-104 self-error routing: tooling failures ≠ farm problems ──────


class TestSelfErrorRoutingCausal:
    """docker CLI failures are the watchdog's own problem — log-only."""

    def test_docker_ps_failure_is_self_error(self, monkeypatch):
        # The 2026-10-02 incident class: launchd PATH had no docker
        monkeypatch.setattr(wd, "_docker_bin", lambda: "/nonexistent/docker")
        problem = wd.check_containers()
        assert problem is not None and problem.startswith("SELF-ERROR"), (
            f"docker ps failing is a tooling failure and must be tagged as "
            f"such, not presented as a farm problem. Got: {problem!r}"
        )

    def test_docker_logs_failure_is_self_error(self, monkeypatch):
        monkeypatch.setattr(wd, "_docker_bin", lambda: "/nonexistent/docker")
        problem = wd.check_regression(
            since=datetime(2026, 10, 11, 0, 0, tzinfo=UTC).replace(tzinfo=None))
        assert problem is not None and problem.startswith("SELF-ERROR"), (
            f"docker logs failing is a tooling failure. Got: {problem!r}"
        )

    @pytest.fixture
    def _paths(self, monkeypatch, tmp_path):
        monkeypatch.setattr(wd, "OUTBOX", tmp_path / "outbox")
        monkeypatch.setattr(wd, "LOG", tmp_path / "watchdog.log")
        monkeypatch.setattr(wd, "STATE_FILE", tmp_path / ".watchdog_since")
        monkeypatch.setattr(wd, "_notify_macos", lambda message: None)
        wd.OUTBOX.mkdir(parents=True, exist_ok=True)
        return tmp_path

    def test_self_error_does_not_write_farm_alert(self, monkeypatch, _paths):
        # RED before the fix: 'docker ps failed' went into farm_alert_*.md
        monkeypatch.setattr(wd, "check_feed", lambda max_age_min, now: None)
        monkeypatch.setattr(
            wd, "check_containers", lambda: "SELF-ERROR: docker ps failed: boom")
        monkeypatch.setattr(wd, "check_regression", lambda since: None)

        code = wd.main()

        alerts = list(wd.OUTBOX.glob("farm_alert_*.md"))
        assert not alerts, (
            f"self-errors must stay out of the alert channel: {alerts}"
        )
        assert code == 2, f"self-error exit code should be distinct. Got {code}"
        log_text = wd.LOG.read_text()
        assert "SELF-ERROR" in log_text, "self-error must still hit watchdog.log"
        assert not wd.STATE_FILE.exists(), (
            "regression scan never ran — state must not advance past the gap"
        )

    def test_farm_problem_still_writes_alert(self, monkeypatch, _paths):
        # Control: a REAL farm problem keeps the full alert path
        monkeypatch.setattr(wd, "check_feed", lambda max_age_min, now: None)
        monkeypatch.setattr(
            wd, "check_containers",
            lambda: "container paper-fetcher NOT RUNNING")
        monkeypatch.setattr(wd, "check_regression", lambda since: None)

        code = wd.main()

        alerts = list(wd.OUTBOX.glob("farm_alert_*.md"))
        assert len(alerts) == 1 and "NOT RUNNING" in alerts[0].read_text()
        assert code == 1

    def test_clean_pass_advances_state(self, monkeypatch, _paths):
        monkeypatch.setattr(wd, "check_feed", lambda max_age_min, now: None)
        monkeypatch.setattr(wd, "check_containers", lambda: None)
        monkeypatch.setattr(wd, "check_regression", lambda since: None)

        assert wd.main() == 0
        assert wd.STATE_FILE.exists(), "all-healthy pass must advance the state"

# ─── Exness calendar + era-aware gate (feed switch 2026-10-30) ─────────────


def _set_era(monkeypatch, tmp_path, era: str) -> None:
    """Point the watchdog at an era marker with the given content."""
    marker = tmp_path / "XAUUSD_M5.csv.era"
    if era is None:
        marker.unlink(missing_ok=True)
    else:
        marker.write_text(era + "\n")
    monkeypatch.setattr(wd, "FEED_ERA", marker)


class TestExnessCalendar:
    """market_open(feed="exness") boundaries — evidence: 3 months of real
    bridge M5 bars grouped by day (2026-08-31..10-30, see module docstring).
    Gap labels in the evidence are the first bar AFTER the silence: the
    22:00 weekday bar follows a 65-min diff from 20:55 → halt is
    21:00-22:00 UTC, NOT 22:05-23:05 (the diff-label direction is easy to
    misread — that misreading is why this class pins the exact minutes)."""

    def test_thursday_daily_halt_boundaries(self):
        # Real bars Thu 2026-10-29: 20:55 last before the halt, 22:00 first after
        assert market_open(datetime(2026, 10, 29, 20, 59, tzinfo=UTC), feed="exness") is True
        assert market_open(datetime(2026, 10, 29, 21, 0, tzinfo=UTC), feed="exness") is False
        assert market_open(datetime(2026, 10, 29, 21, 59, tzinfo=UTC), feed="exness") is False
        assert market_open(datetime(2026, 10, 29, 22, 0, tzinfo=UTC), feed="exness") is True

    def test_friday_closes_21_utc(self):
        # Real bars Fri 2026-10-16: last bar opens 20:55, weekend from 21:00
        assert market_open(datetime(2026, 10, 16, 20, 59, tzinfo=UTC), feed="exness") is True
        assert market_open(datetime(2026, 10, 16, 21, 0, tzinfo=UTC), feed="exness") is False
        # …and Friday 22:00 has NO bar on Exness either
        assert market_open(datetime(2026, 10, 16, 22, 0, tzinfo=UTC), feed="exness") is False

    def test_sunday_opens_22_utc_no_evening_halt(self):
        # Real bars Sun 2026-10-18: first bar 22:00, continuous to 23:55
        assert market_open(datetime(2026, 10, 18, 21, 59, tzinfo=UTC), feed="exness") is False
        assert market_open(datetime(2026, 10, 18, 22, 0, tzinfo=UTC), feed="exness") is True
        assert market_open(datetime(2026, 10, 18, 22, 30, tzinfo=UTC), feed="exness") is True, (
            "Sunday evening has no daily halt on Exness — 24 continuous bars"
        )

    def test_saturday_closed_all_day(self):
        assert market_open(datetime(2026, 10, 17, 12, 0, tzinfo=UTC), feed="exness") is False

    def test_monday_midday_open(self):
        assert market_open(datetime(2026, 10, 19, 10, 0, tzinfo=UTC), feed="exness") is True

    def test_unknown_feed_value_falls_back_to_cme(self):
        assert market_open(datetime(2026, 10, 29, 21, 30, tzinfo=UTC), feed="nonsense") is False


class TestFeedEraGateCausal:
    """The watchdog must price the stale gate with the calendar of the feed
    that OWNED the history (era marker from the fetcher) — bridge era uses
    Exness hours, legacy/yfinance era keeps CME hours.

    During CDT the two calendars coincide in UTC (both halt 21:00-22:00,
    both open Sunday 22:00) — the era marker only changes behavior after
    the 2026-11-01 US DST end, when CME hours shift an hour later in UTC
    and the GMT+0 Exness hours stay put. The causal pin therefore uses
    POST-DST dates, where the same instant genuinely flips with the era:
      Thu 2026-11-05 21:30 UTC — Exness: halted · CME (CST break 22-23): open
      Thu 2026-11-05 22:30 UTC — Exness: open    · CME (CST break 22-23): halted
    """

    def _at(self, monkeypatch, tmp_path, now: datetime, era: str | None):
        _stale_feed(monkeypatch, tmp_path, now=now)
        _set_era(monkeypatch, tmp_path, era)
        return wd.check_feed(max_age_min=30.0, now=now)

    def test_exness_halt_not_flagged_bridge_era(self, monkeypatch, tmp_path):
        # Post-DST Thursday 21:30 UTC: Exness daily halt → skip
        now = datetime(2026, 11, 5, 21, 30, tzinfo=UTC)
        assert self._at(monkeypatch, tmp_path, now, era="bridge") is None, (
            "Exness halt 21:00-22:00 UTC must not alert in bridge era"
        )

    def test_same_instant_cme_era_still_flags(self, monkeypatch, tmp_path):
        # Post-DST Thursday 21:30 UTC is OPEN on the CME calendar (CST break
        # 22:00-23:00) — a yfinance-era feed this stale IS a problem.
        now = datetime(2026, 11, 5, 21, 30, tzinfo=UTC)
        problem = self._at(monkeypatch, tmp_path, now, era="yfinance")
        assert problem is not None and "stale" in problem

    def test_cme_break_open_on_exness_bridge_era(self, monkeypatch, tmp_path):
        # Post-DST Thursday 22:30 UTC: CME break, but Exness is OPEN —
        # bridge era must still detect a stale feed.
        now = datetime(2026, 11, 5, 22, 30, tzinfo=UTC)
        problem = self._at(monkeypatch, tmp_path, now, era="bridge")
        assert problem is not None and "stale" in problem, (
            "Exness is open at 22:30 UTC Thursday post-DST — stale must alert"
        )

    def test_cme_break_not_flagged_yfinance_era(self, monkeypatch, tmp_path):
        # Control: same instant, yfinance era → CME break → skip
        now = datetime(2026, 11, 5, 22, 30, tzinfo=UTC)
        assert self._at(monkeypatch, tmp_path, now, era="yfinance") is None

    def test_missing_marker_defaults_to_cme(self, monkeypatch, tmp_path):
        # No marker file (legacy history) → CME calendar: post-DST 21:30
        # Thursday is CME-open → a stale feed must alert.
        now = datetime(2026, 11, 5, 21, 30, tzinfo=UTC)
        problem = self._at(monkeypatch, tmp_path, now, era=None)
        assert problem is not None and "stale" in problem

    def test_open_market_stale_still_flagged_bridge_era(self, monkeypatch, tmp_path):
        # The required control (task T3): bars SHOULD exist but don't → ALERT
        now = datetime(2026, 10, 30, 10, 0, tzinfo=UTC)  # Friday, open
        problem = self._at(monkeypatch, tmp_path, now, era="bridge")
        assert problem is not None and "stale" in problem, (
            "market open + no fresh bars is the watchdog's whole purpose"
        )
