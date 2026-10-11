"""CME gold market calendar — single source of truth for "is the market open".

ISSUE-104 (2026-10-11): the farm watchdog priced feed-stale alerts in wall-clock
time — gold stops printing M5 bars every weekend and during the daily
maintenance break, so 300+ alerts accumulated that were pure calendar noise
(the agent's launchd fired every 15 min while the market was simply closed).
Every consumer that asks "should fresh bars exist right now?" must ask THIS
module, not re-derive the calendar:

  - scripts/farm_watchdog.py      feed-stale gate (this fix)
  - ~/.hermes/scripts/farm_daily_check.py (Hermes imports this module in its
    next round — its local market_open() copy is hard-coded to winter hours)

Calendar (CME gold futures, Chicago local time — DST-shifted in UTC):
  - Trading week: Sunday 17:00 → Friday 16:00 America/Chicago
  - Daily maintenance break: 16:00-17:00 America/Chicago, every day
  - Saturday: closed all day

Why Chicago-local instead of fixed UTC hours: CME publishes its schedule in
Chicago time and shifts with US DST. Verified against the farm's own feed
(2026-10, CDT = UTC-5): last Friday bar stamps 20:55 (close 21:00 UTC), first
Sunday bar stamps 22:00 (open 22:00 UTC), and the weekday break silences bars
21:00-22:00 UTC — one hour earlier than the fixed winter hours (Sun 23:00 →
Fri 22:00 UTC, break 22:00-23:00) that hold after the November DST end.
Hard-coding either fixed UTC schedule would false-alert ~4× every day for
one half of the year; the local-time rule is correct in both.

Not modeled: CME holiday half-days (a half-day prints bars until its early
close, so the feed gate errs toward checking, which is the safe direction).
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

__all__ = ["market_open", "market_open_now"]

_CHICAGO = ZoneInfo("America/Chicago")

# CME gold futures hours, Chicago local (Sun 17:00 open, Fri 16:00 close,
# daily 16:00-17:00 maintenance break, Saturday closed).
_OPEN_HOUR_SUNDAY = 17
_CLOSE_HOUR = 16
_BREAK_END_HOUR = 17


def _as_chicago(t: datetime) -> datetime:
    """Interpret `t` as UTC (naive = UTC) and convert to Chicago local time."""
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(_CHICAGO)


def market_open(t: datetime) -> bool:
    """True when CME gold should be printing bars at UTC instant `t`.

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


def market_open_now() -> bool:
    """Convenience wrapper: market_open at the current UTC instant."""
    return market_open(datetime.now(timezone.utc))