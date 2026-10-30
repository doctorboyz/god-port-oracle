"""Market calendar — single source of truth for "is the market open".

ISSUE-104 (2026-10-11): the farm watchdog priced feed-stale alerts in wall-clock
time — gold stops printing M5 bars every weekend and during the daily
maintenance break, so 300+ alerts accumulated that were pure calendar noise
(the agent's launchd fired every 15 min while the market was simply closed).
Every consumer that asks "should fresh bars exist right now?" must ask THIS
module, not re-derive the calendar:

  - scripts/farm_watchdog.py      feed-stale gate (this fix)
  - ~/.hermes/scripts/farm_daily_check.py (Hermes imports this module in its
    next round — its local market_open() copy is hard-coded to winter hours)

Two calendars, selected by the feed the bars come from (farm feed switch
2026-10-30 — paper moved from yfinance GC=F to bridge XAUUSD; the watchdog
picks via the fetcher's era marker, XAUUSD_M5.csv.era):

  feed="cme" (default) — CME gold futures (GC=F), Chicago local, DST-shifted:
    - Trading week: Sunday 17:00 → Friday 16:00 America/Chicago
    - Daily maintenance break: 16:00-17:00 America/Chicago, every day
    - Saturday: closed all day
    Why Chicago-local instead of fixed UTC hours: CME publishes its schedule
    in Chicago time and shifts with US DST. Verified against the farm's own
    feed (2026-10, CDT = UTC-5): last Friday bar stamps 20:55 (close 21:00
    UTC), first Sunday bar stamps 22:00 (open 22:00 UTC), and the weekday
    break silences bars 21:00-22:00 UTC — one hour earlier than the fixed
    winter hours (Sun 23:00 → Fri 22:00 UTC, break 22:00-23:00) that hold
    after the November DST end. Hard-coding either fixed UTC schedule would
    false-alert ~4× every day for one half of the year; the local-time rule
    is correct in both.

  feed="exness" — Exness XAUUSD spot over the MT5 bridge (brokerless farm
    realtime feed, 2026-10-30). Fixed UTC hours — Exness server time is
    GMT+0 (verified 2026-10-30: newest bridge bar stamped 09:40 with real
    UTC 09:44, i.e. bar epochs ARE wall-clock UTC), so the schedule does
    not shift with US DST. Verified against three months of the bridge's
    own M5 bars (2026-08-31..10-30, grouped by day; gap labels = first bar
    AFTER the silence):
    - Sunday: opens 22:00 UTC, NO halt that evening (24 continuous bars
      22:00-23:55, every Sunday in the window)
    - Monday-Thursday: bars 00:00-23:55 minus the 21:00-22:00 halt — the
      20:55 bar is the last before it, the 22:00 bar the first after
      (12-bar hole, every weekday; also the US Labor Day early close
      2026-09-07 is visible in the evidence)
    - Friday: last bar opens 20:55, then the weekend (halt from 21:00)
    - Saturday: closed all day
    During US CDT this happens to coincide with the CME calendar in UTC —
    the two only diverge after the 2026-11-01 DST end (CME hours shift an
    hour later in UTC, Exness stays put). Re-verify with one bridge probe
    after 2026-11-01 — GMT+0 server time means no shift is expected, but
    the calendar is priced on evidence, not on expectation.

Not modeled: CME holiday half-days and Exness holiday variations (a
half-day prints bars until its early close, so the feed gate errs toward
checking, which is the safe direction).
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

__all__ = ["market_open", "market_open_now", "FEED_CALENDARS"]

_CHICAGO = ZoneInfo("America/Chicago")

# CME gold futures hours, Chicago local (Sun 17:00 open, Fri 16:00 close,
# daily 16:00-17:00 maintenance break, Saturday closed).
_OPEN_HOUR_SUNDAY = 17
_CLOSE_HOUR = 16
_BREAK_END_HOUR = 17

# Exness XAUUSD hours, minutes-since-midnight UTC (see module docstring):
# Sunday open 22:00, Friday close 21:00 (last bar 20:55), Mon-Thu daily halt
# 21:00-22:00 (last bar 20:55, first bar 22:00), Saturday closed, NO halt
# on Sunday evening.
_EXNESS_SUNDAY_OPEN_MIN = 22 * 60
_EXNESS_FRIDAY_CLOSE_MIN = 21 * 60
_EXNESS_BREAK_START_MIN = 21 * 60
_EXNESS_BREAK_END_MIN = 22 * 60

FEED_CALENDARS = ("cme", "exness")


def _as_chicago(t: datetime) -> datetime:
    """Interpret `t` as UTC (naive = UTC) and convert to Chicago local time."""
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(_CHICAGO)


def _as_utc(t: datetime) -> datetime:
    """Interpret `t` as UTC (naive = UTC) — Exness hours are fixed UTC."""
    if t.tzinfo is None:
        return t
    return t.astimezone(timezone.utc).replace(tzinfo=None)


def _cme_open(t: datetime) -> bool:
    """CME gold should be printing bars at UTC instant `t`.

    Weekday numbering (Chicago local): Mon=0 .. Sun=6.
      Sat (5)  — closed all day
      Sun (6)  — opens 17:00
      Fri (4)  — closes 16:00 (weekend starts, no late reopen)
      Mon-Thu  — open, minus the daily 16:00-17:00 maintenance break
    """
    local = _as_chicago(t)
    wd, h = local.weekday(), local.hour
    if wd == 5:
        return False
    if wd == 6:
        return h >= _OPEN_HOUR_SUNDAY
    if wd == 4:
        return h < _CLOSE_HOUR
    # Mon-Thu: closed during the daily maintenance window
    return h < _CLOSE_HOUR or h >= _BREAK_END_HOUR


def _exness_open(t: datetime) -> bool:
    """Exness XAUUSD should be printing bars at UTC instant `t`.

    Weekday numbering (UTC): Mon=0 .. Sun=6.
      Sat (5)  — closed all day
      Sun (6)  — first bar opens 22:00, no halt that evening
      Fri (4)  — last bar opens 20:55, then the weekend (halt from 21:00)
      Mon-Thu  — open, minus the daily 21:00-22:00 halt
    """
    utc = _as_utc(t)
    wd, m = utc.weekday(), utc.hour * 60 + utc.minute
    if wd == 5:
        return False
    if wd == 6:
        return m >= _EXNESS_SUNDAY_OPEN_MIN
    if wd == 4:
        return m < _EXNESS_FRIDAY_CLOSE_MIN
    # Mon-Thu: closed during the daily halt
    return m < _EXNESS_BREAK_START_MIN or m >= _EXNESS_BREAK_END_MIN


def market_open(t: datetime, feed: str = "cme") -> bool:
    """True when `feed` should be printing bars at UTC instant `t`.

    feed: "cme" (yfinance GC=F futures, default) or "exness" (bridge XAUUSD
    spot). Unknown values fall back to "cme" — the legacy default.
    """
    if feed == "exness":
        return _exness_open(t)
    return _cme_open(t)


def market_open_now(feed: str = "cme") -> bool:
    """Convenience wrapper: market_open at the current UTC instant."""
    return market_open(datetime.now(timezone.utc), feed=feed)