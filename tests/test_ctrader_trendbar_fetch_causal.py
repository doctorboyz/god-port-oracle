"""Causal tests: trendbar fetch for large spans (D1 x 500) — 20/11 fix.

Symptom (engine, every cycle): fetch_candles_sync("XAUUSD", "D1", 500)
returned 0 rows after ~20s with "cTrader sync call failed" — the only
per-cycle error left on IC1. Cause (probe 20/11, exact engine args):
the chunked fetcher asked one-week windows, so D1 x 500 = a 500-day
span = ~72 requests at the 5 req/s historical rate limit (~0.43s each
with RTT) ≈ 31s > the sync wrapper's request_timeout x 2 budget — the
coroutine died mid-walk with zero bars.

Fix under test — three layers, one per server behavior:
1. Wide window + count: the official message reference documents NO
   trendbar span cap (Unix bounds only) and an optional count field
   (bars back from toTimestamp) — ONE request for the whole span,
   paging backward only when the server caps rows per response
   (hasMore). The 1-week cap in the docs applies to historical TICK
   data, not trendbars.
2. Early-stop: backends that reject wide spans fall back to weekly
   windows; consecutive empty windows = start of history, stop paging
   instead of walking dozens of empty windows to the span end.
3. Budget: a wall-clock deadline (config trendbar_fetch_budget) makes
   even an unbounded history return the newest partial slice instead
   of walking past the sync timeout into zero bars.

RED proofs were captured against the pre-fix bridge via git stash.
Run: .venv/bin/python -m pytest tests/test_ctrader_trendbar_fetch_causal.py -q
"""

import time
from typing import Any, Optional

from ctrader import messages as msg

from tests.test_ctrader_bridge import (  # noqa: E402 — sibling helpers
    FakeTransport,
    connect_bridge,
    error_res,
    make_config,
)


def make_period_bars(
    n: int, period: int, spacing_minutes: int, low: float = 2350.0,
) -> list[dict[str, Any]]:
    """n bars of one period ending at 'now' — per-bar delta scheme like
    the real wire (utcTimestampInMinutes ascending, unique)."""
    now_min = int(time.time() // 60)
    return [{
        "volume": 100 + i,
        "period": period,
        "low": int((low + i) * 100_000),
        "deltaOpen": 10_000,
        "deltaClose": 20_000,
        "deltaHigh": 30_000,
        "utcTimestampInMinutes": now_min - (n - 1 - i) * spacing_minutes,
    } for i in range(n)]


class TrendbarServerTransport(FakeTransport):
    """Stateful GetTrendbars backend implementing the documented server
    semantics: bars filtered by [fromTimestamp, toTimestamp), the
    optional count field keeping the newest `count` bars back from
    toTimestamp, and hasMore when a row cap truncated the response.

    Two knobs model the backend behaviors the bridge must survive:
    - wide_reject: answer ProtoOAErrorRes to spans over one week (a
      span-capped backend — the documented 1-week cap applies to tick
      data, but a defensive backend may enforce it on trendbars too).
    - chunk_size: cap rows per response (row-capped backend) — forces
      backward paging even on the wide path.
    """

    def __init__(
        self,
        bars: list[dict[str, Any]],
        wide_reject: bool = False,
        chunk_size: Optional[int] = None,
    ) -> None:
        super().__init__()
        self.bars = sorted(bars, key=lambda b: b["utcTimestampInMinutes"])
        self.wide_reject = wide_reject
        self.chunk_size = chunk_size
        self.trendbar_requests: list[dict[str, Any]] = []

    def _next_reply(self, request) -> Optional[Any]:
        if request.payload_type != msg.GET_TRENDBARS_REQ:
            return super()._next_reply(request)
        payload = request.payload
        self.trendbar_requests.append(dict(payload))
        if request.payload_type in self.no_reply:
            return None  # deliberately never answers (timeout shapes)
        from_ms = int(payload["fromTimestamp"])
        to_ms = int(payload["toTimestamp"])
        if self.wide_reject and to_ms - from_ms > msg.TRENDBAR_MAX_WINDOW_MS:
            return error_res(
                "CH_INVALID_REQUEST", "trendbar span exceeds the weekly cap",
            )
        window = [
            bar for bar in self.bars
            if from_ms <= bar["utcTimestampInMinutes"] * 60_000 < to_ms
        ]
        limit = payload.get("count")
        if limit is not None:
            window = window[-int(limit):]
        has_more = False
        if self.chunk_size is not None and len(window) > self.chunk_size:
            window = window[-self.chunk_size:]  # newest chunk, page backward
            has_more = True
        return {"period": payload["period"], "trendbar": window, "hasMore": has_more}

    def wide_requests(self) -> list[dict[str, Any]]:
        """Trendbar requests whose span exceeds one week (the wide path)."""
        return [
            p for p in self.trendbar_requests
            if p["toTimestamp"] - p["fromTimestamp"] > msg.TRENDBAR_MAX_WINDOW_MS
        ]


def d1_bars(n: int) -> list[dict[str, Any]]:
    return make_period_bars(n, msg.TrendbarPeriod.D1, 1440)


# ----------------------------------------------------------------------
# Layer 1 — wide window + count (primary path)
# ----------------------------------------------------------------------

async def test_d1_500_one_request_when_server_honors_count():
    """D1 x 500 on a count-honoring backend: ONE full-span request
    carrying count=500 — not ~72 weekly windows at the 5 req/s limit.
    (RED on the pre-fix bridge: 72 chunked requests for 500 bars.)"""
    server = TrendbarServerTransport(d1_bars(600))
    bridge = await connect_bridge(server)
    df = await bridge.get_candles("XAUUSD", "D1", 500)
    assert len(df) == 500
    assert len(server.trendbar_requests) == 1
    request = server.trendbar_requests[0]
    assert request["count"] == 500
    assert request["symbolId"] == 101
    # The full requested span in one window, not a 1-week slice.
    assert request["toTimestamp"] - request["fromTimestamp"] == 500 * 86_400_000
    assert request["fromTimestamp"] >= 0
    await bridge.disconnect()


async def test_d1_500_pages_backward_when_server_caps_rows():
    """Row-capped backend (hasMore): the wide path pages backward with
    count=remaining — 5 pages of 100 for 500 bars, newest first."""
    server = TrendbarServerTransport(d1_bars(600), chunk_size=100)
    bridge = await connect_bridge(server)
    df = await bridge.get_candles("XAUUSD", "D1", 500)
    assert len(df) == 500
    assert [r["count"] for r in server.trendbar_requests] == [500, 400, 300, 200, 100]
    # Backward paging: every next window ends strictly before the last.
    for previous, nxt in zip(
        server.trendbar_requests, server.trendbar_requests[1:],
    ):
        assert nxt["toTimestamp"] < previous["toTimestamp"]
    await bridge.disconnect()


async def test_d1_500_returns_newest_slice_when_budget_exhausted():
    """History longer than the fetch budget allows: stop at the deadline
    and return the newest partial slice — NOT walk on until the sync
    wrapper kills the coroutine with zero bars. (RED pre-fix: no budget
    existed, the fetch walked the whole span.)"""
    server = TrendbarServerTransport(d1_bars(600), chunk_size=100)
    bridge = await connect_bridge(server, make_config(trendbar_fetch_budget=0.0))
    df = await bridge.get_candles("XAUUSD", "D1", 500)
    assert 0 < len(df) <= 100  # first page's newest bars, no error raised
    assert len(server.trendbar_requests) == 1
    await bridge.disconnect()


# ----------------------------------------------------------------------
# Layer 2 — weekly-chunk fallback: early-stop at the start of history
# ----------------------------------------------------------------------

async def test_d1_500_early_stops_at_history_start_on_span_capped_backend():
    """Span-capped backend (wide -> error) holding 130 days of history,
    request 500: the fallback walks weekly windows and STOPS after two
    consecutive empty ones (history start) — it must not chase the ~53
    empty windows up to the 500-day span end. (RED pre-fix: the chunk
    walk visited every window, ~72 requests, ~20s+ on the wire.)"""
    server = TrendbarServerTransport(d1_bars(130), wide_reject=True)
    bridge = await connect_bridge(server)
    df = await bridge.get_candles("XAUUSD", "D1", 500)
    assert len(df) == 130  # everything the server has, no error
    # 1 doomed wide attempt + ~19 data windows + 2 empty windows.
    assert 20 <= len(server.trendbar_requests) <= 23
    await bridge.disconnect()


async def test_d1_500_chunked_fallback_respects_budget():
    """Span-capped backend, history longer than the budget: the chunked
    walk stops at the deadline with the bars collected so far."""
    server = TrendbarServerTransport(d1_bars(600), wide_reject=True)
    bridge = await connect_bridge(server, make_config(trendbar_fetch_budget=0.0))
    df = await bridge.get_candles("XAUUSD", "D1", 500)
    assert 0 < len(df) <= 7  # first weekly window's bars
    assert len(server.trendbar_requests) == 2  # wide attempt + one window
    await bridge.disconnect()


async def test_wide_rejection_latches_until_reconnect():
    """A backend that rejects wide spans pays the doomed wide attempt
    once per CONNECTION, not once per fetch — and a reconnect retries
    it (the rejection may have been transient)."""
    server = TrendbarServerTransport(d1_bars(130), wide_reject=True)
    bridge = await connect_bridge(server, make_config())
    await bridge.get_candles("XAUUSD", "D1", 500)
    after_first = len(server.trendbar_requests)

    await bridge.get_candles("XAUUSD", "D1", 500)
    second = server.trendbar_requests[after_first:]
    assert second, "second fetch must still fetch bars (chunked path)"
    assert not [
        r for r in second
        if r["toTimestamp"] - r["fromTimestamp"] > msg.TRENDBAR_MAX_WINDOW_MS
    ], "no wide attempt after the rejection latch"

    # Reconnect resets the latch: a fresh connection retries wide.
    await bridge.disconnect()
    assert await bridge.connect() is True
    before = len(server.trendbar_requests)
    await bridge.get_candles("XAUUSD", "D1", 500)
    assert server.trendbar_requests[before:], "fetch after reconnect ran"
    assert any(
        r["toTimestamp"] - r["fromTimestamp"] > msg.TRENDBAR_MAX_WINDOW_MS
        for r in server.trendbar_requests[before:]
    ), "reconnect must retry the wide path"
    await bridge.disconnect()


# ----------------------------------------------------------------------
# Non-regression pins (probe numbers 20/11)
# ----------------------------------------------------------------------

async def test_m5_probe_shape_unchanged():
    """M5 x 500 (span 41h, one window either way): 486 bars in one
    request — control, green on the pre-fix bridge too."""
    server = TrendbarServerTransport(
        make_period_bars(486, msg.TrendbarPeriod.M5, 5),
    )
    bridge = await connect_bridge(server)
    df = await bridge.get_candles("XAUUSD", "M5", 500)
    assert len(df) == 486
    assert len(server.trendbar_requests) == 1
    await bridge.disconnect()


async def test_h1_and_h4_full_span_single_request():
    """H1/H4 x 500: one full-span request each (was 3/12 weekly windows
    pre-fix). Same rows as the 20/11 probe: H1 341, H4 356."""
    server = TrendbarServerTransport(
        make_period_bars(341, msg.TrendbarPeriod.H1, 60),
    )
    bridge = await connect_bridge(server)
    assert len(await bridge.get_candles("XAUUSD", "H1", 500)) == 341
    assert len(server.trendbar_requests) == 1
    await bridge.disconnect()

    server_h4 = TrendbarServerTransport(
        make_period_bars(356, msg.TrendbarPeriod.H4, 240),
    )
    bridge_h4 = await connect_bridge(server_h4)
    assert len(await bridge_h4.get_candles("XAUUSD", "H4", 500)) == 356
    assert len(server_h4.trendbar_requests) == 1
    await bridge_h4.disconnect()


async def test_zero_count_requests_nothing():
    """count=0 must not touch the wire (control shape)."""
    server = TrendbarServerTransport(
        make_period_bars(3, msg.TrendbarPeriod.M5, 5),
    )
    bridge = await connect_bridge(server)
    assert (await bridge.get_candles("XAUUSD", "M5", 0)).empty
    assert server.trendbar_requests == []
    await bridge.disconnect()


async def test_window_start_clamped_to_epoch():
    """A span predating 1970 must clamp fromTimestamp to 0 (proto:
    fromTimestamp must be >= 0) — never send a negative value."""
    server = TrendbarServerTransport([])
    bridge = await connect_bridge(server)
    # 32M M1 bars = ~61 years of span -> before the Unix epoch.
    assert (await bridge.get_candles("XAUUSD", "M1", 32_000_000)).empty
    assert server.trendbar_requests, "one wide request still goes out"
    assert server.trendbar_requests[0]["fromTimestamp"] == 0
    await bridge.disconnect()


# ----------------------------------------------------------------------
# Cross-context review round (H1/M2/M4) — budget clamp, latch narrowness,
# chunked hasMore cursor
# ----------------------------------------------------------------------

async def test_budget_bounds_in_flight_requests_under_sync_deadline():
    """H1 (review): the budget must bound IN-FLIGHT requests too — a
    page starting just before the deadline must not wait a full
    request_timeout past it (worst case budget + request_timeout + sleep
    ≈ 25s > the sync wrapper's 2 x request_timeout deadline = the
    original killed-with-zero-bars bug again)."""
    server = TrendbarServerTransport(d1_bars(600))
    server.no_reply.add(msg.GET_TRENDBARS_REQ)  # backend never answers
    bridge = await connect_bridge(
        server, make_config(request_timeout=2.0, trendbar_fetch_budget=0.3),
    )
    started = time.monotonic()
    df = await bridge.get_candles("XAUUSD", "D1", 500)
    elapsed = time.monotonic() - started
    assert df.empty
    assert elapsed < 1.5, f"fetch took {elapsed:.2f}s — budget not bounding in-flight pages"
    await bridge.disconnect()


async def test_wide_timeout_does_not_latch_rejection():
    """M2 (review): a transient wide-request TIMEOUT must not latch the
    rejection — only an explicit error payload does. Otherwise one
    hiccup degrades every later fetch on this connection to the slow
    chunked path (deep history: ~72 windows, budget-capped slices)."""
    server = TrendbarServerTransport(d1_bars(600))
    server.no_reply.add(msg.GET_TRENDBARS_REQ)
    bridge = await connect_bridge(
        server, make_config(request_timeout=0.2, trendbar_fetch_budget=1.0),
    )
    await bridge.get_candles("XAUUSD", "D1", 500)
    first_wide = len(server.wide_requests())
    assert first_wide == 1
    await bridge.get_candles("XAUUSD", "D1", 500)
    # Timeout != rejection: the next fetch retries the wide path.
    assert len(server.wide_requests()) == 2
    await bridge.disconnect()


async def test_chunked_hasmore_cursor_excludes_boundary_bar():
    """M4 (review): the chunked path's hasMore cursor must use the same
    exclusive bound as the wide path (oldest - 1ms) — an inclusive
    cursor re-fetches the boundary bar every page, inflating len(bars)
    and ending the walk with fewer UNIQUE bars than requested."""
    from tests.test_ctrader_bridge import FakeTransport

    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    now_min = int(time.time() // 60)

    def bar(minutes: int) -> dict[str, Any]:
        return {"volume": 1, "period": msg.TrendbarPeriod.M5, "low": 1,
                "deltaOpen": 0, "deltaClose": 0, "deltaHigh": 0,
                "utcTimestampInMinutes": minutes}

    # Wide rejected (error payload) -> latch -> chunked pages below.
    transport.replies[msg.GET_TRENDBARS_REQ] = [
        error_res("CH_INVALID_REQUEST", "span too wide"),
        {"period": msg.TrendbarPeriod.M5, "hasMore": True,
         "trendbar": [bar(now_min), bar(now_min - 5)]},
        {"period": msg.TrendbarPeriod.M5, "hasMore": False,
         "trendbar": [bar(now_min - 10), bar(now_min - 15)]},
    ]
    df = await bridge.get_candles("XAUUSD", "M5", 4)
    assert len(df) == 4
    requests = transport.sent_of(msg.GET_TRENDBARS_REQ)
    assert len(requests) == 3  # wide attempt + two chunked pages
    assert requests[2].payload["toTimestamp"] == (now_min - 5) * 60_000 - 1
    await bridge.disconnect()
