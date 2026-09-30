#!/usr/bin/env python3
"""Brokerless candle feed fetcher — paper trade farm (Mac mini).

Polls Yahoo Finance (GC=F gold futures) and maintains three accumulating
CSVs in premium format (no header: date,open,high,low,close,volume):

    XAUUSD_M5.csv   — refreshed every POLL_INTERVAL (default 300s)
    XAUUSD_H1.csv   — refreshed every HTF_REFRESH_INTERVAL (default 3600s)
    XAUUSD_D1.csv   — refreshed every HTF_REFRESH_INTERVAL

LiveTrader._fetch_candles_csv reads these directly (see
tests/test_csv_feed_htf_causal.py) — H4 resamples from H1 inside the
trader. The M5 file is the engine's working window; H1/D1 give the EMA
trend context the bridge used to provide.

Design constraints:
- ONLY CLOSED bars are written (age >= timeframe), so paper decisions are
  deterministic — never act on a forming bar.
- Atomic writes (tmp + rename): an engine never reads a half-written CSV.
- A failed fetch keeps the previous file (stale data beats no data).
- Merge-dedup by timestamp: safe to restart, safe to re-seed.

Run:  python3 scripts/paper_feed_fetcher.py --feed-dir ./data/paper-feed
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

# Repo-root bootstrap when invoked as `python3 scripts/paper_feed_fetcher.py`
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from broky.data.yfinance_loader import fetch_xauusd  # noqa: E402

logger = logging.getLogger("paper_feed_fetcher")

# Bar-open time -> a bar is closed once this many seconds have passed.
BAR_CLOSE_SECONDS = {"M5": 300, "H1": 3600, "D1": 86400}
CSV_NAMES = {"M5": "XAUUSD_M5.csv", "H1": "XAUUSD_H1.csv", "D1": "XAUUSD_D1.csv"}
# yfinance seed window per timeframe (M5 intraday capped at 60d by Yahoo).
SEED_PERIOD = {"M5": "60d", "H1": "1y", "D1": "2y"}
# Incremental poll window — overlaps the seed to survive clock skew/gaps.
DELTA_PERIOD = {"M5": "1d", "H1": "5d", "D1": "10d"}

FETCH_ATTEMPTS = 3
FETCH_RETRY_SLEEP = 5


def _fetch_with_retry(tf: str, period: str) -> pd.DataFrame | None:
    """Fetch a timeframe, retrying transient failures. None when all fail."""
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            df = fetch_xauusd(period=period, interval={"M5": "5m", "H1": "1h", "D1": "1d"}[tf])
            if not df.empty:
                return df
            logger.warning("%s: empty response (attempt %d/%d)", tf, attempt, FETCH_ATTEMPTS)
        except Exception as e:
            logger.warning("%s: fetch failed (attempt %d/%d): %s", tf, attempt, FETCH_ATTEMPTS, e)
        time.sleep(FETCH_RETRY_SLEEP)
    return None


def _drop_unclosed(df: pd.DataFrame, tf: str, now: datetime) -> pd.DataFrame:
    """Keep only bars whose window has closed (deterministic paper decisions)."""
    if df.empty:
        return df
    min_age = BAR_CLOSE_SECONDS[tf]
    ages = (now - df.index).total_seconds()
    closed = df[ages >= min_age]
    dropped = len(df) - len(closed)
    if dropped:
        logger.info("%s: dropped %d unclosed bar(s)", tf, dropped)
    return closed


def _load_existing(feed_dir: Path, tf: str) -> pd.DataFrame:
    path = feed_dir / CSV_NAMES[tf]
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(
        path, header=None,
        names=["date", "open", "high", "low", "close", "volume"],
        parse_dates=["date"],
    ).set_index("date")
    return df.dropna().sort_index()


def _write_atomic(feed_dir: Path, tf: str, df: pd.DataFrame) -> None:
    tmp = feed_dir / f"{CSV_NAMES[tf]}.tmp"
    final = feed_dir / CSV_NAMES[tf]
    out = df.copy()
    out.index.name = "date"
    out.to_csv(tmp, header=False, float_format="%.3f")
    tmp.replace(final)


def _now_utc() -> datetime:
    """Current UTC time (naive) — injectable for tests."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def update_timeframe(feed_dir: Path, tf: str, seed: bool = False) -> bool:
    """Fetch + merge one timeframe into its CSV. Returns True when the file changed."""
    now = _now_utc()
    fetched = _fetch_with_retry(tf, SEED_PERIOD[tf] if seed else DELTA_PERIOD[tf])
    if fetched is None or fetched.empty:
        logger.warning("%s: fetch failed — keeping previous CSV", tf)
        return False

    fetched = _drop_unclosed(fetched, tf, now)
    if fetched.empty:
        logger.info("%s: no closed bars yet — keeping previous CSV", tf)
        return False

    existing = _load_existing(feed_dir, tf)
    merged = pd.concat([existing, fetched]) if not existing.empty else fetched
    merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    if not existing.empty and len(merged) == len(existing) and merged.index.equals(existing.index):
        logger.info("%s: no new bars (%d total)", tf, len(merged))
        return False

    _write_atomic(feed_dir, tf, merged)
    logger.info("%s: wrote %d bars (last %s)", tf, len(merged), merged.index[-1])
    return True


def run(feed_dir: Path, interval: int, htf_interval: int, once: bool = False) -> None:
    feed_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Paper feed fetcher starting — feed_dir=%s", feed_dir)

    # Seed all timeframes on boot (idempotent — merge-dedup keeps existing rows)
    for tf in ("M5", "H1", "D1"):
        update_timeframe(feed_dir, tf, seed=True)

    if once:
        return

    last_htf = time.monotonic()
    while True:
        time.sleep(max(1, interval))
        try:
            update_timeframe(feed_dir, "M5", seed=False)
            if time.monotonic() - last_htf >= htf_interval:
                update_timeframe(feed_dir, "H1", seed=False)
                update_timeframe(feed_dir, "D1", seed=False)
                last_htf = time.monotonic()
        except Exception:
            logger.exception("Feed cycle crashed — continuing with next cycle")


def main() -> None:
    parser = argparse.ArgumentParser(description="Brokerless paper-farm candle feed fetcher")
    parser.add_argument("--feed-dir", default="data/paper-feed",
                        help="Directory for the CSV feed (default: data/paper-feed)")
    parser.add_argument("--interval", type=int, default=300,
                        help="M5 poll interval in seconds (default: 300)")
    parser.add_argument("--htf-interval", type=int, default=3600,
                        help="H1/D1 refresh interval in seconds (default: 3600)")
    parser.add_argument("--once", action="store_true",
                        help="Seed/refresh once and exit (testing)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run(Path(args.feed_dir), args.interval, args.htf_interval, once=args.once)


if __name__ == "__main__":
    main()