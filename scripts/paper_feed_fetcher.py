#!/usr/bin/env python3
"""Brokerless candle feed fetcher — paper trade farm (Mac mini).

Three data sources, selected via FEED_SOURCE (default yfinance until the
farm is switched, 2026-10-30 — then bridge; 2026-11-18 — then ctrader):

    yfinance — polls Yahoo Finance GC=F gold futures (delayed 5-15 min).
    bridge   — polls the VPS MT5 bridge (mt5b) over Tailscale for Exness
               XAUUSD real-time — the SAME prices the demo B/C/D engines
               trade on, so paper↔demo signal timing lines up (คุณหมอ
               30/10/26: "แก้ feed timing ใช้ realtime อันเดียว ทั้ง paper
               และ demo"). Exness era ended 17-18/11/26: every MT5 bridge on
               the VPS is gone — kept for history/era logic only.
    ctrader  — polls IC Markets XAUUSD M5/H1/D1 trendbars over the cTrader
               Open API (read-only market data — see CTraderFeed).

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
- Bridge/ctrader mode NEVER falls back to yfinance (same anti-mix reason):
  retries with backoff, and the engines' stale guard
  (MAX_CANDLE_AGE_SECONDS, default 1800s) blocks trading while the feed
  is frozen.
- Dir-level era stamp feed-era.json ({era, since, venue}): once the feed
  dir is pinned to one venue era, ANY other source is refused — merge AND
  seed, with the reason in the log. Switching era on purpose requires
  deleting the marker (same anti-mix disease as yfinance→Exness, now
  structural for the IC Markets era).

Bridge safety contract: the fetcher is a READ-ONLY bridge client — the only
server methods ever called are initialize / symbol_select / symbol_info /
copy_rates_from_pos_json. No order/position methods: mt5b also serves the
live demo B engine.

cTrader safety contract: CTraderFeed touches ONLY read-only market data
(connect / disconnect / get_candles trendbars). No order method is ever
imported or called — enforced by tests/test_paper_feed_ctrader_causal.py
(AST scan of this file + exact allowed-set on the wire payload types).

Run:  python3 scripts/paper_feed_fetcher.py --feed-dir ./data/paper-feed
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

# Repo-root bootstrap when invoked as `python3 scripts/paper_feed_fetcher.py`
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from broky.data.yfinance_loader import fetch_xauusd  # noqa: E402
from metty.bridge.client import MT5_TIMEFRAMES, SYMBOL_ALIASES  # noqa: E402

from ctrader import CTraderApiError, CTraderBridge, CTraderConfig  # noqa: E402

logger = logging.getLogger("paper_feed_fetcher")

# ─── Feed source selection ────────────────────────────────────────────────
FEED_SOURCE = os.environ.get("FEED_SOURCE", "yfinance").strip().lower()
SOURCES = ("yfinance", "bridge", "ctrader")

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

# cTrader (IC Markets) uses the SAME depths as the bridge era —
# CTraderBridge chunks the trendbar windows itself (≤ 1 week/request,
# historical rate limit 5 req/s — see ctrader/README.md), so a "count" is
# just a depth request.

# CTRADER_* env keys — the literal names ARE the wiring contract: the farm
# compose writes exactly these into paper-fetcher, and the env-coverage
# guard greps THIS file for readers (its production corpus is
# metty/broky/shared/scripts — ctrader/ is deliberately outside it, so a
# key only readable via CTraderConfig.from_env() inside ctrader/config.py
# would be a silent no-op from the farm's config point of view: the
# ISSUE-100 class). Never read these from anywhere else.
_CTRADER_ENV_KEYS = (
    "CTRADER_CLIENT_ID",
    "CTRADER_CLIENT_SECRET",
    "CTRADER_ACCESS_TOKEN",
    "CTRADER_ACCOUNT_ID",
    "CTRADER_HOST_TYPE",
    "CTRADER_NAME",
    "CTRADER_SYMBOL",
)


def _ctrader_config_from_env() -> CTraderConfig:
    """Env-only credentials → CTraderConfig (env-only pattern, like MT5)."""
    return CTraderConfig.from_env(
        {key: os.environ.get(key, "") for key in _CTRADER_ENV_KEYS}
    )

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


# ─── cTrader feed (READ-ONLY market data — IC Markets XAUUSD) ─────────────

@dataclass
class _ReadOnlyCTraderView:
    """Read-only façade over one CTraderBridge — order methods unreachable.

    The wrapped bridge is private; the ONLY exposed surface is the
    read-only allowlist (connect / disconnect / get_candles /
    last_connect_error). Contract tests pin the class's method surface AND
    every wrapped member it touches — adding an order path here fails the
    suite, not the portfolio.
    """

    _bridge: CTraderBridge
    last_connect_error: Exception | None = None

    async def connect(self) -> bool:
        ok = await self._bridge.connect()
        self.last_connect_error = self._bridge.last_connect_error
        return ok

    async def disconnect(self) -> None:
        await self._bridge.disconnect()

    async def get_candles(self, symbol: str, timeframe: str,
                          count: int) -> pd.DataFrame:
        return await self._bridge.get_candles(symbol, timeframe, count)


# Auth-class failures that mean "this token/app is dead" — retrying can
# never succeed, so the feed stops hammering the endpoint until restart.
_AUTH_DEAD_CODES = {
    "CH_ACCESS_TOKEN_EXPIRED",
    "CH_ACCESS_TOKEN_INVALID",
    "CH_APP_AUTH_FAILURE",
    "CH_CLIENT_AUTH_FAILURE",
}

# ONE event loop for every ctrader fetch in the process. Transports (and
# their internal queues) bind to the loop they first run on — a fresh
# loop per fetch would orphan every shared transport object (mock tests
# surface this as "Queue is bound to a different event loop"). The fetcher
# is single-threaded, so the loop is driven synchronously per poll.
_ctrader_loop: asyncio.AbstractEventLoop | None = None


def _ctrader_run(coro) -> object:
    """Drive one fetch coroutine on the process-wide cTrader loop."""
    global _ctrader_loop
    if _ctrader_loop is None or _ctrader_loop.is_closed():
        _ctrader_loop = asyncio.new_event_loop()
    return _ctrader_loop.run_until_complete(coro)


class CTraderFeed:
    """Read-only cTrader market-data feed for the paper farm.

    One connection per fetch: connect (2-layer auth) → get_candles
    (chunked trendbars, ≤ 1 week/request) → disconnect. Stateless between
    polls — no heartbeat to keep alive and no handle for anything else to
    kill (the Exness shared-terminal disease cannot recur: the connection
    is ours alone and short-lived).

    READ-ONLY CONTRACT: the bridge is only ever constructed inside
    _ReadOnlyCTraderView — connect / disconnect / get_candles are the only
    reachable methods. tests/test_paper_feed_ctrader_causal.py pins this
    three ways: no order method name in this module's text, the view's
    allowlisted surface, and the wire carrying read-only payload types only.

    Token problems (CH_ACCESS_TOKEN_EXPIRED etc.) fail loud with the API
    error code + remediation in the log and set _auth_dead — further
    fetches short-circuit with ZERO wire traffic until restart. Never a
    yfinance fallback.

    An empty or failed poll re-serves the last genuinely-fetched raw
    frame for that timeframe: bars that closed since get merged (trendbar
    OHLC never changes after close, and _commit dedupes), so a forming
    bar fetched at poll N-1 is not lost. A true outage freezes the feed —
    the engines' stale guard blocks trading meanwhile.
    """

    def __init__(self, config: CTraderConfig | None = None,
                 transport=None) -> None:
        self._config = config or _ctrader_config_from_env()
        self._transport = transport  # injectable for tests (FakeTransport)
        self._auth_dead = False
        self._last_frame: dict[str, pd.DataFrame] = {}

    def close(self) -> None:
        """Drop cached frames — stateless, so nothing to tear down."""
        self._last_frame.clear()

    def fetch(self, tf: str, count: int) -> pd.DataFrame | None:
        """Bars for `tf` (newest last), or None on any failure. Read-only."""
        if self._auth_dead:
            logger.warning("ctrader auth is dead (expired/invalid token or app) — "
                           "not retrying; refresh the token and restart paper-fetcher")
            return None
        if not self._config.is_configured:
            logger.error(
                "ctrader feed not configured — set %s in the host .env "
                "(env-only pattern). No fallback will happen; the feed stays "
                "frozen until the engines' stale guard blocks trading.",
                " / ".join(_CTRADER_ENV_KEYS[:4]),
            )
            return None
        try:
            df = _ctrader_run(self._fetch(tf, count))
        except Exception as e:  # noqa: BLE001 — a failed poll keeps the CSV
            logger.warning("ctrader fetch %s failed: %s — no yfinance fallback", tf, e)
            df = None
        if df is not None and not df.empty:
            self._last_frame[tf] = df
            return df
        cached = self._last_frame.get(tf)
        if cached is None:
            return None
        logger.info("%s: ctrader poll came back empty/failed — re-serving the last "
                   "genuine frame (tail %s); bars closed since are merged, deduped",
                   tf, cached.index[-1])
        return cached

    async def _fetch(self, tf: str, count: int) -> pd.DataFrame | None:
        # One reconnect attempt per poll: a transient connect failure (a
        # server hang-up between polls, a TLS handshake flake) heals with a
        # brand-new bridge; a dead token does NOT get retried (see below).
        for attempt in (1, 2):
            bridge = _ReadOnlyCTraderView(CTraderBridge(self._config, transport=self._transport))
            try:
                if not await bridge.connect():
                    reason = bridge.last_connect_error
                    if isinstance(reason, CTraderApiError) \
                            and reason.code in _AUTH_DEAD_CODES:
                        self._auth_dead = True
                        logger.error(
                            "ctrader auth failed on %s: %s — refresh the token at "
                            "https://openapi.ctrader.com/apps/token "
                            "(grant_type=refresh_token) and restart paper-fetcher; "
                            "NOT retrying until restart", tf, reason,
                        )
                        return None
                    logger.warning("ctrader connect failed on %s (attempt %d/2): "
                                   "%s — no yfinance fallback", tf, attempt, reason)
                    continue
                df = await bridge.get_candles(self._config.symbol, tf, count)
                if df is None or df.empty:
                    logger.warning("%s: ctrader returned no bars — no yfinance fallback", tf)
                    return None
                return df
            finally:
                await bridge.disconnect()
        return None


_ctrader_feed: CTraderFeed | None = None


def _ctrader_fetch(tf: str, count: int) -> pd.DataFrame | None:
    """Singleton CTraderFeed.fetch — injectable seam for tests."""
    global _ctrader_feed
    if _ctrader_feed is None:
        _ctrader_feed = CTraderFeed()
    return _ctrader_feed.fetch(tf, count)


def _fetch_ctrader_with_retry(tf: str, count: int) -> pd.DataFrame | None:
    """CTrader fetch with backoff. None when all attempts fail (NO yfinance
    fallback — same anti-mix reason as bridge mode)."""
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        df = _ctrader_fetch(tf, count)
        if df is not None and not df.empty:
            return df
        logger.warning("%s: ctrader fetch failed (attempt %d/%d) — no yfinance fallback",
                       tf, attempt, FETCH_ATTEMPTS)
        if attempt < FETCH_ATTEMPTS:
            time.sleep(FETCH_RETRY_SLEEP * attempt)  # backoff 5s, 10s
    return None


# ─── Era marker: which feed wrote the current history ──────────────────────

def _era_path(feed_dir: Path, tf: str) -> Path:
    return feed_dir / f"{CSV_NAMES[tf]}.era"


def _read_era(feed_dir: Path, tf: str) -> str:
    """Feed era of the CSV on disk. Missing marker = legacy yfinance history.

    When the per-TF marker is lost, the dir-level feed-era.json keeps the
    venue truth (IC-era history must never be mistaken for yfinance-era —
    a lost marker file must not open the door for a silent mix). Per-TF
    markers stay authoritative when present: a seed can complete for one
    timeframe and fail for another.
    """
    try:
        era = _era_path(feed_dir, tf).read_text().strip()
        if era in SOURCES:
            return era
    except OSError:
        pass
    marker = _read_feed_era(feed_dir)
    if marker is not None:
        source = _ERA_TO_SOURCE.get(str(marker.get("era")))
        if source is not None:
            return source
    return "yfinance"


def _write_era(feed_dir: Path, tf: str, source: str) -> None:
    _era_path(feed_dir, tf).write_text(source + "\n")


# ─── Dir-level era stamp: feed-era.json ({era, since, venue}) ──────────────
# Pins the WHOLE feed dir to one venue era. A marker pointing at another
# era refuses writes from any other source (merge AND seed) — the structural
# fix for the yfinance→Exness silent mix; written by the ctrader path.

FEED_ERA_JSON = "feed-era.json"
ERA_JSON_META = {
    "ctrader": {"era": "ic-ctrader", "venue": "IC Markets"},
    "bridge": {"era": "exness-bridge", "venue": "Exness (MT5 bridge)"},
    "yfinance": {"era": "yfinance", "venue": "Yahoo Finance (GC=F)"},
}
_ERA_TO_SOURCE = {meta["era"]: src for src, meta in ERA_JSON_META.items()}


def _read_feed_era(feed_dir: Path) -> dict | None:
    """Parsed feed-era.json, or None when missing/malformed (loud on junk)."""
    try:
        data = json.loads((feed_dir / FEED_ERA_JSON).read_text())
    except OSError:
        return None
    except ValueError as e:
        logger.warning("%s is malformed (%s) — treating as absent", FEED_ERA_JSON, e)
        return None
    if not isinstance(data, dict) or not data.get("era"):
        logger.warning("%s has no era field — treating as absent", FEED_ERA_JSON)
        return None
    return data


def _write_feed_era(feed_dir: Path) -> None:
    """Idempotent IC-era stamp — writes once per era, never per poll.

    `since` = when the IC era BEGAN. Re-stamping later timeframes (or later
    polls) must never move it, so an already-correct marker is left alone.
    """
    marker = _read_feed_era(feed_dir)
    if marker is not None and marker.get("era") == ERA_JSON_META["ctrader"]["era"]:
        return  # already stamped (and `since` preserved by not rewriting)
    now = _now_utc()
    payload = {
        "era": ERA_JSON_META["ctrader"]["era"],
        # naive `now` is UTC by convention here — stamp it explicitly.
        "since": int(now.replace(tzinfo=timezone.utc).timestamp()),
        "venue": ERA_JSON_META["ctrader"]["venue"],
    }
    tmp = feed_dir / (FEED_ERA_JSON + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    tmp.replace(feed_dir / FEED_ERA_JSON)


def _remove_feed_era(feed_dir: Path) -> None:
    """Delete the dir-level stamp — a stale ic-ctrader marker sitting on
    another venue's data is a LIE (venue-mix trap), so an era switch AWAY
    from ctrader must clean it up."""
    try:
        (feed_dir / FEED_ERA_JSON).unlink()
    except FileNotFoundError:
        pass


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
            seed: bool, now: datetime) -> bool:
    """Era-aware write: merge within an era, full-replace across eras.

    An era switch must only ever write a FULL history (seed), and only on a
    successful fetch — otherwise the farm is left on the previous feed until
    the next retry (the engines' stale guard blocks trading meanwhile).

    feed-era.json guard: the dir-level marker is the venue truth. A known
    era that does not match the active source refuses DELTA merges — the
    loud, dir-level venue-mix guard (a mis-set FEED_SOURCE can never
    quietly extend an IC-era history). A deliberate era switch (seed) is
    allowed through and then re-stamps: ctrader seeds write the ic-ctrader
    stamp, any other source DELETES a stale stamp (an ic-ctrader marker on
    bridge-era data is a lie). An era the fetcher does NOT recognize is
    refused entirely (fail-closed): a future marker format must never open
    the door for a silent mix. The per-TF logic below still refuses
    cross-era merges per timeframe.
    """
    marker = _read_feed_era(feed_dir)
    if marker is not None:
        marker_source = _ERA_TO_SOURCE.get(str(marker.get("era")))
        if marker_source is None:
            logger.error(
                "%s: %s carries an UNKNOWN era %r (venue %s) — refusing to "
                "write as %r (fail-closed venue-mix guard). To switch era "
                "on purpose: delete %s and seed.",
                tf, FEED_ERA_JSON, marker.get("era"), marker.get("venue"),
                source, FEED_ERA_JSON,
            )
            return False
        if marker_source != source and not seed:
            logger.error(
                "%s: %s pins this feed to era %r (venue %s) but the active "
                "source is %r — refusing the merge (venue-mix guard). A full "
                "seed may switch era on purpose.",
                tf, FEED_ERA_JSON, marker.get("era"), marker.get("venue"),
                source,
            )
            return False

    era = _read_era(feed_dir, tf)
    if era != source:
        if not seed:
            logger.error("%s: era mismatch (%s file, %s source) outside seed — "
                         "waiting for full-history fetch", tf, era, source)
            return False
        _write_atomic(feed_dir, tf, fetched)
        _write_era(feed_dir, tf, source)
        if source == "ctrader":
            _write_feed_era(feed_dir)
        elif marker is not None and marker.get("era") != ERA_JSON_META[source]["era"]:
            _remove_feed_era(feed_dir)  # a stale ic stamp on other-era data lies
        logger.info("%s: era switch %s→%s — history REPLACED (%d bars, %s → %s)",
                    tf, era, source, len(fetched), fetched.index[0], fetched.index[-1])
        return True

    existing = _load_existing(feed_dir, tf)
    merged = pd.concat([existing, fetched]) if not existing.empty else fetched
    merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    if not existing.empty and len(merged) == len(existing) and merged.index.equals(existing.index):
        logger.info("%s: no new bars (%d total)", tf, len(merged))
        if source == "ctrader":
            _write_feed_era(feed_dir)  # heals a deleted marker, idempotent
        return False

    _write_atomic(feed_dir, tf, merged)
    if source == "ctrader":
        _write_feed_era(feed_dir)
    logger.info("%s: wrote %d bars (last %s)", tf, len(merged), merged.index[-1])
    return True


def _backfill_stale_gap(feed_dir: Path, tf: str,
                        fetched: pd.DataFrame | None,
                        now: datetime,
                        source: str) -> pd.DataFrame | None:
    """Deep re-fetch when the delta window did not reach the stored tail.

    2026-11-07: a fetcher outage LONGER than the delta window (~25 trading
    hours for M5) plants a PERMANENT hole — every later poll looks only at
    the newest window, so bars older than the window that the source still
    has are never merged (until a fetcher restart re-seeds). Compare the
    store's last bar with the fetched window's first bar: when the window
    does not reach back to the tail, ask the SAME source (read-only, same
    era — never yfinance, never a mix) for enough bars to cover [store tail
    → now] and merge in time order. _commit dedupes by timestamp, so
    overlapping frames can never double-write.

    False-positive free: the delta window overlaps the stored tail in every
    normal situation (fresh tail, daily halt, weekend — the window reaches
    ~25 trading hours back, past the Friday session into Thursday), so the
    deep fetch fires only for a real beyond-window hole. A market closure
    the source genuinely has no bars for (e.g. the 22:00-22:55 UTC daily
    halt post-DST on Exness) cannot be healed by ANY fetch — that is a
    closure, not missing data (verified live 2026-11-07: the bridge holds
    zero bars there, every trading day shows the identical pattern).
    """
    if fetched is None or fetched.empty:
        return fetched
    if source not in ("bridge", "ctrader"):
        return fetched  # deep backfill only heals broker-era feeds
    if _read_era(feed_dir, tf) != source:
        return fetched  # cross-era writes are refused by _commit anyway
    existing = _load_existing(feed_dir, tf)
    if existing.empty:
        return fetched
    interval = BAR_CLOSE_SECONDS[tf]
    store_last = existing.index[-1]
    fetched_first = fetched.index[0]
    if fetched_first <= store_last + pd.Timedelta(seconds=interval):
        return fetched  # window reached the tail — contiguous, nothing to heal
    needed = int((now - store_last).total_seconds() // interval) + BRIDGE_DELTA_COUNT[tf]
    count = min(BRIDGE_SEED_COUNT[tf], max(needed, BRIDGE_DELTA_COUNT[tf]))
    logger.info("%s: delta window starts at %s but the store tail is %s — "
                "backfilling %d bars from the same %s source",
                tf, fetched_first, store_last, count, source)
    if source == "bridge":
        deep = _fetch_bridge_with_retry(tf, count)
    else:
        deep = _fetch_ctrader_with_retry(tf, count)
    if deep is None or deep.empty:
        logger.warning("%s: backfill fetch failed — hole remains until next poll", tf)
        return fetched
    deep = _drop_unclosed(deep, tf, now)
    if deep.empty:
        return fetched
    return pd.concat([fetched, deep])


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

    if source in ("bridge", "ctrader"):
        # Same depths for both broker feeds (cTrader chunks trendbar windows
        # itself — a count is just a depth request).
        count = BRIDGE_SEED_COUNT[tf] if seed else BRIDGE_DELTA_COUNT[tf]
        if source == "bridge":
            fetched = _fetch_bridge_with_retry(tf, count)
        else:
            fetched = _fetch_ctrader_with_retry(tf, count)
        if not seed:
            fetched = _backfill_stale_gap(feed_dir, tf, fetched, now, source)
    else:
        fetched = _fetch_with_retry(tf, SEED_PERIOD[tf] if seed else DELTA_PERIOD[tf])

    if fetched is None or fetched.empty:
        logger.warning("%s: fetch failed — keeping previous CSV", tf)
        return False

    fetched = _drop_unclosed(fetched, tf, now)
    if fetched.empty:
        logger.info("%s: no closed bars yet — keeping previous CSV", tf)
        return False

    return _commit(feed_dir, tf, fetched, source, seed, now)


def _pending_era_switch(feed_dir: Path, source: str) -> list[str]:
    """Timeframes whose on-disk era differs from the active source.

    Recovers automatically after a failed switch (bridge down at boot left
    the yfinance history in place) and after a lost marker file: the run
    loop re-seeds those timeframes before any incremental merge can happen.
    """
    if source not in ("bridge", "ctrader"):
        return []
    return [tf for tf in ("M5", "H1", "D1") if _read_era(feed_dir, tf) != source]


def run(feed_dir: Path, interval: int, htf_interval: int, once: bool = False,
        source: str | None = None) -> None:
    source = (source or FEED_SOURCE).strip().lower()
    feed_dir.mkdir(parents=True, exist_ok=True)
    detail = ""
    if source == "bridge":
        detail = f" bridge={BRIDGE_HOST}:{BRIDGE_PORT}"
    elif source == "ctrader":
        cfg = _ctrader_config_from_env()
        detail = (f" ctrader={cfg.host_type} symbol={cfg.symbol}"
                  f" configured={cfg.is_configured}")
    logger.info("Paper feed fetcher starting — feed_dir=%s source=%s%s",
                feed_dir, source, detail)

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

    if args.source in ("bridge", "ctrader") and args.interval > 60:
        # Realtime feed — 60s poll keeps bar age << engines' 6-min window.
        logger.info("%s mode: interval %ds → 60s (realtime feed)",
                    args.source, args.interval)
        args.interval = 60

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run(Path(args.feed_dir), args.interval, args.htf_interval, once=args.once,
        source=args.source)


if __name__ == "__main__":
    main()