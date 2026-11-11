#!/usr/bin/env python3
"""Deep tick downloader — XAUUSD tick history from the Exness bridge.

Read-only contract (same as the fetcher / bet engine): the ONLY bridge
methods touched are initialize / symbol_select / symbol_info /
copy_ticks_from(flags=1 INFO). No orders, no positions, no shutdown.
NEVER modify the bridge server to add methods for this — copy_ticks_from
is the only tick API we get.

Transfer uses the str()+ast.literal_eval bulk pattern (live incident
2026-11-11: per-field netref access = 136s per fetch; bulk str = 0.3s).

Storage: one parquet per UTC day under data/paper-feed/ticks/ plus a
progress.json. Resumable: completed days are skipped on restart; the
current (in-flight) day is re-downloaded whole.

Usage:
    python3 scripts/tick_download.py                     # download everything
    python3 scripts/tick_download.py --from 2026-01-01 --to 2026-02-01
    python3 scripts/tick_download.py --spot-check        # verify vs M5 bars
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("tick_download")

TICK_DIR = _ROOT / "data" / "paper-feed" / "ticks"
PROGRESS_FILE = TICK_DIR / "progress.json"

# Earliest tick the broker serves (probe 2026-11-12, see tick_probe.py).
DEEP_START_UTC = datetime(2026, 1, 1, 23, 0, tzinfo=timezone.utc)
# Per-call request cap. MT5's copy_ticks_from(date_from, count) walks
# FORWARD from date_from — an oversized count asks for months of ticks
# in one response and the server render (str of the whole remote list)
# blows past the rpyc request timeout (live failure 2026-11-12: the
# first January window with count=5M tried ~66M ticks → "result
# expired"). 150k covers a 4-hour XAUUSD window in one call; denser
# windows continue from the last tick (fetch_window loop).
CALL_CAP = 150_000
# Download window length (hours) — small chunks keep each rpyc call
# comfortably under the 300s sync timeout (Hermes: ตั้งชำให้เล็กลง).
WINDOW_HOURS = 2
# Retries per window on transport failure before giving up (resumable).
MAX_ATTEMPTS = 3

COLUMNS = ["time_msc", "time", "bid", "ask", "last", "volume_real", "flags"]


class ArchiveBridge:
    """Dedicated read-only bridge client for bulk tick download.

    Same safety posture as the fetcher/TickFeed (initialize / symbol_
    select / symbol_info / copy_ticks_from ONLY — never shutdown, never
    orders), but with a longer rpyc sync timeout (300s vs the live
    engine's 60s) because archive chunks are far bigger than live polls.
    Kept local to this script so the live TickFeed class is untouched.
    """

    def __init__(self, host: str = None, port: int = None,
                 symbol: str = "XAUUSD") -> None:
        from scripts.bet_engine import BRIDGE_HOST, BRIDGE_PORT

        self.host = host or BRIDGE_HOST
        self.port = port or BRIDGE_PORT
        self.symbol = symbol
        self._conn = None
        self._resolved: str | None = None

    def _connect(self) -> bool:
        if self._conn is not None:
            return True
        import rpyc

        try:
            self._conn = rpyc.connect(
                self.host, self.port,
                config={"sync_request_timeout": 300},
            )
            if not self._conn.root.initialize():
                logger.warning("bridge initialize failed")
                self._close()
                return False
            self._resolved = self._resolve_symbol()
            logger.info("bridge connected %s:%s symbol=%s",
                        self.host, self.port, self._resolved)
            return True
        except Exception as exc:
            logger.warning("bridge connect failed: %s", exc)
            self._close()
            return False

    def _close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = None
        self._resolved = None

    def _resolve_symbol(self) -> str:
        from metty.bridge.client import SYMBOL_ALIASES

        for name in SYMBOL_ALIASES.get(self.symbol, [self.symbol]):
            try:
                self._conn.root.symbol_select(name, True)
                if self._conn.root.symbol_info(name):
                    return name
            except Exception:
                continue
        return self.symbol

    def copy_ticks(self, from_epoch: int, count: int) -> list[dict]:
        """Ticks from `from_epoch` (forward, up to `count`), local dicts.

        The str()+ast.literal_eval bulk pattern: per-field netref access
        is a round trip per key (live incident 2026-11-11: 136s → 0.3s).
        """
        import ast

        if not self._connect():
            raise ConnectionError("bridge unavailable")
        ticks = self._conn.root.copy_ticks_from(
            self._resolved, int(from_epoch), int(count), 1,
        )
        if not ticks:
            return []
        return ast.literal_eval(str(ticks))


def _window_start(day: datetime) -> datetime:
    """UTC midnight of the day containing `day`."""
    return day.replace(hour=0, minute=0, second=0, microsecond=0)


def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        return json.loads(PROGRESS_FILE.read_text())
    return {"completed_days": []}


def save_progress(progress: dict) -> None:
    PROGRESS_FILE.write_text(json.dumps(progress, indent=2))


def fetch_window(bridge: ArchiveBridge, start_epoch: int,
                  end_epoch: int) -> list[dict]:
    """All INFO ticks in [start_epoch, end_epoch) — one or more calls.

    copy_ticks_from(symbol, date_from, count, flags) walks forward from
    date_from up to count ticks; when a window is denser than CALL_CAP
    we continue from the last tick's msc until the window is exhausted.
    Transport failures are retried with a fresh connection (the failed
    window is re-requested from its start — resumability is per day).
    """
    out: list[dict] = []
    cursor = int(start_epoch)
    while True:
        batch = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                batch = bridge.copy_ticks(cursor, CALL_CAP)
                break
            except Exception as exc:
                logger.warning("  window fetch failed (attempt %d/%d): %s",
                               attempt, MAX_ATTEMPTS, exc)
                bridge._close()
                if attempt == MAX_ATTEMPTS:
                    raise
                time.sleep(5 * attempt)
        if not batch:
            if cursor == int(start_epoch):
                return []          # empty window (weekend/halt)
            return out             # continuation exhausted
        out.extend(batch)
        last_msc = int(batch[-1]["time_msc"])
        if last_msc >= end_epoch * 1000:
            return out
        if len(batch) < CALL_CAP:
            return out
        cursor = last_msc // 1000 + 1
        logger.info("  window overflow, continuing at msc=%s", last_msc)


def download_day(bridge: ArchiveBridge, day: datetime) -> pd.DataFrame | None:
    """Download one UTC day; returns the day's ticks or None when empty."""
    start = _window_start(day)
    rows: list[dict] = []
    for hour in range(0, 24, WINDOW_HOURS):
        w_start = start + timedelta(hours=hour)
        w_end = w_start + timedelta(hours=WINDOW_HOURS)
        now_epoch = int(time.time())
        if int(w_start.timestamp()) >= now_epoch:
            return None                    # day not fully elapsed yet
        batch = fetch_window(bridge, int(w_start.timestamp()), int(w_end.timestamp()))
        if batch:
            # trim to the exact window (probe may straddle boundaries)
            batch = [t for t in batch
                     if w_start.timestamp() * 1000 <= t["time_msc"] < w_end.timestamp() * 1000]
            rows.extend(batch)
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df = df[COLUMNS].sort_values("time_msc").reset_index(drop=True)
    df = df.drop_duplicates(subset="time_msc", keep="first")
    return df


def day_path(day: datetime) -> Path:
    return TICK_DIR / f"ticks_{day.strftime('%Y-%m-%d')}.parquet"


def run_download(bridge: ArchiveBridge, from_day: datetime, to_day: datetime) -> None:
    TICK_DIR.mkdir(parents=True, exist_ok=True)
    progress = load_progress()
    completed = set(progress["completed_days"])

    day = from_day
    while day <= to_day:
        key = day.strftime("%Y-%m-%d")
        path = day_path(day)
        if key in completed and path.exists():
            day += timedelta(days=1)
            continue
        if int(_window_start(day).timestamp()) + 86_400 > int(time.time()):
            logger.info("day %s not fully elapsed yet — stopping", key)
            break
        t0 = time.time()
        try:
            df = download_day(bridge, day)
        except Exception as exc:
            # One bad day must not kill the run (Hermes 2026-11-12: the
            # January window blew the rpyc timeout and the whole detached
            # download died at day 1). Log, leave the day un-marked, and
            # move on — progress is per-day, a rerun re-attempts it.
            logger.error("%s FAILED (%s) — skipping, rerun will retry", key, exc)
            bridge._close()
            day += timedelta(days=1)
            continue
        if df is None:
            logger.info("%s empty (market closed)", key)
            path.unlink(missing_ok=True)
        else:
            df.to_parquet(path, index=False)
            logger.info("%s %d ticks (%.1fs) -> %s",
                        key, len(df), time.time() - t0, path.name)
        completed.add(key)
        progress["completed_days"] = sorted(completed)
        progress["last_day"] = key
        save_progress(progress)
        day += timedelta(days=1)


def spot_check() -> int:
    """Continuity + price agreement vs the existing M5 bar history.

    For a sample of downloaded days: aggregate ticks to M5 OHLC (mid) and
    compare against data/paper-feed/XAUUSD_M5.csv. Every bar inside the
    day must have ticks (no missing bars) and close prices must agree
    within one spread-width ($0.20/oz) — ticks are mid, bars are bid, so
    small offsets are expected.
    """
    bars = pd.read_csv(
        _ROOT / "data" / "paper-feed" / "XAUUSD_M5.csv", header=None,
        names=["time", "open", "high", "low", "close", "volume"],
    )
    bars["ts"] = pd.to_datetime(bars["time"], utc=True)
    bars = bars.set_index("ts")

    tick_days = sorted(TICK_DIR.glob("ticks_*.parquet"))
    if not tick_days:
        print("no tick days downloaded")
        return 1
    sample = tick_days[:: max(1, len(tick_days) // 8)][:8] + tick_days[-2:]
    worst_close_delta = 0.0
    total_bars_checked = 0
    for path in sorted(set(sample)):
        day = path.stem.replace("ticks_", "")
        df = pd.read_parquet(path)
        df["ts"] = pd.to_datetime(df["time_msc"], unit="ms", utc=True)
        df = df.set_index("ts")
        mid = (df["bid"] + df["ask"]) / 2.0
        ohlc = mid.resample("5min").agg(["first", "max", "min", "last"]).dropna()
        day_bars = bars[bars.index.strftime("%Y-%m-%d") == day]
        if len(day_bars) == 0:
            print(f"{day}: no M5 bars in store — skip")
            continue
        # every bar's 5-minute slot must contain at least one tick
        covered = ohlc.index.strftime("%Y-%m-%d %H:%M")
        bar_keys = day_bars.index.strftime("%Y-%m-%d %H:%M")
        missing = set(bar_keys) - set(covered)
        if missing:
            print(f"{day}: FAIL — {len(missing)} M5 bars have NO ticks: {sorted(missing)[:5]}")
            return 1
        joined = day_bars.join(ohlc, rsuffix="_tick")
        delta = (joined["close"] - joined["last"]).abs().max()
        total_bars_checked += len(joined)
        worst_close_delta = max(worst_close_delta, float(delta))
        print(f"{day}: {len(joined)} bars covered, max |bar_close - tick_mid_close| = ${delta:.2f}")
    print(f"SPOT-CHECK PASS: {total_bars_checked} bars on {len(set(sample))} days, "
          f"worst close delta ${worst_close_delta:.2f} (mid vs bid, spread=$0.20)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="from_day", default=None)
    ap.add_argument("--to", dest="to_day", default=None)
    ap.add_argument("--spot-check", action="store_true")
    args = ap.parse_args()

    if args.spot_check:
        return spot_check()

    from_day = (datetime.strptime(args.from_day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                 if args.from_day else DEEP_START_UTC)
    to_day = (datetime.strptime(args.to_day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
              if args.to_day else datetime.now(timezone.utc))

    bridge = ArchiveBridge()
    if not bridge._connect():
        print("bridge connect failed")
        return 1
    logger.info("downloading %s -> %s (start=%s)",
                from_day.date(), to_day.date(), bridge._resolved)
    run_download(bridge, from_day, to_day)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
