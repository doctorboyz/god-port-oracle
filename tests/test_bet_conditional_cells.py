"""Conditional bet cells (BET-RF / BET-SF) — gate + event families, T2.

Locks the iron constraints of the task (คุณหมอ 13/11):

1. Theory-free formula: families R/S fire SELL at EVERY event of the
   indicator — BOTH polarities (a cross up through 30 fires SELL exactly
   like a cross down through 70). Polarity is never a filter; this is the
   result that won the conditional grid and must not regress.
2. Gate causality: the h4_ema state is read from CLOSED H4 buckets only
   (align_state_to_m5 machinery) — mutating/extending the bucket that is
   still forming cannot move the gate's answer, and a bucket closing
   exactly at the signal moment becomes visible.
3. Live path: FLAT + event → the bet opens with the champion's contract
   geometry; any other state → skipped with a clear log, no bet.
4. The contract (TP/SL/seed/risk/spread) matches the playbook champions
   byte-for-byte via the yml loader.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from broky.bet import cells, entries, gates, store  # noqa: E402
from broky.betbracket import events  # noqa: E402
from broky.betbracket.htf import STATE_FLAT, STATE_UP  # noqa: E402
from scripts import bet_engine  # noqa: E402

# ── synthetic frame builders ────────────────────────────────────────────────
# H4 bucket = 48 M5 bars. The h4_ema detector needs 50 warmup buckets,
# so gate frames carry 54-56 buckets (~2,600-2,700 M5 bars). Frames END at
# the current moment so the live engine's stale-feed guard (1800s) stays
# open; the event-crafting tail always sits INSIDE the forming H4 bucket
# — which the gate cannot see (causal) — so the closed buckets alone
# decide FLAT/UP.


def _now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC").tz_localize(None)


def make_m5(closes: list[float] | np.ndarray, *, start=None, end=None,
            high_pad: float = 0.5, low_pad: float = 0.5) -> pd.DataFrame:
    closes_arr = np.asarray(closes, dtype=float)
    opens = np.concatenate([[closes_arr[0]], closes_arr[:-1]])
    kwargs: dict = {"periods": len(closes_arr), "freq": "5min"}
    kwargs["start" if start is not None else "end"] = (
        start if start is not None else end)
    idx = pd.date_range(**kwargs)
    return pd.DataFrame(
        {
            "open": opens,
            "high": closes_arr + high_pad,
            "low": closes_arr - low_pad,
            "close": closes_arr,
            "volume": [100.0] * len(closes_arr),
        },
        index=idx,
    )


def flat_history(n_buckets: int = 54, base: float = 4000.0) -> pd.DataFrame:
    """Constant closes → EMA50 slope 0 → FLAT on every closed H4 bucket.

    Ends one bar before "now" so the last closed bucket is fully closed
    and the stale-feed guard stays open."""
    return make_m5([base] * (n_buckets * 48),
                   end=_now() - pd.Timedelta(minutes=5))


def rising_history(n_buckets: int = 56, base: float = 4000.0,
                   step: float = 4.0) -> pd.DataFrame:
    """Steady rise → EMA50 slope ≈ step/H4-bar (10bp ≫ 2bp) → UP."""
    closes = [base + step * (i // 48) for i in range(n_buckets * 48)]
    return make_m5(closes, end=_now() - pd.Timedelta(minutes=5))


def _tail_bars(family: str) -> int:
    """Bars in the event-crafting tail of a family (see docstring below)."""
    return (15 if family == entries.FAMILY_RSI_REV_SELL
            else 21 if family == entries.FAMILY_STOCH_REV_SELL else 0)


def _anchored_base(base: pd.DataFrame, want_state: str,
                   tail_bars: int) -> pd.DataFrame:
    """Extend the base so the tail cannot cross an H4 bucket boundary.

    The gate is FRAME-RELATIVE: a bucket becomes visible once its close time
    is ≤ the last M5 bar's close. A tail that spills across a 4-hour
    boundary therefore CLOSES its own bucket and the gate reads the tail
    instead of the base — the helper used to depend on wall clock (red
    within ~105 min of every boundary; proven on a clean HEAD worktree
    2026-11-17 — red at 02:40 UTC, green at 02:05 UTC, same tree). The fix:
    look at the H4 bucket CONTAINING the tail's first bar; if that bucket
    closes before the tail ends, extend the base IN ITS OWN PATTERN (flat
    stays flat, rising keeps rising by rising_history's per-bucket step)
    so the tail starts ON/AFTER that bucket's end — every bucket visible
    to the gate is then pure base, and the tail lives inside one forming
    bucket. The stale-feed guard still passes — it compares the frame's
    LAST bar against now, and the frame already extends past now by design.

    Grid-safety (2026-11-17, cross-context review catch): the frame's bar
    timestamps carry `_now()`'s seconds (pd.date_range(end=...) does NOT
    snap to the 5-min grid — verified empirically), while bucket ends are
    grid-aligned. So bucket_end - tail_first_open is usually NOT a
    multiple of 5 min and a floor division loses a bar: the tail's first
    bar then lands inside the closing bucket and — H4 close being the LAST
    member bar's close — a single leaked bar flips the gate (proven: spike
    scenario read UP at 36 of 50 sampled minutes, sec=0, and at every
    off-grid second; RSI/Stoch family tails share the helper). extend_bars
    is therefore CEIL, floored at 1 (0 bars → make_m5([]) → IndexError,
    proven at minutes ≥ 235-236 of the bucket), and the bucket end is the
    CONTAINING bucket's end — an exact on-grid tail start (08:00:00)
    belongs to the bucket starting there, not the one ending there.
    """
    tail_bars = _tail_bars_guard(tail_bars)
    tail_first_open = base.index[-1] + pd.Timedelta(minutes=5)
    # END of the H4 bucket CONTAINING the tail's first bar. On-grid exact
    # hits (e.g. 08:00:00) belong to the bucket STARTING there (closed-left
    # bins), so bump an exact match to the following boundary.
    bucket_end = tail_first_open.ceil("4h")
    if bucket_end == tail_first_open:
        bucket_end += pd.Timedelta(hours=4)
    last_tail_close = tail_first_open + pd.Timedelta(minutes=5 * tail_bars)
    if bucket_end > last_tail_close:
        return base  # the whole tail lives inside that still-forming bucket
    # extend so the tail's first open lands ON/AFTER bucket_end:
    #   tail_first_open + 5min*extend >= bucket_end  →  ceil, never 0
    #   (ceil, because the off-grid seconds make the delta a non-multiple
    #    of 5 min — floor drops a bar back into the closing bucket)
    extend_bars = max(1, math.ceil(
        (bucket_end - tail_first_open).total_seconds() / 300))
    last_close = float(base["close"].iloc[-1])
    if want_state == STATE_UP:
        # rising_history: +step per 48-bar bucket → per-bar continuation
        step_per_bar = 4.0 / 48.0
        ext_closes = [last_close + step_per_bar * (k + 1)
                      for k in range(extend_bars)]
    else:
        ext_closes = [last_close] * extend_bars
    ext = make_m5(ext_closes, start=tail_first_open)
    return pd.concat([base, ext])


def _tail_bars_guard(tail_bars: int) -> int:
    if tail_bars <= 0:
        raise ValueError("unknown family — no tail recipe")
    return tail_bars


def frame_with_event_at_last_bar(family: str, want_state: str
                                 ) -> tuple[pd.DataFrame, str]:
    """A frame whose LAST closed bar is a BUY-polarity event of `family`
    (indicator hints UP) while the gate reads `want_state`.

    The recipe: a base history whose closed H4 buckets are all FLAT (or
    all UP), then a short decline+pop tail crafted to cross RSI 30 up /
    Stoch 20 up on its last bar. The tail lives inside the forming H4
    bucket — invisible to the gate — so the gate state comes purely from
    the base. BUY polarity on purpose: the live bet must still open SELL
    (theory-free, no polarity filter).

    The tail anchor is BUCKET-AWARE (2026-11-17): when the natural tail
    would cross an H4 boundary, the base is extended to the next bucket
    first (see _anchored_base) — the gate's answer must never depend on
    when the test happens to run.
    """
    if want_state == STATE_FLAT:
        base = flat_history(54)
    elif want_state == STATE_UP:
        base = rising_history(56)
    else:
        raise ValueError(want_state)
    base = _anchored_base(base, want_state, _tail_bars(family))
    last_close = float(base["close"].iloc[-1])
    if family == entries.FAMILY_RSI_REV_SELL:
        # decline (RSI → 0) then a pop (RSI jumps ≥ 30): cross 30 upward
        tail_closes = ([last_close - 4.0 * (k + 1) for k in range(14)]
                       + [last_close + 60.0])
    elif family == entries.FAMILY_STOCH_REV_SELL:
        # decline (%K → ~0) then a pop (%K jumps ≥ 20): cross 20 upward
        tail_closes = ([last_close - 5.0 * (k + 1) for k in range(20)]
                       + [last_close + 25.0])
    else:
        raise ValueError(family)
    tail = make_m5(tail_closes,
                  start=base.index[-1] + pd.Timedelta(minutes=5))
    frame = pd.concat([base, tail])
    ev = (events.EVENT_RSI_REVERSAL if family == entries.FAMILY_RSI_REV_SELL
          else events.EVENT_STOCH_REVERSAL)
    polarity = events.event_polarity_series(ev, frame)
    pol = str(polarity.iloc[-1])
    assert pol == "BUY", (
        f"crafted tail did not produce a BUY-polarity {family} event on the "
        f"last bar (polarity={pol!r}) — fix the tail, not the assertion"
    )
    return frame, pol


class _FakeFeed:
    """TickFeed stand-in: fixed mid, no bridge."""

    def __init__(self, mid: float = 4000.0) -> None:
        self._mid = mid

    def mid(self):
        return self._mid


# ── 1. event families fire SELL at every event, both polarities ────────────


class TestEventFamilies:
    def test_r_fires_sell_at_every_rsi_event(self):
        rng = np.random.RandomState(11)
        df = make_m5(4000.0 + np.cumsum(rng.normal(0, 6.0, 600)),
                     end=_now() - pd.Timedelta(minutes=5))
        series = entries.family_signal_series(entries.FAMILY_RSI_REV_SELL, df)
        polarity = events.event_polarity_series(events.EVENT_RSI_REVERSAL, df)
        fires = polarity.isin(["BUY", "SELL"])
        assert fires.sum() > 0
        assert (series[fires] == entries.SELL).all()          # every event → SELL
        assert (series[~fires] == "").all()                   # nothing else fires

    def test_s_fires_sell_at_every_stoch_event(self):
        rng = np.random.RandomState(7)
        df = make_m5(4000.0 + np.cumsum(rng.normal(0, 6.0, 600)),
                     end=_now() - pd.Timedelta(minutes=5))
        series = entries.family_signal_series(entries.FAMILY_STOCH_REV_SELL, df)
        polarity = events.event_polarity_series(events.EVENT_STOCH_REVERSAL, df)
        fires = polarity.isin(["BUY", "SELL"])
        assert fires.sum() > 0
        assert (series[fires] == entries.SELL).all()
        assert (series[~fires] == "").all()

    def test_both_polarities_fire_the_same_side(self):
        """The theory-free contract: a BUY-polarity cross (indicator hints
        up) and a SELL-polarity cross both open the SAME SELL ledger."""
        for family, ev in ((entries.FAMILY_RSI_REV_SELL, events.EVENT_RSI_REVERSAL),
                           (entries.FAMILY_STOCH_REV_SELL, events.EVENT_STOCH_REVERSAL)):
            rng = np.random.RandomState(11)
            df = make_m5(4000.0 + np.cumsum(rng.normal(0, 6.0, 600)),
                     end=_now() - pd.Timedelta(minutes=5))
            series = entries.family_signal_series(family, df)
            polarity = events.event_polarity_series(ev, df)
            for hint in ("BUY", "SELL"):
                assert (series[polarity == hint] == entries.SELL).all(), \
                    f"{family}: {hint}-polarity events must also fire SELL"

    def test_fire_indices_match_the_bracket_machinery(self):
        rng = np.random.RandomState(5)
        df = make_m5(4000.0 + np.cumsum(rng.normal(0, 5.0, 500)),
                     end=_now() - pd.Timedelta(minutes=5))
        for family, ev in ((entries.FAMILY_RSI_REV_SELL, events.EVENT_RSI_REVERSAL),
                           (entries.FAMILY_STOCH_REV_SELL, events.EVENT_STOCH_REVERSAL)):
            series = entries.family_signal_series(family, df)
            polarity = events.event_polarity_series(ev, df)
            want = [i for i in range(len(df))
                    if str(polarity.iloc[i]) in ("BUY", "SELL")]
            got = [i for i in range(len(df)) if series.iloc[i] == entries.SELL]
            assert got == want, family

    def test_no_event_bar_is_silent(self):
        df = flat_history()
        for family in (entries.FAMILY_RSI_REV_SELL, entries.FAMILY_STOCH_REV_SELL):
            assert entries.signal_on_last_bar(df, family) is None


# ── 2. gate causality — closed H4 buckets only ─────────────────────────────


class TestGateCausality:
    def test_flat_history_gates_flat(self):
        assert gates.gate_state_on_last_bar(flat_history(), "h4_ema") == STATE_FLAT

    def test_rising_history_gates_up(self):
        assert gates.gate_state_on_last_bar(rising_history(), "h4_ema") == STATE_UP

    def test_forming_bucket_is_invisible(self):
        """Causal lock: the H4 bucket still forming at the signal moment
        cannot move the gate — 10 steep bars inside a new bucket on top of
        a flat history still read FLAT (closed buckets only).

        Bucket-aware anchor (2026-11-17, คุณหมอผ่าน Hermes): the spike used
        to be appended straight at `_now()`, so whenever the run happened
        within the 10-bar span (~50 min) of an H4 boundary the spike CLOSED
        its own bucket and the gate read UP (proven red at minute 195 of
        the bucket on a clean tree — fail window = minute ≥ 190). The base
        is extended flat to the next boundary first (see _anchored_base)
        so the spike always lands inside the forming bucket."""
        base = _anchored_base(flat_history(n_buckets=54), STATE_FLAT, 10)
        last_close = float(base["close"].iloc[-1])
        spike = [last_close + 30.0 * (k + 1) for k in range(10)]  # forming, steep
        frame = pd.concat([base, make_m5(spike, start=base.index[-1] + pd.Timedelta(minutes=5))])
        assert gates.gate_state_on_last_bar(frame, "h4_ema") == STATE_FLAT

    def test_bucket_closing_exactly_now_becomes_visible(self):
        """The same steep bucket, once FULLY closed by the last M5 bar's
        close time, IS the state (alignment contract of align_state_to_m5:
        a bucket closing exactly at the query time is available)."""
        df = flat_history(n_buckets=54)
        spike = [4000.0 + 30.0 * (k + 1) for k in range(48)]  # one full bucket
        frame = pd.concat([df, make_m5(spike, start=df.index[-1] + pd.Timedelta(minutes=5))])
        state = gates.gate_state_on_last_bar(frame, "h4_ema")
        assert state == STATE_UP

    def test_warmup_returns_none(self):
        """Inside the detector's 50-bucket warmup the state is unknown —
        never satisfies any gate (the live path treats None as not-FLAT)."""
        assert gates.gate_state_on_last_bar(flat_history(n_buckets=40), "h4_ema") is None

    def test_unknown_detector_raises(self):
        with pytest.raises(ValueError):
            gates.gate_state_on_last_bar(flat_history(), "h1_adx")


# ── 3. live path — gate decides whether the event opens a bet ───────────────


class TestLiveGatePath:
    def _cell(self, cell_id: str) -> tuple[object, object]:
        specs = {s.cell_id: s for s in cells.load_bet_cells()}
        return specs[cell_id], store

    def test_flat_plus_event_opens_the_sell_bet(self, tmp_path, caplog):
        """FLAT + rsi_reversal event → the bet opens: direction SELL (the
        event is BUY-polarity — the hint is ignored, theory-free), the
        champion's contract geometry TP12:SL6, risk = balance × 1.5%."""
        spec, _ = self._cell("BET-RF")
        frame, polarity = frame_with_event_at_last_bar(spec.family, STATE_FLAT)
        assert polarity == "BUY"          # indicator hints UP — still a SELL bet
        conn = store.connect(tmp_path / "bet.db")
        store.seed_cells(conn, [spec])
        feed = _FakeFeed(mid=4000.0)
        with caplog.at_level("INFO", logger="bet_engine"):
            bet_engine.try_open_bet(conn, spec, frame, feed, {})
        row = store.get_open_bet(conn, "BET-RF")
        assert row is not None
        assert row["direction"] == "SELL"
        assert row["entry_price"] == 4000.0
        assert row["tp_price"] == pytest.approx(4000.0 - 12.0)   # TP12
        assert row["sl_price"] == pytest.approx(4000.0 + 6.0)    # SL6
        assert row["risk_amount"] == pytest.approx(1000.0 * 0.015)
        assert row["signal_ts"] == frame.index[-1].isoformat()
        conn.close()

    def test_up_state_skips_the_event(self, tmp_path, caplog):
        """Same event, gate reads UP → no bet, and the skip is logged with
        the required-vs-got state (the audit trail Hermes reads from logs)."""
        spec, _ = self._cell("BET-RF")
        frame, _ = frame_with_event_at_last_bar(spec.family, STATE_UP)
        assert gates.gate_state_on_last_bar(frame, "h4_ema") == STATE_UP
        assert entries.signal_on_last_bar(frame, spec.family) is not None

        conn = store.connect(tmp_path / "bet.db")
        store.seed_cells(conn, [spec])
        with caplog.at_level("INFO", logger="bet_engine"):
            bet_engine.try_open_bet(conn, spec, frame, _FakeFeed(), {})
        assert store.get_open_bet(conn, "BET-RF") is None
        skip = [r.message for r in caplog.records if "skipped" in r.message]
        assert skip and "h4_ema=FLAT required" in skip[0], caplog.text
        conn.close()

    def test_flat_without_event_opens_nothing(self, tmp_path):
        spec, _ = self._cell("BET-RF")
        frame = flat_history()   # FLAT gate, but no event fires
        conn = store.connect(tmp_path / "bet.db")
        store.seed_cells(conn, [spec])
        bet_engine.try_open_bet(conn, spec, frame, _FakeFeed(), {})
        assert store.get_open_bet(conn, "BET-RF") is None
        conn.close()

    def test_sf_contract_geometry_from_yml(self, tmp_path):
        """BET-SF opens with TP12:SL12 — the champion's geometry, not a
        neighbour's (the cell axis must not drift)."""
        spec, _ = self._cell("BET-SF")
        frame, _ = frame_with_event_at_last_bar(spec.family, STATE_FLAT)
        conn = store.connect(tmp_path / "bet.db")
        store.seed_cells(conn, [spec])
        bet_engine.try_open_bet(conn, spec, frame, _FakeFeed(mid=4000.0), {})
        row = store.get_open_bet(conn, "BET-SF")
        assert row is not None
        assert row["tp_price"] == pytest.approx(4000.0 - 12.0)
        assert row["sl_price"] == pytest.approx(4000.0 + 12.0)
        conn.close()

    def test_gate_state_transition_logged_once(self, caplog):
        spec, _ = self._cell("BET-RF")
        frame = flat_history()
        last_state: dict = {}
        with caplog.at_level("INFO", logger="bet_engine"):
            bet_engine._log_gate_transition("BET-RF", spec.gate, STATE_FLAT, last_state)
            bet_engine._log_gate_transition("BET-RF", spec.gate, STATE_FLAT, last_state)
            bet_engine._log_gate_transition("BET-RF", spec.gate, STATE_UP, last_state)
        msgs = [r.message for r in caplog.records if "gate" in r.message]
        assert len(msgs) == 2                     # only on CHANGE, not per cycle
        assert "FLAT (armed)" in msgs[0]
        assert "UP" in msgs[1]


# ── 3b. the frame anchor must be bucket-aware, never wall-clock lucky ─────


class TestTailAnchorBucketAware:
    """2026-11-17 flake: frame_with_event_at_last_bar depended on WALL CLOCK.

    The gate is frame-relative (a bucket becomes visible once its close time
    is ≤ the last bar's close), so a decline tail that spills across an H4
    boundary CLOSES the decline bucket — the gate reads DOWN instead of the
    base's FLAT and the bet never opens. That happened whenever the test ran
    within the tail span (~105 min) of a 4-hour boundary: proven on a CLEAN
    HEAD worktree (zero local changes) — red at 02:40 UTC, green at 02:05 UTC,
    i.e. this test file was red ~44% of the day by construction.

    These tests pin BOTH wall-clock positions (safe + crossing) so the
    helper is deterministic around the whole 4-hour cycle.
    """

    _SAFE_MINUTE = 10        # deep inside a bucket — always worked
    _CROSSING_MINUTE = 200   # the old fail window: boundary inside the tail

    def _cell(self, cell_id: str) -> tuple[object, object]:
        specs = {s.cell_id: s for s in cells.load_bet_cells()}
        return specs[cell_id], store

    def _fake_now(self, minute_into_bucket: int,
                  sec: int = 0) -> pd.Timestamp:
        return pd.Timestamp("2026-11-17 04:00") + pd.Timedelta(
            minutes=minute_into_bucket, seconds=sec)

    @pytest.mark.parametrize("minute,sec", [
        (10, 0), (200, 0),          # the historical green/red pair
        (200, 27), (193, 51),       # off-grid seconds (cross-context review
    ])                              # catch 2026-11-17: floor lost a bar)
    def test_flat_gate_opens_bet_at_any_clock(self, monkeypatch, tmp_path,
                                              minute, sec):
        fake_now = self._fake_now(minute, sec)
        monkeypatch.setattr(sys.modules[__name__], "_now", lambda: fake_now)
        # Third wall-clock leak of this family (2026-11-17): pinning the
        # FRAME clock left the ENGINE's clock live. try_open_bet's stale-
        # feed guard reads bet_engine._now_utc() (datetime.now, line ~353)
        # — against REAL time the fixed frame (ends ~05:50 fake) went
        # stale at 06:25 UTC real and the bet stopped opening, PERMANENTLY
        # (the fake date is pinned, so this was not a window flake but a
        # time bomb; proven red on a clean HEAD via git stash at 08:02
        # UTC). Patch the engine to the SAME fake now so the guard sees a
        # fresh frame at every parametrize point (age is negative — the
        # frame extends past fake now by design).
        monkeypatch.setattr(bet_engine, "_now_utc",
                            lambda: fake_now.to_pydatetime())
        spec, _ = self._cell("BET-SF")
        frame, pol = frame_with_event_at_last_bar(spec.family, STATE_FLAT)
        assert gates.gate_state_on_last_bar(frame, "h4_ema") == STATE_FLAT, (
            f"minute {minute}:{sec:02d}: the gate must read the base's FLAT, "
            "not the tail"
        )
        assert pol == "BUY"
        conn = store.connect(tmp_path / "bet.db")
        store.seed_cells(conn, [spec])
        bet_engine.try_open_bet(conn, spec, frame, _FakeFeed(mid=4000.0), {})
        row = store.get_open_bet(conn, "BET-SF")
        conn.close()
        assert row is not None, (
            f"wall clock (minute {minute}:{sec:02d} of the H4 bucket) changed "
            "whether the bet opened — the tail anchor is not bucket-aware"
        )

    @pytest.mark.parametrize("minute,sec", [
        (10, 0), (200, 0), (200, 27), (193, 51),
    ])
    def test_up_gate_state_at_any_clock(self, monkeypatch, minute, sec):
        fake_now = self._fake_now(minute, sec)
        monkeypatch.setattr(sys.modules[__name__], "_now", lambda: fake_now)
        spec, _ = self._cell("BET-RF")
        frame, _ = frame_with_event_at_last_bar(spec.family, STATE_UP)
        assert gates.gate_state_on_last_bar(frame, "h4_ema") == STATE_UP, (
            f"minute {minute}:{sec:02d}: UP base must read UP regardless of "
            "wall clock"
        )

    # The forming-bucket scenario has a 10-bar spike tail (50 min). Its
    # un-anchored fail window is minute ≥ 190; on the FIRST anchored version
    # (floor instead of ceil) it stayed red at 36 of 50 sampled minutes
    # (sec=0) and at EVERY off-grid second, because pd.date_range(end=...)
    # does not snap the frame to the 5-min grid — so the parametrization
    # pins minutes AND seconds, including the previously-red ones.
    _FORMING_POINTS = [
        (0, 0), (60, 0), (120, 0), (185, 0), (189, 0), (190, 0),
        (191, 0), (193, 0), (195, 0), (197, 0), (200, 0), (210, 0),
        (221, 0), (233, 0), (235, 0), (236, 0), (238, 0), (239, 0),
        (190, 27), (195, 27), (200, 51), (221, 13), (235, 27), (239, 59),
    ]

    @pytest.mark.parametrize("minute,sec", _FORMING_POINTS)
    def test_forming_bucket_invisible_at_any_clock(self, monkeypatch, minute, sec):
        """2026-11-17 flake #2 (คุณหมอผ่าน Hermes 17/11): the anchor fix
        for the family tails (above) left this scenario un-anchored —
        Hermes saw test_forming_bucket_is_invisible fail at 03:14 UTC on
        BOTH f0a640d and a clean bb5be64 worktree (46 min before the 04:00
        boundary). RED proof on the old construction: gate read UP at
        every minute ≥ 190. The anchored construction must read FLAT at
        EVERY offset of the 4-hour cycle, grid-aligned seconds or not —
        no wall-clock luck (the first ceil-fix review caught that the
        sampled minutes happened to sit exactly on the floor==ceil grid).
        """
        fake_now = self._fake_now(minute, sec)
        monkeypatch.setattr(sys.modules[__name__], "_now", lambda: fake_now)
        base = _anchored_base(flat_history(n_buckets=54), STATE_FLAT, 10)
        last_close = float(base["close"].iloc[-1])
        spike = [last_close + 30.0 * (k + 1) for k in range(10)]
        frame = pd.concat([base, make_m5(
            spike, start=base.index[-1] + pd.Timedelta(minutes=5))])
        assert gates.gate_state_on_last_bar(frame, "h4_ema") == STATE_FLAT, (
            f"minute {minute}:{sec:02d}: the 10-bar spike must stay inside "
            "the forming bucket at every wall-clock offset (fail window of "
            "the un-anchored construction was minute >= 190)"
        )


# ── 4. config errors fail closed at boot, not silently mid-run ─────────────


class TestGateConfigErrors:
    def test_half_gate_is_rejected(self):
        with pytest.raises(ValueError):
            cells._spec_from_env("BET-X1", {
                "BET_FAMILY": "R", "BET_TP_DIST": "12.0", "BET_SL_DIST": "6.0",
                "BET_GATE_DETECTOR": "h4_ema",
            })

    def test_unknown_detector_is_rejected(self):
        with pytest.raises(ValueError):
            cells._spec_from_env("BET-X1", {
                "BET_FAMILY": "R", "BET_TP_DIST": "12.0", "BET_SL_DIST": "6.0",
                "BET_GATE_DETECTOR": "h1_adx", "BET_GATE_STATE": "FLAT",
            })

    def test_unknown_state_is_rejected(self):
        with pytest.raises(ValueError):
            cells._spec_from_env("BET-X1", {
                "BET_FAMILY": "R", "BET_TP_DIST": "12.0", "BET_SL_DIST": "6.0",
                "BET_GATE_DETECTOR": "h4_ema", "BET_GATE_STATE": "SIDEWAYS",
            })

    def test_env_parser_rejects_half_gate_too(self, monkeypatch):
        monkeypatch.setenv("BET_GATE_DETECTOR_BET_X1", "h4_ema")
        monkeypatch.delenv("BET_GATE_STATE_BET_X1", raising=False)
        with pytest.raises(ValueError):
            bet_engine.cell_spec_from_env("BET-X1")
