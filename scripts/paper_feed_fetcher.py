#!/usr/bin/env python3
"""Brokerless candle feed fetcher — paper trade farm (Mac mini).

Two data sources, selected via FEED_SOURCE (default yfinance until the farm
is switched, 2026-10-30 — then bridge):

    yfinance — polls Yahoo Finance GC=F gold futures (delayed 5-15 min).
    bridge   — polls the VPS MT5 bridge (mt5b) over Tailscale for Exness
               XAUUSD real-time — the SAME prices the demo B/C/D engines
               trade on, so paper↔demo signal timing lines up (คุณหมอ
               30/10/26: "แก้ feed timing ใช้ realtime อันเดียว ทั้ง paper
               และ demo").

Maintains three accumulating CSVs in premium format (no header:
date,open,high,low,close,volume):

    XAUUSD_M5.csv   — refreshed every POLL_INTERVAL (60s in bridge mode)
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
- Merge-dedup by timestamp: safe to restart, safe to re-seed — within one
  feed era. Across eras the history is REPLACED, never merged: GC=F
  futures and XAUUSD spot differ by dollars of basis, and stitching them
  would plant a fake gap that corrupts Boll/ATR/ADX across the whole farm
  (era marker: XAUUSD_<TF>.csv.era, missing = legacy yfinance).
- Bridge mode NEVER falls back to yfinance (same anti-mix reason): retries
  with backoff, and the engines' stale guard (MAX_CANDLE_AGE_SECONDS,
  default 1800s) blocks trading while the feed is frozen.

Bridge safety contract: the fetcher is a READ-ONLY bridge client — the only
server methods ever called are initialize / symbol_select / symbol_info /
copy_rates_from_pos_json. No order/position methods: mt5b also serves the
live demo B engine.

Run:  python3 scripts/paper_feed_fetcher.py --feed-dir ./data/paper-feed
"""

from __future__ import annotations

import argparse
import json
import logging
import os
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
from metty.bridge.client import MT5_TIMEFRAMES, SYMBOL_ALIASES  # noqa: E402

logger = logging.getLogger("paper_feed_fetcher")

# ─── Feed source selection ────────────────────────────────────────────────
FEED_SOURCE = os.environ.get("FEED_SOURCE", "yfinance").strip().lower()
SOURCES = ("yfinance", "bridge")

# Bridge endpoint (VPS mt5q — the farm's dedicated PRICE terminal:
# host 5012 → container 8001, reached over the tailnet — MagicDNS name
# resolves inside OrbStack containers, verified 2026-10-30). Exness XAUUSD,
# the same symbol the demo engines trade. mt5q logged into demo B env;
# mt5b stays exclusive to the demo B engine (race fix 2026-10-30).
BRIDGE_HOST = os.environ.get("PAPER_BRIDGE_HOST", "vpsdeluna")
BRIDGE_PORT = int(os.environ.get("PAPER_BRIDGE_PORT", "5012"))

# Bridge backfill depth in bars — matches/exceeds the yfinance seed windows
# (M5 60d / H1 1y / D1 2y). Verified against the live bridge 2026-10-30:
# 15000 M5 bars = 74.4d · 6000 H1 = 370.8d · 800 D1 = 935d.
BRIDGE_SEED_COUNT = {"M5": 15000, "H1": 6000, "D1": 800}
# Incremental poll window (bars) — overlaps the seed so fetcher downtime
# between restarts heals without a full re-seed.
BRIDGE_DELTA_COUNT = {"M5": 300, "H1": 72, "D1": 14}

# Bar-open time -> a bar is closed once this many seconds have passed.
BAR_CLOSE_SECONDS = {"M5": 300, "H1": 3600, "D1": 86400}
CSV_NAMES = {"M5": "XAUUSD_M5.csv", "H1": "XAUUSD_H1.csv", "D1": "XAUUSD_D1.csv"}
# yfinance seed window per timeframe (M5 intraday capped at 60d by Yahoo).
SEED_PERIOD = {"M5": "60d", "H1": "1y", "D1": "2y"}
# Incremental poll window — overlaps the seed to survive clock skew/gaps.
# M5 window must span the weekend closure (~2.3d): with "1d" Yahoo returned
# empty from ~24h after Friday's close until Sunday reopen (2026-10-03 incident
# — farm blind, watchdog alerting "feed stale" all weekend).
DELTA_PERIOD = {"M5": "5d", "H1": "5d", "D1": "10d"}

FETCH_ATTEMPTS = 3
FETCH_RETRY_SLEEP = 5


def _fetch_with_retry(tf: str, period: str) -> pd.DataFrame | None:
    """Fetch a timeframe from yfinance, retrying transient failures."""
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


# ─── Bridge client (READ-ONLY — see module docstring safety contract) ─────

class BridgeFeed:
    """Persistent read-only connection to one VPS MT5 bridge terminal.

    Deliberately NOT MT5Bridge.fetch_candles_sync: that helper connects and
    calls root.shutdown() on every call, and mt5b also serves the live demo
    B engine — a per-poll mt5.shutdown() could kill the engine's in-flight
    read. This class connects once, resolves the symbol once, and only
    closes the rpyc socket on failure (socket close only, never the
    server-side shutdown — the other client owns the terminal lifecycle).

    Only read methods are touched: initialize, symbol_select, symbol_info,
    copy_rates_from_pos_json (see module docstring — no order/position calls).
    """

    def __init__(self, host: str = BRIDGE_HOST, port: int = BRIDGE_PORT,
                 symbol: str = "XAUUSD"):
        self.host = host
        self.port = port
        self._symbol = symbol
        self._resolved: str | None = None
        self._conn = None

    def _close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = None
        self._resolved = None

    def _connect(self) -> bool:
        if self._conn is not None:
            return True
        import rpyc  # deferred: tests mock this class entirely

        try:
            self._conn = rpyc.connect(
                self.host, self.port, config={"sync_request_timeout": 60},
            )
            if not self._conn.root.initialize():
                logger.warning("bridge initialize failed at %s:%s", self.host, self.port)
                self._close()
                return False
            self._resolved = self._resolve_symbol()
            logger.info("bridge connected %s:%s symbol=%s", self.host, self.port, self._resolved)
            return True
        except Exception as e:
            logger.warning("bridge connect %s:%s failed: %s", self.host, self.port, e)
            self._close()
            return False

    def _resolve_symbol(self) -> str:
        """Pick the first broker symbol variant that exists (Exness: XAUUSDm)."""
        for name in SYMBOL_ALIASES.get(self._symbol, [self._symbol]):
            try:
                self._conn.root.symbol_select(name, True)
                info = self._conn.root.symbol_info(name)
                if info:
                    return name
            except Exception:
                continue
        logger.warning("no symbol variant found, using %s", self._symbol)
        return self._symbol

    def _copy(self, tf_id: int, count: int) -> pd.DataFrame | None:
        """One read: rates → frame. None on no data (payload missing, EMPTY
        JSON "[]", or empty frame — a shut-down module returns "[]", a
        truthy string, so emptiness must be judged AFTER parsing)."""
        payload = self._conn.root.copy_rates_from_pos_json(
            self._resolved, tf_id, 0, count,
        )
        if not payload:
            return None
        df = pd.DataFrame(json.loads(payload))
        if df.empty:
            return None
        df["time"] = pd.to_datetime(df["time"], unit="s")
        if "tick_volume" in df.columns:
            df = df.rename(columns={"tick_volume": "volume"})
        df = df.set_index("time").sort_index()
        cols = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
        return df[cols]

    def fetch(self, tf: str, count: int) -> pd.DataFrame | None:
        """Bars for `tf` (newest last), or None on any failure. Read-only."""
        try:
            if not self._connect():
                return None
            tf_id = MT5_TIMEFRAMES.get(tf, MT5_TIMEFRAMES["M5"])
            df = self._copy(tf_id, count)
            if df is None:
                # SHARED TERMINAL (live incident 2026-10-30 10:02 UTC): the
                # demo engine's per-cycle root.shutdown() kills the process-
                # global MT5 handle while our rpyc connection stays alive —
                # every copy then returns empty data and never recovers.
                # Heal with one idempotent initialize() + retry. We never
                # call shutdown ourselves (that would kill the demo
                # engine's in-flight read instead).
                logger.warning("%s: bridge returned no data — re-initializing "
                               "shared terminal and retrying", tf)
                if not self._conn.root.initialize():
                    self._close()
                    return None
                return self._copy(tf_id, count)
            return df
        except Exception as e:
            logger.warning("bridge fetch %s failed: %s — dropping connection", tf, e)
            self._close()
            return None


_bridge_feed: BridgeFeed | None = None


def _bridge_fetch(tf: str, count: int) -> pd.DataFrame | None:
    """Singleton BridgeFeed.fetch — injectable seam for tests."""
    global _bridge_feed
    if _bridge_feed is None:
        _bridge_feed = BridgeFeed()
    return _bridge_feed.fetch(tf, count)


def _fetch_bridge_with_retry(tf: str, count: int) -> pd.DataFrame | None:
    """Bridge fetch with backoff. None when all attempts fail (NO yfinance
    fallback — mixing the two feeds would corrupt every indicator)."""
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        df = _bridge_fetch(tf, count)
        if df is not None and not df.empty:
            return df
        logger.warning("%s: bridge fetch failed (attempt %d/%d) — no yfinance fallback",
                       tf, attempt, FETCH_ATTEMPTS)
        if attempt < FETCH_ATTEMPTS:
            time.sleep(FETCH_RETRY_SLEEP * attempt)  # backoff 5s, 10s
    return None


# ─── Era marker: which feed wrote the current history ──────────────────────

def _era_path(feed_dir: Path, tf: str) -> Path:
    return feed_dir / f"{CSV_NAMES[tf]}.era"


def _read_era(feed_dir: Path, tf: str) -> str:
    """Feed era of the CSV on disk. Missing marker = legacy yfinance history."""
    try:
        era = _era_path(feed_dir, tf).read_text().strip()
    except OSError:
        return "yfinance"
    return era if era in SOURCES else "yfinance"


def _write_era(feed_dir: Path, tf: str, source: str) -> None:
    _era_path(feed_dir, tf).write_text(source + "\n")


# ─── CSV plumbing (shared by both sources) ─────────────────────────────────

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


def _commit(feed_dir: Path, tf: str, fetched: pd.DataFrame, source: str,
            seed: bool) -> bool:
    """Era-aware write: merge within an era, full-replace across eras.

    An era switch must only ever write a FULL history (seed), and only on a
    successful fetch — otherwise the farm is left on the previous feed until
    the next retry (the engines' stale guard blocks trading meanwhile).
    """
    era = _read_era(feed_dir, tf)
    if era != source:
        if not seed:
            logger.error("%s: era mismatch (%s file, %s source) outside seed — "
                         "waiting for full-history fetch", tf, era, source)
            return False
        _write_atomic(feed_dir, tf, fetched)
        _write_era(feed_dir, tf, source)
        logger.info("%s: era switch %s→%s — history REPLACED (%d bars, %s → %s)",
                    tf, era, source, len(fetched), fetched.index[0], fetched.index[-1])
        return True

    existing = _load_existing(feed_dir, tf)
    merged = pd.concat([existing, fetched]) if not existing.empty else fetched
    merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    if not existing.empty and len(merged) == len(existing) and merged.index.equals(existing.index):
        logger.info("%s: no new bars (%d total)", tf, len(merged))
        return False

    _write_atomic(feed_dir, tf, merged)
    logger.info("%s: wrote %d bars (last %s)", tf, len(merged), merged.index[-1])
    return True


def update_timeframe(feed_dir: Path, tf: str, seed: bool = False,
                     source: str | None = None) -> bool:
    """Fetch + commit one timeframe into its CSV. True when the file changed.

    `source` defaults to FEED_SOURCE (env) — explicit so tests can exercise
    both modes in one process.
    """
    source = (source or FEED_SOURCE).strip().lower()
    if source not in SOURCES:
        logger.error("unknown FEED_SOURCE %r — keeping previous CSV", source)
        return False
    now = _now_utc()

    if source == "bridge":
        count = BRIDGE_SEED_COUNT[tf] if seed else BRIDGE_DELTA_COUNT[tf]
        fetched = _fetch_bridge_with_retry(tf, count)
    else:
        fetched = _fetch_with_retry(tf, SEED_PERIOD[tf] if seed else DELTA_PERIOD[tf])

    if fetched is None or fetched.empty:
        logger.warning("%s: fetch failed — keeping previous CSV", tf)
        return False

    fetched = _drop_unclosed(fetched, tf, now)
    if fetched.empty:
        logger.info("%s: no closed bars yet — keeping previous CSV", tf)
        return False

    return _commit(feed_dir, tf, fetched, source, seed)


def _pending_era_switch(feed_dir: Path, source: str) -> list[str]:
    """Timeframes whose on-disk era differs from the active source.

    Recovers automatically after a failed switch (bridge down at boot left
    the yfinance history in place) and after a lost marker file: the run
    loop re-seeds those timeframes before any incremental merge can happen.
    """
    if source != "bridge":
        return []
    return [tf for tf in ("M5", "H1", "D1") if _read_era(feed_dir, tf) != source]


def run(feed_dir: Path, interval: int, htf_interval: int, once: bool = False,
        source: str | None = None) -> None:
    source = (source or FEED_SOURCE).strip().lower()
    feed_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Paper feed fetcher starting — feed_dir=%s source=%s%s",
                feed_dir, source,
                f" bridge={BRIDGE_HOST}:{BRIDGE_PORT}" if source == "bridge" else "")

    # Seed all timeframes on boot (era-aware: a source change replaces history)
    for tf in ("M5", "H1", "D1"):
        update_timeframe(feed_dir, tf, seed=True, source=source)

    if once:
        return

    last_htf = time.monotonic()
    while True:
        time.sleep(max(1, interval))
        try:
            pending = _pending_era_switch(feed_dir, source)
            if pending:
                # Era switch not finished (fetch failed at boot) — retry the
                # full-history fetch for those timeframes only. No incremental
                # merge happens across eras, so nothing can mix meanwhile.
                for tf in pending:
                    update_timeframe(feed_dir, tf, seed=True, source=source)
                continue
            update_timeframe(feed_dir, "M5", seed=False, source=source)
            if time.monotonic() - last_htf >= htf_interval:
                update_timeframe(feed_dir, "H1", seed=False, source=source)
                update_timeframe(feed_dir, "D1", seed=False, source=source)
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
    parser.add_argument("--source", choices=SOURCES, default=FEED_SOURCE,
                        help="Data source (default: FEED_SOURCE env or yfinance)")
    args = parser.parse_args()

    if args.source == "bridge" and args.interval > 60:
        # Realtime bridge — 60s poll keeps bar age << engines' 6-min window.
        logger.info("bridge mode: interval %ds → 60s (realtime feed)", args.interval)
        args.interval = 60

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run(Path(args.feed_dir), args.interval, args.htf_interval, once=args.once,
        source=args.source)


if __name__ == "__main__":
    main()