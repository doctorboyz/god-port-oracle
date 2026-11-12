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
    """
    if want_state == STATE_FLAT:
        base = flat_history(54)
    elif want_state == STATE_UP:
        base = rising_history(56)
    else:
        raise ValueError(want_state)
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
        a flat history still read FLAT (closed buckets only)."""
        df = flat_history(n_buckets=54)
        spike = [4000.0 + 30.0 * (k + 1) for k in range(10)]  # forming, steep
        frame = pd.concat([df, make_m5(spike, start=df.index[-1] + pd.Timedelta(minutes=5))])
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
