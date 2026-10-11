#!/usr/bin/env python3
"""Farm watchdog — durable alerting for the brokerless paper farm (ISSUE-097).

The farm's own guards are silent by design: MAX_CANDLE_AGE_SECONDS blocks
ENTRIES on a stale feed but nothing alerts — a multi-hour Yahoo rate-limit
means the league table quietly fills with "0 trades" while the data froze
(ISSUE-097). Likewise a dead container or a regression of a fixed bug
(see ISSUE-099/101) currently has no signal outside a live agent session
(ISSUE-094/084 — session-only monitors die with the session).

This script runs via launchd every 15 min (com.godport.farm-watchdog),
survives reboots, needs no agent session. Checks:

  1. feed age  — newest M5 bar in data/paper-feed/XAUUSD_M5.csv vs
     FARM_WATCHDOG_MAX_FEED_AGE_MIN (default 30, same ceiling as
     MAX_CANDLE_AGE_SECONDS; GC=F delay is ~10-15 min)
     ONLY while the market is open (shared/market_calendar.py, ISSUE-104:
     gold prints no bars on weekends / during the daily break — 300+ stale
     alerts were pure calendar noise before this gate)
  2. containers — paper-fetcher + oracle-engine-farm1 must be Up
  3. regression — 'spread unavailable' / 'ranging_hard_block' must not
     reappear in the engine log since the last watchdog pass

Tooling failures (docker CLI unavailable/slow) are SELF-ERRORs, not farm
problems (ISSUE-104): they go to watchdog.log only — never to the alert
channel, which must stay trustworthy. A self-error also refuses to advance
the regression-scan state, so the missed window is rescanned next pass.

On a farm problem: append one alert file per day to ψ/outbox (Hermes reads
outbox on its own schedule) + a macOS notification (visible on the Mac
mini desktop) + one line to data/farm/watchdog.log. All-clear passes log
one line only — no alert spam when healthy.

Exit code: 0 healthy, 1 farm problems, 2 self-errors only (launchd ignores
it; the log channel is the point).
"""

from __future__ import annotations

import csv
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shared.market_calendar import market_open  # noqa: E402

FEED_M5 = ROOT / "data" / "paper-feed" / "XAUUSD_M5.csv"
OUTBOX = ROOT / "ψ" / "outbox"
LOG = ROOT / "data" / "farm" / "watchdog.log"
STATE_FILE = ROOT / "data" / "farm" / ".watchdog_since"

CONTAINERS = ["paper-fetcher", "oracle-engine-farm1"]
REGRESSION_PATTERNS = ["spread unavailable", "ranging_hard_block"]
DEFAULT_MAX_AGE_MIN = 30.0
SELF_ERROR_PREFIX = "SELF-ERROR: "


def _docker_bin() -> str:
    """Absolute docker path — launchd agents get a minimal PATH without
    /usr/local/bin (first real run ALERTed 'docker: No such file or
    directory'). Resolve at call time so tests pick up whatever is on PATH
    and the launchd fallback hits the OrbStack symlink."""
    return shutil.which("docker") or "/usr/local/bin/docker"


def _newest_bar_age_minutes(feed_csv: Path, now: datetime) -> float | None:
    """Age of the newest M5 bar in minutes. None if file unreadable/empty."""
    try:
        with open(feed_csv, newline="") as fh:
            rows = list(csv.reader(fh))
    except OSError:
        return None
    for row in reversed(rows):
        if not row:
            continue
        try:
            ts = datetime.fromisoformat(row[0].strip())
        except ValueError:
            continue
        if ts.tzinfo is not None:
            ts = ts.replace(tzinfo=None)
        return (now.replace(tzinfo=None) - ts).total_seconds() / 60.0
    return None


def check_feed(max_age_min: float, now: datetime) -> str | None:
    """Return a problem description, or None when the feed is fine.

    ISSUE-104 gate: while the market is closed (weekend + daily break, see
    shared/market_calendar.py) gold prints no bars by design — a "stale" feed
    then is the calendar talking, not the farm. Skip the age check entirely;
    the last market-open pass owns the feed's health.
    """
    if not market_open(now):
        return None
    age = _newest_bar_age_minutes(FEED_M5, now)
    if age is None:
        return "feed unreadable/empty — fetcher may have never written a bar"
    if age > max_age_min:
        return (
            f"feed stale: newest M5 bar {age:.0f} min old "
            f"(limit {max_age_min:.0f} min) — Yahoo rate-limit or fetcher down?"
        )
    return None


def check_containers() -> str | None:
    """All farm containers must report Up in docker ps.

    A docker CLI failure is SELF-ERROR (ISSUE-104): the watchdog's own
    tooling broke — a farm problem can be neither confirmed nor excluded,
    and writing it to the alert channel trains readers to distrust alerts.
    """
    try:
        out = subprocess.run(
            [_docker_bin(), "ps", "--format", "{{.Names}}: {{.Status}}"],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except (subprocess.TimeoutExpired, OSError) as e:
        return f"{SELF_ERROR_PREFIX}docker ps failed: {e}"
    for name in CONTAINERS:
        line = next((l for l in out.splitlines() if l.startswith(name + ":")), None)
        if line is None:
            return f"container {name} NOT RUNNING (docker ps has no entry)"
        if "Up" not in line:
            return f"container {name} not Up: {line}"
    return None


def check_regression(since: datetime) -> str | None:
    """Fixed-bug signatures must not reappear in the engine log since `since`.

    STATE_FILE records the last healthy pass — first run after a gap scans
    from the file's saved time so a regression during downtime is still
    caught. Missing state (first ever run) scans the last 15 minutes only.
    A docker CLI failure is SELF-ERROR (ISSUE-104) — log-only, and the
    caller must not advance the state past the unscanned window.
    """
    try:
        out = subprocess.run(
            [_docker_bin(), "logs", "oracle-engine-farm1", "--since",
             since.strftime("%Y-%m-%dT%H:%M:%S")],
            capture_output=True, text=True, timeout=60,
        ).stdout
    except (subprocess.TimeoutExpired, OSError) as e:
        return f"{SELF_ERROR_PREFIX}docker logs failed: {e}"
    for pattern in REGRESSION_PATTERNS:
        if pattern in out:
            # context line for the alert
            line = next((l for l in out.splitlines() if pattern in l), "")
            return f"REGRESSION: '{pattern}' reappeared in engine log: {line[-200:]}"
    return None


def _notify_macos(message: str) -> None:
    """macOS desktop notification — best effort, never fatal."""
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{message}" with title "Farm Watchdog"'],
            capture_output=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass


def _log(line: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG, "a") as fh:
        fh.write(f"{stamp} {line}\n")


def _write_alert(problems: list[str]) -> Path:
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = OUTBOX / f"farm_alert_{date}.md"
    existing = out.read_text() if out.exists() else ""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    entry = "\n".join(f"- **{stamp}** — {p}" for p in problems)
    header = (
        "# Farm Alert — ต้องตรวจด่วน\n\n"
        "เกิดปัญหาใน paper farm (เขียนอัตโนมัติโดย scripts/farm_watchdog.py)\n\n"
        if not existing.startswith("# Farm Alert")
        else ""
    )
    out.write_text(header + existing.rstrip("\n") + "\n" + entry + "\n")
    return out


def main() -> int:
    now = datetime.now(timezone.utc)
    max_age = float(os.environ.get(
        "FARM_WATCHDOG_MAX_FEED_AGE_MIN", str(DEFAULT_MAX_AGE_MIN)))

    # last pass time for regression scan (state written on every run)
    if STATE_FILE.exists():
        try:
            since = datetime.fromisoformat(STATE_FILE.read_text().strip())
        except ValueError:
            since = now - timedelta(minutes=15)
    else:
        since = now - timedelta(minutes=15)
    if since.tzinfo is not None:
        since = since.replace(tzinfo=None)

    farm_problems: list[str] = []
    self_errors: list[str] = []
    for check in (lambda: check_feed(max_age, now),
                  check_containers,
                  lambda: check_regression(since)):
        problem = check()
        if problem is None:
            continue
        if problem.startswith(SELF_ERROR_PREFIX):
            self_errors.append(problem)
        else:
            farm_problems.append(problem)

    if farm_problems:
        alert_path = _write_alert(farm_problems)
        for p in farm_problems:
            _notify_macos(p[:200])
        if self_errors:
            for p in self_errors:
                _log(f"SELF-ERROR (tooling, not farm): {p[len(SELF_ERROR_PREFIX):]}")
        _log(f"ALERT ({len(farm_problems)}): {' | '.join(farm_problems)} → {alert_path.name}")
        return 1
    if self_errors:
        # ISSUE-104: tooling failures never touch the alert channel; the
        # regression scan never ran, so the state must not advance either.
        for p in self_errors:
            _log(f"SELF-ERROR (tooling, not farm): {p[len(SELF_ERROR_PREFIX):]}")
        return 2
    STATE_FILE.write_text(now.isoformat())
    feed_note = (
        "feed fresh" if market_open(now) else "market closed (feed check skipped)")
    _log(f"OK — {feed_note}, containers up, no regressions")
    return 0


if __name__ == "__main__":
    sys.exit(main())