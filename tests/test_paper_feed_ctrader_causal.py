"""cTrader feed source for the paper farm — wiring + safety contract.

Task 18/11/2026 (Hermes invest scope): FEED_SOURCE=ctrader pulls XAUUSD
M5/H1/D1 trendbars from the cTrader Open API (IC Markets) through the
existing CTraderBridge — READ-ONLY market data, zero network in tests.

Invariants proven here:
1. Era discipline: IC-era bars never merge onto another venue's history.
   The IC era stamps feed-era.json {era: ic-ctrader, since, venue}; a
   marker pointing anywhere else refuses LOUD — no silent mix (the
   yfinance→Exness lesson, never again). Era switches on seed are
   allowed: ctrader restamps, other sources delete the stale stamp.
2. Read-only guard: the bridge is reachable ONLY through
   _ReadOnlyCTraderView (no attribute path to any order method exists);
   text/AST contract here + wire-level proof in TestEndToEndMock.
3. Token expired/missing = fail-loud + the API error code in the log
   with the remediation, then the feed FREEZES (auth-dead, zero further
   wire traffic). NO yfinance fallback — the engines' stale guard
   blocks trading; that is the designed behavior.
4. Backfill: a gap beyond the delta window heals from the SAME source,
   deduped by timestamp (mirror of the 2026-11-07 bridge backfill fix).
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import paper_feed_fetcher as pff  # noqa: E402

from ctrader import CTraderApiError, CTraderBridge, CTraderConfig  # noqa: E402
from ctrader import messages as msg  # noqa: E402
from tests.test_ctrader_bridge import (  # noqa: E402
    FakeTransport,
    assets_payload,
    error_res,
    gold_symbols_payload,
    make_config,
    symbol_by_id_payload,
)

FETCHER_FILE = ROOT / "scripts" / "paper_feed_fetcher.py"
COMPOSE_FILE = ROOT / "docker-compose.paper-farm.yml"

# Fake "now" for deterministic bar-closure math (never the real wall clock).
NOW = datetime(2026, 11, 18, 12, 0)
UTC_NOW_TS = int(NOW.replace(tzinfo=timezone.utc).timestamp())

# The ONLY payload types a read-only feed may ever put on the wire.
READ_ONLY_WIRE = {
    msg.APPLICATION_AUTH_REQ,
    msg.ACCOUNT_AUTH_REQ,
    msg.VERSION_REQ,
    msg.SYMBOLS_LIST_REQ,
    msg.SYMBOL_BY_ID_REQ,
    msg.ASSET_LIST_REQ,
    msg.SUBSCRIBE_SPOTS_REQ,
    msg.GET_TRENDBARS_REQ,
    msg.HEARTBEAT_EVENT,
}

# The bridge surface the fetcher may reach (view delegation allowlist).
VIEW_ALLOWLIST = {"connect", "disconnect", "get_candles", "last_connect_error"}


def _bars(start: str, n: int, freq="5min", base=4200.0) -> pd.DataFrame:
    """Fetch-seam bars (same builder the legacy fetcher tests use)."""
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


def _minutes(ts) -> int:
    return int(pd.Timestamp(ts).timestamp() // 60)


def trendbars(start, n: int, base=4200.0, period_min=5) -> dict:
    """ProtoOAGetTrendbarsResponse payload with realistic scaled fields.

    Trendbar prices are low + delta{Open,High,Close} in 1/100000 —
    bar i: low=base-1+i*0.5, open=low+1, high=low+2, close=low+1.5.
    """
    start_min = _minutes(start)
    bars = []
    for i in range(n):
        low = base + i * 0.5 - 1.0
        bars.append({
            "utcTimestampInMinutes": start_min + i * period_min,
            "volume": 100 + i,
            "low": int(low * 100_000),
            "deltaOpen": 100_000,
            "deltaClose": 150_000,
            "deltaHigh": 200_000,
        })
    return {"trendbar": bars}


def _wire_bars(transport: FakeTransport, start: str, n: int) -> None:
    """(Re)script the trendbar reply the NEXT fetch will consume —
    CTraderFeed is stateless: one connection per fetch."""
    transport.replies[msg.GET_TRENDBARS_REQ] = [trendbars(start, n)]


def _connected_transport() -> FakeTransport:
    """Session-scoped fake: CTraderFeed's stateless design runs each fetch
    in a FRESH asyncio.run loop, and in production each fetch builds a
    fresh TcpNewlineTransport (bridge-constructed). FakeTransport's queue
    binds to the first loop it sees — so this subclass resets the queue on
    connect(), modeling the fresh session the real transport gives."""
    class SessionFakeTransport(FakeTransport):
        async def connect(self) -> None:
            self._queue = asyncio.Queue()  # fresh session, fresh queue
            self.connected = True

    transport = SessionFakeTransport()
    transport.replies[msg.SYMBOLS_LIST_REQ] = [gold_symbols_payload()]
    transport.replies[msg.SYMBOL_BY_ID_REQ] = [symbol_by_id_payload()]
    transport.replies[msg.ASSET_LIST_REQ] = [assets_payload()]
    return transport


@pytest.fixture
def ctrader_singleton():
    """Guarantee the shared CTraderFeed seam is clean after e2e tests
    (CTraderFeed is stateless — nothing to close, only the singleton)."""
    yield
    pff._ctrader_feed = None


class TestCTraderFeedUnit:
    """CTraderFeed — stateless per-fetch connection, mock transport only."""

    def _feed(self, transport: FakeTransport) -> "pff.CTraderFeed":
        return pff.CTraderFeed(make_config(), transport=transport)

    def test_fetch_returns_scaled_candles(self):
        transport = _connected_transport()
        _wire_bars(transport, "2026-11-18 11:00", 3)
        df = self._feed(transport).fetch("M5", 3)
        assert df is not None and len(df) == 3
        assert list(df.columns) == ["open", "high", "low", "close", "volume"]
        assert df.index[0] == pd.Timestamp("2026-11-18 11:00")
        # low+deltaOpen = (4200-1)+1 = 4200.0 — the 1/100000 scaling holds
        assert abs(df["open"].iloc[0] - 4200.0) < 1e-6
        assert abs(df["low"].iloc[0] - 4199.0) < 1e-6
        reqs = transport.sent_of(msg.GET_TRENDBARS_REQ)
        assert reqs, "candles must come from ProtoOAGetTrendbarsReq"
        assert reqs[0].payload["period"] == msg.TrendbarPeriod.M5
        assert reqs[0].payload["symbolId"] == 101  # resolved from symbols list

    def test_token_expired_fails_loud_then_freezes(self, caplog):
        """CH_ACCESS_TOKEN_EXPIRED: named in the log with the remediation,
        then the feed is auth-dead — zero further wire traffic, never
        yfinance. (Retrying a dead token is noise without signal.)"""
        transport = _connected_transport()
        transport.replies[msg.ACCOUNT_AUTH_REQ] = [
            error_res("CH_ACCESS_TOKEN_EXPIRED", "access token expired"),
        ]
        feed = self._feed(transport)
        caplog.set_level("ERROR")
        assert feed.fetch("M5", 3) is None
        assert feed._auth_dead is True
        assert "CH_ACCESS_TOKEN_EXPIRED" in caplog.text, (
            "the log must NAME the failure (spec: fail-loud with explanation)"
        )
        assert "refresh" in caplog.text, "the log must say how to fix it"
        sent_after_first = len(transport.sent_messages)
        assert feed.fetch("M5", 3) is None
        assert len(transport.sent_messages) == sent_after_first, (
            "an auth-dead feed must produce NO further wire traffic"
        )

    def test_missing_credentials_fail_loud_without_network(self, caplog):
        caplog.set_level("ERROR")
        transport = _connected_transport()
        # UNCONFIGURED feed: no credentials at all — nothing may hit the wire
        assert pff.CTraderFeed(CTraderConfig(), transport=transport).fetch("M5", 3) is None
        assert transport.sent_messages == [], (
            "nothing may be sent before credentials exist"
        )
        assert "CTRADER_CLIENT_ID" in caplog.text, (
            "the log must say WHICH credentials are missing"
        )

    def test_read_only_view_blocks_order_methods(self):
        """The bridge is reachable only through the view: no attribute
        path from the fetcher to any order method can exist."""
        view = pff._ReadOnlyCTraderView(
            CTraderBridge(make_config(), transport=FakeTransport()))
        for forbidden in (
            "send_order", "close_position", "close_position_with_fill",
            "amend_position_sltp",
        ):
            with pytest.raises(AttributeError):
                getattr(view, forbidden)

    def test_broken_poll_does_not_poison_the_next_one(self):
        """Stateless by construction: each fetch opens a FRESH session (in
        production a fresh TcpNewlineTransport per bridge). A poll that
        dies must not poison the next one — the empty poll re-serves the
        last genuine frame (stale beats empty), auth stays alive, and the
        next successful poll returns FRESH data."""
        transport = _connected_transport()
        _wire_bars(transport, "2026-11-18 11:00", 3)
        feed = self._feed(transport)
        assert feed.fetch("M5", 3) is not None

        transport.no_reply.add(msg.GET_TRENDBARS_REQ)  # trendbars request dies
        cached = feed.fetch("M5", 3)
        assert cached is not None and not cached.empty, (
            "a broken poll must re-serve the last genuine frame — stale "
            "beats empty at the feed level too"
        )
        assert feed._auth_dead is False, "a broken poll is NOT an auth death"

        transport.no_reply.clear()
        _wire_bars(transport, "2026-11-18 11:00", 3)
        fresh = feed.fetch("M5", 3)
        assert fresh is not None and not fresh.empty, (
            "the next poll must fetch fresh data from a fresh session — "
            "a stateless feed has no poisoned handle to heal"
        )

    def test_last_frame_cache_keeps_delta_alive_on_flaky_polls(self):
        """A fetch that returns no bars must not lose the previous poll's
        frame: the feed serves the last genuine raw frame (merge-dedup in
        _commit makes re-serving safe; the engines' stale guard covers a
        true outage). Auth-dead still returns None (freeze)."""
        transport = _connected_transport()
        _wire_bars(transport, "2026-11-18 11:00", 3)
        feed = self._feed(transport)
        first = feed.fetch("M5", 3)
        assert first is not None and len(first) == 3

        transport.replies[msg.GET_TRENDBARS_REQ] = []  # next poll: no bars
        transport.no_reply.add(msg.GET_TRENDBARS_REQ)
        second = feed.fetch("M5", 3)
        assert second is not None and len(second) == 3, (
            "a failed delta poll must keep serving the last genuine frame — "
            "stale beats empty, same rule as the CSV layer"
        )


class TestCTraderEraDiscipline:
    """Era marker: the IC era is stamped and enforced — never mix venues."""

    def _seed_other_era(self, tmp_path, era: str) -> pd.DataFrame:
        """On-disk history from another venue (yfinance or Exness bridge)."""
        old = _bars("2026-11-10 00:00", n=100, base=3990.0)
        pff._write_atomic(tmp_path, "M5", old)
        if era != "yfinance":
            pff._write_era(tmp_path, "M5", era)
        return old

    def _seed_ic_era(self, tmp_path, n=100) -> pd.DataFrame:
        history = _bars("2026-11-17 00:00", n=n, base=4200.0)
        pff._write_atomic(tmp_path, "M5", history)
        pff._write_era(tmp_path, "M5", "ctrader")
        pff._write_feed_era(tmp_path)
        return history

    def test_switch_replaces_history_and_stamps_ic_era(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(pff, "_now_utc", lambda: NOW)
        old = self._seed_other_era(tmp_path, "bridge")
        history = _bars("2026-11-17 00:00", n=432, base=4200.0)
        monkeypatch.setattr(pff, "_ctrader_fetch", lambda tf, count: history)

        assert pff.update_timeframe(tmp_path, "M5", seed=True, source="ctrader") is True

        out = _read(tmp_path)
        assert out.index[0] == history.index[0], "bridge-era bars must be GONE"
        assert len(out) == len(history)
        assert pff._read_era(tmp_path, "M5") == "ctrader"
        marker = json.loads((tmp_path / "feed-era.json").read_text())
        assert marker == {
            "era": "ic-ctrader",
            "since": UTC_NOW_TS,
            "venue": "IC Markets",
        }, "the era stamp must be exactly the spec'd shape"

    def test_delta_refused_across_era(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pff, "_now_utc", lambda: NOW)
        old = self._seed_other_era(tmp_path, "bridge")
        delta = _bars("2026-11-18 10:00", n=5, base=4200.0)
        monkeypatch.setattr(pff, "_ctrader_fetch", lambda tf, count: delta)

        assert pff.update_timeframe(tmp_path, "M5", seed=False, source="ctrader") is False
        assert len(_read(tmp_path)) == len(old), "cross-era merge must be refused"
        assert pff._read_era(tmp_path, "M5") == "bridge"
        assert not (tmp_path / "feed-era.json").exists(), (
            "a refused delta must not stamp an era it did not switch to"
        )

    def test_delta_merges_within_ic_era(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pff, "_now_utc", lambda: NOW)
        history = self._seed_ic_era(tmp_path, n=100)  # tail 11-17 08:15
        delta = _bars(history.index[-1], n=6, base=4210.0)  # overlap + 5 new
        monkeypatch.setattr(pff, "_ctrader_fetch", lambda tf, count: delta)

        assert pff.update_timeframe(tmp_path, "M5", seed=False, source="ctrader") is True

        out = _read(tmp_path)
        assert out.index.duplicated().sum() == 0
        assert len(out) == len(history) + 5
        assert out.index[-1] == delta.index[-1]

    def test_failed_switch_keeps_old_era_and_history(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(pff, "_now_utc", lambda: NOW)
        monkeypatch.setattr(pff, "FETCH_RETRY_SLEEP", 0)
        old = self._seed_other_era(tmp_path, "bridge")
        monkeypatch.setattr(pff, "_ctrader_fetch", lambda tf, count: None)

        assert pff.update_timeframe(tmp_path, "M5", seed=True, source="ctrader") is False
        assert len(_read(tmp_path)) == len(old), "failed fetch keeps previous CSV"
        assert pff._read_era(tmp_path, "M5") == "bridge", (
            "the era marker must NOT move on a failed fetch"
        )
        assert not (tmp_path / "feed-era.json").exists()

    def test_switch_away_deletes_ic_era_stamp(self, tmp_path, monkeypatch):
        """Era switches on seed are allowed — but a source leaving the IC
        era must DELETE the stamp: a stale ic-ctrader marker on
        bridge-era data is a lie (venue-mix trap)."""
        monkeypatch.setattr(pff, "_now_utc", lambda: NOW)
        self._seed_ic_era(tmp_path, n=50)
        assert (tmp_path / "feed-era.json").exists()

        back = _bars("2026-11-17 00:00", n=50, base=3990.0)
        monkeypatch.setattr(pff, "_bridge_fetch", lambda tf, count: back)
        assert pff.update_timeframe(tmp_path, "M5", seed=True, source="bridge") is True
        assert pff._read_era(tmp_path, "M5") == "bridge"
        assert not (tmp_path / "feed-era.json").exists(), (
            "stale ic-ctrader stamp on bridge-era data = venue-mix trap"
        )

    def test_foreign_feed_era_marker_refuses_delta(
            self, tmp_path, monkeypatch, caplog):
        """A feed-era.json pointing at another venue while the source is
        ctrader must refuse loudly — belt-and-braces on top of the per-TF
        marker (which alone can drift/lie)."""
        monkeypatch.setattr(pff, "_now_utc", lambda: NOW)
        pff._write_atomic(tmp_path, "M5", _bars("2026-11-17 00:00", n=50))
        pff._write_era(tmp_path, "M5", "ctrader")
        (tmp_path / "feed-era.json").write_text(json.dumps(
            {"era": "exness-mt5", "since": 1, "venue": "Exness"}))

        delta = _bars("2026-11-18 10:00", n=5)
        monkeypatch.setattr(pff, "_ctrader_fetch", lambda tf, count: delta)

        caplog.set_level("ERROR")
        assert pff.update_timeframe(tmp_path, "M5", seed=False, source="ctrader") is False
        assert "exness-mt5" in caplog.text, "the refusal must NAME the foreign era"
        assert len(_read(tmp_path)) == 50, "nothing may be written on a refusal"

    def test_lost_per_tf_marker_recovers_era_from_feed_era_json(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(pff, "_now_utc", lambda: NOW)
        self._seed_ic_era(tmp_path, n=50)

        (tmp_path / "XAUUSD_M5.csv.era").unlink()  # marker lost
        assert pff._read_era(tmp_path, "M5") == "ctrader", (
            "feed-era.json must keep the venue truth when the per-TF marker dies"
        )
        # ...and a yfinance source must therefore NOT merge onto IC history
        fresh = _bars("2026-11-18 10:00", n=5, base=3960.0)
        monkeypatch.setattr(pff, "_fetch_with_retry", lambda tf, period: fresh)
        assert pff.update_timeframe(tmp_path, "M5", seed=False, source="yfinance") is False

    def test_since_stamp_kept_for_later_tf_switches(self, tmp_path, monkeypatch):
        later = datetime(2026, 11, 18, 14, 0)
        monkeypatch.setattr(pff, "_now_utc", lambda: NOW)
        pff._write_feed_era(tmp_path)
        first = json.loads((tmp_path / "feed-era.json").read_text())
        assert first["since"] == UTC_NOW_TS

        monkeypatch.setattr(pff, "_now_utc", lambda: later)
        pff._write_feed_era(tmp_path)
        second = json.loads((tmp_path / "feed-era.json").read_text())
        assert second == first, (
            "since = when the IC era BEGAN (H1/D1 switches must not restamp it)"
        )


class TestCTraderSourceFailure:
    """Token/endpoint down = fail-loud, NEVER a yfinance fallback."""

    def test_ctrader_down_keeps_history_and_never_calls_yfinance(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(pff, "_now_utc", lambda: NOW)
        monkeypatch.setattr(pff, "FETCH_RETRY_SLEEP", 0)
        old = _bars("2026-11-10 00:00", n=100, base=3990.0)
        pff._write_atomic(tmp_path, "M5", old)

        monkeypatch.setattr(pff, "_ctrader_fetch", lambda tf, count: None)

        def _no_yfinance(tf, period):
            raise AssertionError("ctrader mode must NEVER fall back to yfinance")

        monkeypatch.setattr(pff, "_fetch_with_retry", _no_yfinance)
        assert pff.update_timeframe(tmp_path, "M5", seed=True, source="ctrader") is False
        assert len(_read(tmp_path)) == len(old), "failed fetch keeps previous CSV"

    def test_ctrader_down_after_switch_keeps_ic_history(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(pff, "_now_utc", lambda: NOW)
        history = _bars("2026-11-17 00:00", n=50)
        monkeypatch.setattr(pff, "_ctrader_fetch", lambda tf, count: history)
        pff.update_timeframe(tmp_path, "M5", seed=True, source="ctrader")

        monkeypatch.setattr(pff, "FETCH_RETRY_SLEEP", 0)
        monkeypatch.setattr(pff, "_ctrader_fetch", lambda tf, count: None)
        monkeypatch.setattr(pff, "_fetch_with_retry",
                            lambda tf, period: (_ for _ in ()).throw(
                                AssertionError("no yfinance fallback")))
        assert pff.update_timeframe(tmp_path, "M5", seed=False, source="ctrader") is False
        assert len(_read(tmp_path)) == len(history), "stale beats empty"

    def test_seed_requests_full_history_depth(self, tmp_path, monkeypatch):
        """Era-switch seed must pull >= 5 days of M5 (farm league continuity)."""
        asked = {}
        monkeypatch.setattr(pff, "_now_utc", lambda: NOW)

        def _recorder(tf, count):
            asked["count"] = count
            return _bars("2026-11-17 00:00", n=432)

        monkeypatch.setattr(pff, "_ctrader_fetch", _recorder)
        pff.update_timeframe(tmp_path, "M5", seed=True, source="ctrader")
        assert asked["count"] == pff.BRIDGE_SEED_COUNT["M5"]
        assert asked["count"] >= 1440, "5 days of M5 = 1440 bars minimum"

    def test_unclosed_ctrader_bar_dropped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pff, "_now_utc", lambda: NOW)  # 12:00
        fetched = _bars("2026-11-18 11:50", n=4)  # last bar opens 12:05 — forming
        monkeypatch.setattr(pff, "_ctrader_fetch", lambda tf, count: fetched)
        pff.update_timeframe(tmp_path, "M5", seed=True, source="ctrader")
        out = _read(tmp_path)
        assert out.index[-1] == pd.Timestamp("2026-11-18 11:55"), (
            "forming cTrader bars must never be written"
        )


class TestCTraderBackfill:
    """Gap beyond the delta window heals from the SAME source (no mix)."""

    def _seed_store(self, tmp_path) -> pd.DataFrame:
        store = _bars("2026-11-18 00:00", n=60)  # tail 04:55
        pff._write_atomic(tmp_path, "M5", store)
        pff._write_era(tmp_path, "M5", "ctrader")
        pff._write_feed_era(tmp_path)
        return store

    def test_gap_after_stale_is_backfilled_in_order(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pff, "_now_utc", lambda: NOW)
        self._seed_store(tmp_path)
        full = _bars("2026-11-18 00:00", n=144)          # truth: 00:00..11:55
        delta = full.iloc[84:]                           # window 07:00..11:55
        calls: list[int] = []

        def _fetch(tf, count):
            calls.append(count)
            return delta if count <= pff.BRIDGE_DELTA_COUNT["M5"] else full

        monkeypatch.setattr(pff, "_ctrader_fetch", _fetch)
        assert pff.update_timeframe(tmp_path, "M5", seed=False, source="ctrader") is True

        out = _read(tmp_path)
        assert list(out.index) == list(full.index), (
            "bars 05:00-06:55 sit beyond the delta window — the hole would "
            "survive every poll without a deep re-fetch from the same source"
        )
        assert out.index.duplicated().sum() == 0
        assert len(calls) == 2
        assert calls[0] == pff.BRIDGE_DELTA_COUNT["M5"]
        assert calls[1] > pff.BRIDGE_DELTA_COUNT["M5"]

    def test_no_gap_no_deep_fetch(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pff, "_now_utc", lambda: NOW)
        self._seed_store(tmp_path)
        fetched = _bars("2026-11-18 04:00", n=14)  # window overlaps the tail
        calls: list[int] = []

        def _fetch(tf, count):
            calls.append(count)
            return fetched

        monkeypatch.setattr(pff, "_ctrader_fetch", _fetch)
        assert pff.update_timeframe(tmp_path, "M5", seed=False, source="ctrader") is True
        assert calls == [pff.BRIDGE_DELTA_COUNT["M5"]], (
            "a contiguous fetch must NOT trigger an extra deep fetch"
        )


class TestEndToEndMock:
    """done-criteria #7: FEED_SOURCE=ctrader → bars → CSV → era marker,
    exercised through the REAL CTraderFeed (mock transport, zero network)."""

    def test_bars_to_csv_and_era_marker(self, tmp_path, monkeypatch,
                                        ctrader_singleton):
        monkeypatch.setattr(pff, "_now_utc", lambda: NOW)
        transport = _connected_transport()
        pff._ctrader_feed = pff.CTraderFeed(make_config(), transport=transport)

        # Seed: full-history fetch through the real bridge path
        _wire_bars(transport, "2026-11-18 10:00", 24)  # 10:00..11:55, closed
        assert pff.update_timeframe(tmp_path, "M5", seed=True, source="ctrader") is True
        out = _read(tmp_path)
        assert len(out) == 24
        assert out.index[-1] == pd.Timestamp("2026-11-18 11:55")
        assert abs(out["open"].iloc[0] - 4200.0) < 1e-3

        # Era markers on disk
        assert pff._read_era(tmp_path, "M5") == "ctrader"
        marker = json.loads((tmp_path / "feed-era.json").read_text())
        assert marker["era"] == "ic-ctrader"
        assert marker["venue"] == "IC Markets"
        assert marker["since"] == UTC_NOW_TS

        # Wire contract: everything sent is read-only market data
        sent_types = {m.payload_type for m in transport.sent_messages}
        assert sent_types <= READ_ONLY_WIRE, (
            f"non-read-only traffic on the wire: {sent_types - READ_ONLY_WIRE}"
        )
        assert msg.NEW_ORDER_REQ not in sent_types

        # Delta poll: forming bars dropped, overlap deduped
        _wire_bars(transport, "2026-11-18 11:50", 6)  # 11:50..12:15 — 12:00+ forming
        assert pff.update_timeframe(tmp_path, "M5", seed=False, source="ctrader") is False
        assert len(_read(tmp_path)) == 24, "forming bars must not extend the feed"

        # Clock advances — the closed 12:00..12:15 bars now merge
        monkeypatch.setattr(pff, "_now_utc", lambda: datetime(2026, 11, 18, 12, 20))
        _wire_bars(transport, "2026-11-18 11:50", 6)
        assert pff.update_timeframe(tmp_path, "M5", seed=False, source="ctrader") is True
        out = _read(tmp_path)
        assert out.index[-1] == pd.Timestamp("2026-11-18 12:15")
        assert out.index.duplicated().sum() == 0


class TestReadOnlyContract:
    """done-criteria #3: the fetcher's cTrader path can never trade.

    Text + AST guards (compile-time) — the dynamic wire proof lives in
    TestEndToEndMock (runtime, mock transport).
    """

    FORBIDDEN = (
        "send_order", "close_position", "close_position_with_fill",
        "amend_position_sltp",
    )

    def test_no_order_method_referenced_anywhere(self):
        text = FETCHER_FILE.read_text()
        for name in self.FORBIDDEN:
            assert name not in text, (
                f"paper_feed_fetcher.py must never reference {name}() — "
                "the fetcher is a READ-ONLY market-data client"
            )

    def test_only_read_payload_types_are_sent(self):
        """No order-sending payload type may be reachable from the fetcher
        (positive direction proven at runtime in TestEndToEndMock)."""
        text = FETCHER_FILE.read_text()
        for constant in ("NEW_ORDER_REQ", "AMEND_POSITION_SLTP_REQ",
                         "CLOSE_POSITION_REQ"):
            assert constant not in text, (
                f"{constant} must never be reachable from the fetcher"
            )

    def test_bridge_only_reachable_through_read_only_view(self):
        """Every CTraderBridge construction must be wrapped in the view —
        no other construction site, so no attribute path from the fetcher
        to any order method can exist. Whitespace-normalized so a
        multi-line construction still matches."""
        collapsed = re.sub(r"\s+", "", FETCHER_FILE.read_text())
        total = collapsed.count("CTraderBridge(")
        wrapped = collapsed.count("_ReadOnlyCTraderView(CTraderBridge(")
        assert total > 0, "the fetcher must construct a CTraderBridge somewhere"
        assert total == wrapped, (
            f"{total - wrapped} raw CTraderBridge construction(s) outside "
            "the read-only view — every bridge must be wrapped at construction"
        )

    def test_view_surface_limited_to_read_methods(self):
        tree = ast.parse(FETCHER_FILE.read_text())
        cls = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.ClassDef) and n.name == "_ReadOnlyCTraderView"
        )
        for node in cls.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("__") and node.name.endswith("__"):
                    continue
                assert node.name in VIEW_ALLOWLIST, (
                    f"_ReadOnlyCTraderView.{node.name} is outside the "
                    "read-only allowlist"
                )

    def test_view_delegation_only_touches_allowed_members(self):
        """Every member the view reaches on the wrapped bridge (pattern
        `self._bridge.<member>`) must be in the read-only allowlist."""
        tree = ast.parse(FETCHER_FILE.read_text())
        cls = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.ClassDef) and n.name == "_ReadOnlyCTraderView"
        )
        touched = set()
        for node in ast.walk(cls):
            if (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Attribute)
                    and node.value.attr == "_bridge"):
                touched.add(node.attr)
        assert touched <= VIEW_ALLOWLIST, (
            f"view reaches non-read members: {touched - VIEW_ALLOWLIST}"
        )

    def test_bridge_member_usage_in_module_is_read_only(self):
        """Module-level contract: every plain `bridge.<member>` reference
        in the fetcher stays on the read-only surface (the dotted import
        path metty.bridge.client is excluded by the lookbehind)."""
        text = FETCHER_FILE.read_text()
        used = set(re.findall(r"(?<![\w.])bridge\.(\w+)", text))
        assert used <= VIEW_ALLOWLIST, (
            f"fetcher reaches non-read bridge members: {used - VIEW_ALLOWLIST}"
        )


class TestFarmComposeContract:
    """done-criteria #6: CTRADER_* reaches paper-fetcher ONLY — the three
    engine services must not change by a single line."""

    def _services(self) -> dict:
        return yaml.safe_load(COMPOSE_FILE.read_text())["services"]

    def _fetcher_env(self) -> list[str]:
        return self._services()["paper-fetcher"]["environment"]

    def test_fetcher_selects_ctrader_source(self):
        assert "FEED_SOURCE=ctrader" in self._fetcher_env(), (
            "the Exness era is closed — bridge mode has nothing to talk to"
        )

    def test_fetcher_carries_ctrader_runtime_env(self):
        env = self._fetcher_env()
        keys = {e.split("=", 1)[0] for e in env}
        for key in (
            "CTRADER_CLIENT_ID", "CTRADER_CLIENT_SECRET",
            "CTRADER_ACCESS_TOKEN", "CTRADER_ACCOUNT_ID",
            "CTRADER_HOST_TYPE", "CTRADER_NAME", "CTRADER_SYMBOL",
        ):
            assert key in keys, f"{key} missing from paper-fetcher env"
        for e in env:
            if e.startswith("CTRADER_"):
                assert "${" in e, (
                    "ctrader credentials are RUNTIME values — they must "
                    "interpolate from the host .env, not be baked in"
                )

    def test_fetcher_env_names_are_read_by_the_fetcher(self):
        """The 2026-10 compose-env no-op class: grep BOTH ends before closing.
        (test_env_coverage_guard enforces the same at suite level — its
        production corpus does not include ctrader/.)"""
        text = FETCHER_FILE.read_text()
        for e in self._fetcher_env():
            key = e.split("=", 1)[0]
            if key.startswith("CTRADER_"):
                assert key in text, (
                    f"{key} is written by compose but read by NOTHING in the "
                    "fetcher — silent no-op config"
                )

    def test_engines_have_no_ctrader_env(self):
        for name, svc in self._services().items():
            if name == "paper-fetcher":
                continue
            for e in svc.get("environment", []):
                assert not e.startswith("CTRADER_"), (
                    f"{name} must not carry cTrader env (engine services untouched)"
                )

    def test_host_type_defaults_to_demo(self):
        """Demo-first is mandatory on first real connection (README plan)."""
        for e in self._fetcher_env():
            if e.startswith("CTRADER_HOST_TYPE"):
                assert "demo" in e, (
                    "the compose default must be demo — live needs explicit opt-in"
                )
