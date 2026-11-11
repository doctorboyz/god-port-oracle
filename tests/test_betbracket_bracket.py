"""Bracket machinery tests — tick ordering + the 4 pair outcomes.

The vectorised first-passage engine must agree tick-for-tick with the
farm's canonical BetResolver.step_tick contract (differential test on
random walks) — same levels, same spread booking, same post-entry-tick
start. The farm module itself is imported, never modified.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pytest

from broky.bet.contract import BetGeometry, BetResolver, open_bet
from broky.betbracket import (
    PAIR_BOTH_LOSE,
    PAIR_BOTH_WIN,
    PAIR_BUY_WIN,
    PAIR_OPEN,
    PAIR_SELL_WIN,
    max_streak,
    quarter_of_msc,
    resolve_bracket,
)

START_S = 1_700_000_000
GEO_ZIGZAG = BetGeometry(tp_dist=6.0, sl_dist=12.0)   # tp < sl
GEO_WHIPSAW = BetGeometry(tp_dist=12.0, sl_dist=6.0)  # tp > sl
ENTRY = 4000.0
SPREAD = 0.20


def msc_path(n: int, step_ms: int = 1000) -> np.ndarray:
    return (START_S * 1000 + np.arange(n) * step_ms).astype(np.int64)


def run(mid_path, geo, signal_offset_ms=0):
    """Resolve a bracket from a mid-price path (1 tick per price)."""
    mid = np.asarray(mid_path, dtype=float)
    msc = msc_path(len(mid))
    return resolve_bracket("test", 0, START_S * 1000 + signal_offset_ms,
                           msc, mid, geo, risk_amount=100.0, spread_per_oz=SPREAD)


def test_pair_buy_win_up_move():
    """Monotonic up: BUY TP first, SELL SL later -> buy_win."""
    path = [ENTRY, ENTRY + 1, ENTRY + 3, ENTRY + 6, ENTRY + 9, ENTRY + 12]
    res = run(path, GEO_ZIGZAG)
    assert res.buy.outcome == "tp"
    assert res.sell.outcome == "sl"
    assert res.pair_class == PAIR_BUY_WIN
    # BUY resolved on the first tick at/after +6 (index 3)
    assert res.buy.exit_msc == (START_S + 3) * 1000
    # SELL resolved when the path reached +12 (index 5)
    assert res.sell.exit_msc == (START_S + 5) * 1000


def test_pair_sell_win_down_move():
    """Monotonic down: SELL TP first, BUY SL later -> sell_win."""
    path = [ENTRY, ENTRY - 1, ENTRY - 3, ENTRY - 6, ENTRY - 9, ENTRY - 12]
    res = run(path, GEO_ZIGZAG)
    assert res.sell.outcome == "tp"
    assert res.buy.outcome == "sl"
    assert res.pair_class == PAIR_SELL_WIN


def test_pair_both_win_zigzag_tp_lt_sl():
    """Up +6 (BUY TP) then down -6 (SELL TP), never +-12 -> both_win."""
    path = [ENTRY, ENTRY + 2, ENTRY + 6, ENTRY + 2, ENTRY - 2, ENTRY - 6]
    res = run(path, GEO_ZIGZAG)
    assert res.buy.outcome == "tp"
    assert res.sell.outcome == "tp"
    assert res.pair_class == PAIR_BOTH_WIN


def test_pair_both_lose_whipsaw_tp_gt_sl():
    """Down -6 (BUY SL) then up +6 (SELL SL), never +-12 -> both_lose."""
    path = [ENTRY, ENTRY - 2, ENTRY - 6, ENTRY - 2, ENTRY + 2, ENTRY + 6]
    res = run(path, GEO_WHIPSAW)
    assert res.buy.outcome == "sl"
    assert res.sell.outcome == "sl"
    assert res.pair_class == PAIR_BOTH_LOSE


def test_tick_order_decides_outcome():
    """Both levels eventually touched — the EARLIER tick wins the ledger."""
    # BUY: SL (-12) at tick 1, TP (+6) at tick 2 → SL is the answer
    path = [ENTRY, ENTRY - 12, ENTRY + 6, ENTRY + 13]
    res = run(path, GEO_ZIGZAG)
    assert res.buy.outcome == "sl"
    assert res.buy.exit_msc == (START_S + 1) * 1000
    # SELL: TP (-6) is passed through at tick 1, SL (+12) only at tick 3
    assert res.sell.outcome == "tp"
    assert res.sell.exit_msc == (START_S + 1) * 1000


def test_entry_is_first_tick_at_or_after_close():
    """Signal close falls between ticks — entry anchors to the NEXT tick."""
    # signal at START_S + 500ms: entry = tick 1 (mid 4001), not tick 0
    path = [ENTRY, ENTRY + 1, ENTRY + 7]
    res = run(path, GEO_ZIGZAG, signal_offset_ms=500)
    assert res.entry_msc == (START_S + 1) * 1000
    assert res.entry_price == ENTRY + 1
    # BUY TP = 4007 touched exactly on the last tick
    assert res.buy.outcome == "tp"
    assert res.buy.exit_msc == (START_S + 2) * 1000


def test_no_tick_after_signal_returns_none():
    """Signal close after the last tick → no bracket possible."""
    mid = np.array([ENTRY, ENTRY + 1], dtype=float)
    msc = msc_path(2)
    res = resolve_bracket("t", 0, (START_S + 5) * 1000, msc, mid, GEO_ZIGZAG)
    assert res is None


def test_unresolved_is_pair_open():
    """Data ends before any level → both sides None, pair open."""
    path = [ENTRY, ENTRY + 0.5, ENTRY + 1.0]
    res = run(path, GEO_ZIGZAG)
    assert res.buy.outcome is None and res.sell.outcome is None
    assert res.pair_class == PAIR_OPEN


def test_pnl_matches_farm_contract_semantics():
    """Win pnl = tp*qty - spread*qty with qty = risk/sl — farm formula."""
    path = [ENTRY, ENTRY + 6, ENTRY + 7]
    res = run(path, GEO_ZIGZAG)
    qty = 100.0 / GEO_ZIGZAG.sl_dist
    assert res.buy.pnl_usd == pytest.approx(GEO_ZIGZAG.tp_dist * qty - SPREAD * qty)
    loss = run([ENTRY, ENTRY - 12, ENTRY - 13], GEO_ZIGZAG)
    assert loss.buy.pnl_usd == pytest.approx(-GEO_ZIGZAG.sl_dist * qty - SPREAD * qty)


def _farm_resolve(direction, entry_price, geo, msc, mid, entry_idx):
    """The farm's own resolver walked over the same tick slice."""
    entry_ts = datetime.fromtimestamp(msc[entry_idx] / 1000.0, tz=timezone.utc)
    bet = open_bet("diff", direction, signal_ts=entry_ts, entry_ts=entry_ts,
                   entry_price=entry_price, geometry=geo,
                   risk_amount=100.0, spread_cost_per_oz=SPREAD)
    resolver = BetResolver(bet)
    for i in range(entry_idx + 1, len(msc)):
        ts = datetime.fromtimestamp(msc[i] / 1000.0, tz=timezone.utc)
        state = resolver.step_tick(ts, float(mid[i]), int(msc[i]))
        if state.outcome is not None:
            return state
    return None


@pytest.mark.parametrize("seed", range(40))
def test_differential_vs_farm_resolver(seed):
    """Random walks: bracket engine == BetResolver.step_tick tick-for-tick.

    The farm's live contract (broky/bet/contract.py) is the semantic
    reference; the vectorised deep-grid engine must produce the identical
    outcome, exit timestamp and pnl on every walk and both sides.
    """
    rng = np.random.default_rng(seed)
    n = 400
    mid = ENTRY + np.cumsum(rng.normal(0, 2.0, n))
    msc = msc_path(n, step_ms=250)
    geo = GEO_ZIGZAG if seed % 2 == 0 else GEO_WHIPSAW

    res = resolve_bracket("t", 0, START_S * 1000, msc, mid, geo,
                          risk_amount=100.0, spread_per_oz=SPREAD)
    assert res is not None
    entry_idx = int(np.searchsorted(msc, res.entry_msc))

    for side, direction in ((res.buy, "BUY"), (res.sell, "SELL")):
        farm = _farm_resolve(direction, res.entry_price, geo, msc, mid, entry_idx)
        if side.outcome is None:
            assert farm is None, f"seed={seed} {direction}: farm resolved, bracket open"
            continue
        assert farm is not None, f"seed={seed} {direction}: farm open, bracket={side.outcome}"
        assert farm.outcome == side.outcome, (
            f"seed={seed} {direction}: farm={farm.outcome} bracket={side.outcome}")
        assert farm.pnl == pytest.approx(side.pnl_usd), (
            f"seed={seed} {direction}: pnl farm={farm.pnl} bracket={side.pnl_usd}")
        assert farm.exit_ts.timestamp() == pytest.approx(side.exit_msc / 1000.0, abs=0.001)


def test_max_streak():
    outcomes = ["tp", "tp", "sl", None, "tp", "tp", "tp", "sl", "sl"]
    assert max_streak(outcomes, "tp") == 3
    assert max_streak(outcomes, "sl") == 2
    assert max_streak([None, None], "tp") == 0


def test_quarter_of_msc():
    assert quarter_of_msc(1_767_225_600_000) == "2026-Q1"   # 2026-01-01
    assert quarter_of_msc(1_780_272_000_000) == "2026-Q2"   # 2026-06-01
    assert quarter_of_msc(1_759_190_400_000) == "2025-Q3"   # 2025-09-30
    assert quarter_of_msc(1_759_276_800_000) == "2025-Q4"   # 2025-10-01


def test_window_steps_cover_full_tail():
    """The expanding window schedule must reach any tail length we feed it."""
    from broky.betbracket.bracket import _WINDOW_STEPS
    assert _WINDOW_STEPS[-1] >= 400_000_000
    assert _WINDOW_STEPS[0] <= 200_000


# ── Bar-based two-edge resolution (deep-grid mode) ─────────────────────────


def bars_df(high, low, open_):
    import pandas as pd

    idx = pd.date_range("2026-01-01", periods=len(high), freq="5min", tz="UTC")
    return pd.DataFrame({"open": open_, "high": high, "low": low},
                        index=idx)


def run_bars(highs, lows, opens, geo, signal_bar=0):
    from broky.betbracket import resolve_bracket_on_bars

    return resolve_bracket_on_bars(
        "test", signal_bar, np.asarray(highs, float),
        np.asarray(lows, float), np.asarray(opens, float), geo,
        risk_amount=100.0, spread_per_oz=SPREAD)


def test_bar_buy_win_up_move():
    res = run_bars([ENTRY + 1, ENTRY + 6, ENTRY + 9, ENTRY + 12],
                   [ENTRY, ENTRY + 0.5, ENTRY + 5, ENTRY + 8],
                   [ENTRY] * 4, GEO_ZIGZAG)
    assert res.buy.outcome_pes == "tp" and res.buy.outcome_opt == "tp"
    assert res.sell.outcome_pes == "sl" and res.sell.outcome_opt == "sl"
    assert res.buy.bars_to_finish == 1          # entry bar itself resolves at +6
    assert not res.buy.ambiguous


def test_bar_ambiguous_bar_books_sl_pes_tp_opt():
    """One bar spanning both levels: pes books SL, opt books TP."""
    # entry bar (bar 1) spans -12 (SL) and +6 (TP) in a single bar
    res = run_bars([ENTRY + 1, ENTRY + 6], [ENTRY - 1, ENTRY - 12],
                   [ENTRY, ENTRY], GEO_ZIGZAG)
    assert res.buy.ambiguous
    assert res.buy.outcome_pes == "sl"
    assert res.buy.outcome_opt == "tp"
    qty = 100.0 / GEO_ZIGZAG.sl_dist
    assert res.buy.pnl_pes_usd == pytest.approx(-GEO_ZIGZAG.sl_dist * qty - SPREAD * qty)
    assert res.buy.pnl_opt_usd == pytest.approx(GEO_ZIGZAG.tp_dist * qty - SPREAD * qty)


def test_bar_unresolved_when_no_touch():
    res = run_bars([ENTRY + 1, ENTRY + 2], [ENTRY - 1, ENTRY - 0.5],
                   [ENTRY] * 2, GEO_ZIGZAG)
    assert res.buy.outcome_pes is None and res.buy.bars_to_finish == 0
    assert res.pair_class("pes") == PAIR_OPEN


def test_bar_signal_on_last_bar_returns_none():
    res = run_bars([ENTRY + 6], [ENTRY - 1], [ENTRY], GEO_ZIGZAG, signal_bar=0)
    assert res is None       # signal bar is the last bar — no fill bar


@pytest.mark.parametrize("seed", range(30))
def test_bar_differential_vs_farm_resolver(seed):
    """Random-walk bars: two-edge bar engine == farm resolve_bet_on_bars.

    Under POLICY_SL_FIRST the farm's booking must match outcome_pes, and
    under POLICY_TP_FIRST it must match outcome_opt — outcome, exit bar
    timestamp, pnl and the ambiguity flag, for both sides.
    """
    from broky.bet.contract import POLICY_SL_FIRST, POLICY_TP_FIRST, resolve_bet_on_bars

    rng = np.random.default_rng(1000 + seed)
    n = 300
    close = ENTRY + np.cumsum(rng.normal(0, 3.0, n))
    spread_w = np.abs(rng.normal(1.0, 0.5, n))
    high = close + spread_w
    low = close - spread_w
    open_ = close + rng.normal(0, 0.8, n)
    geo = GEO_ZIGZAG if seed % 2 == 0 else GEO_WHIPSAW
    sig = int(rng.integers(0, 5))

    res = run_bars(high, low, open_, geo, signal_bar=sig)
    assert res is not None
    df = bars_df(high, low, open_)
    entry_ts = df.index[res.entry_index]

    for side, direction in ((res.buy, "BUY"), (res.sell, "SELL")):
        bet = open_bet("diff", direction, signal_ts=entry_ts, entry_ts=entry_ts,
                       entry_price=res.entry_price, geometry=geo,
                       risk_amount=100.0, spread_cost_per_oz=SPREAD)
        # farm walk: entry bar IS the first observation (its open is the fill)
        walk = df.iloc[res.entry_index:]
        farm_sl = resolve_bet_on_bars(bet, walk, POLICY_SL_FIRST)
        farm_tp = resolve_bet_on_bars(bet, walk, POLICY_TP_FIRST)
        for edge, farm, outcome, pnl in (
            ("pes", farm_sl, side.outcome_pes, side.pnl_pes_usd),
            ("opt", farm_tp, side.outcome_opt, side.pnl_opt_usd),
        ):
            if outcome is None:
                assert farm.outcome is None, \
                    f"seed={seed} {direction} {edge}: farm={farm.outcome}, bracket open"
                continue
            assert farm.outcome == outcome, (
                f"seed={seed} {direction} {edge}: farm={farm.outcome} bracket={outcome}")
            assert farm.pnl == pytest.approx(pnl), (
                f"seed={seed} {direction} {edge}: pnl farm={farm.pnl} bracket={pnl}")
            assert farm.ambiguous == side.ambiguous
            assert farm.bars_to_finish == side.bars_to_finish
