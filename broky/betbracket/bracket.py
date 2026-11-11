"""Bracket machinery: two-ledger, tick-resolved, theory-free bets.

At every event bar the bracket opens TWO bets with identical geometry —
one BUY, one SELL — and resolves each against the real tick stream
(first tick at/after the contract level touches, strict ordering).

Resolution semantics are the live farm's contract (broky/bet/contract.py
BetResolver.step_tick): mid = (bid+ask)/2, levels fixed at entry, entry
tick itself cannot resolve the contract, ticks at-or-before the entry
tick are ignored. This module reimplements that walk vectorised
(expanding windows + argmax) because the deep grid runs ~10 months of
ticks per event; tests/test_betbracket_bracket.py proves the two engines
agree tick-for-tick on random walks (differential equivalence).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import numpy as np

from broky.bet.contract import (
    DEFAULT_SPREAD_COST_PER_OZ,
    BetGeometry,
    BUY,
    SELL,
    OUTCOME_TP,
    OUTCOME_SL,
)

SIDE_BUY = BUY
SIDE_SELL = SELL

PAIR_BUY_WIN = "buy_win"        # BUY TP + SELL SL
PAIR_SELL_WIN = "sell_win"      # SELL TP + BUY SL
PAIR_BOTH_WIN = "both_win"      # zigzag — possible when tp < sl
PAIR_BOTH_LOSE = "both_lose"    # whipsaw — possible when tp > sl
PAIR_OPEN = "open"              # one or both sides still unresolved

# Expanding-window scan schedule (in ticks) for the first-passage search.
_WINDOW_STEPS = (200_000, 1_000_000, 5_000_000, 20_000_000, 400_000_000)


@dataclass(frozen=True)
class SideResult:
    """One ledger's resolved contract."""
    side: str
    outcome: Optional[str]        # OUTCOME_TP / OUTCOME_SL / None (open)
    exit_msc: Optional[int]
    seconds_to_finish: Optional[float]
    pnl_usd: Optional[float]      # risk-normalised to risk_amount (see grid)


@dataclass(frozen=True)
class BracketResult:
    """Both ledgers of one event, one geometry."""
    event_type: str
    bar_index: int
    signal_msc: int               # close time of the firing bar
    entry_msc: int
    entry_price: float            # mid at entry tick
    buy: SideResult
    sell: SideResult

    @property
    def pair_class(self) -> str:
        if self.buy.outcome is None or self.sell.outcome is None:
            return PAIR_OPEN
        buy_win = self.buy.outcome == OUTCOME_TP
        sell_win = self.sell.outcome == OUTCOME_TP
        if buy_win and sell_win:
            return PAIR_BOTH_WIN
        if buy_win:
            return PAIR_BUY_WIN
        if sell_win:
            return PAIR_SELL_WIN
        return PAIR_BOTH_LOSE


def _first_pass(mid_tail: np.ndarray, level: float, above: bool,
                start: int, total: int) -> int:
    """Index (relative to `mid_tail`) of the first tick beyond `level`.

    `above=True` searches mid >= level; `above=False` searches mid <= level.
    Expanding windows keep the typical case (resolves in minutes) cheap
    while guaranteeing correctness over arbitrarily long tails. Returns
    -1 when the data ends before the level is touched.
    """
    limit = total - start
    for step in _WINDOW_STEPS:
        n = min(limit, step)
        if n <= 0:
            return -1
        seg = mid_tail[start:start + n]
        hits = np.flatnonzero(seg >= level) if above else np.flatnonzero(seg <= level)
        if hits.size:
            return int(hits[0])
        if n == limit:
            return -1
    return -1


def _side_pnl(outcome: Optional[str], geometry: BetGeometry,
              risk_amount: float, spread_per_oz: float) -> Optional[float]:
    if outcome is None:
        return None
    qty = risk_amount / geometry.sl_dist
    spread_cost = qty * spread_per_oz
    if outcome == OUTCOME_TP:
        return geometry.tp_dist * qty - spread_cost
    return -geometry.sl_dist * qty - spread_cost


def resolve_bracket(
    event_type: str,
    bar_index: int,
    signal_msc: int,
    msc: np.ndarray,
    mid: np.ndarray,
    geometry: BetGeometry,
    risk_amount: float = 100.0,
    spread_per_oz: float = DEFAULT_SPREAD_COST_PER_OZ,
) -> Optional[BracketResult]:
    """Resolve one event's BUY+SELL pair against the tick stream.

    Entry = first tick at/after the firing bar's close (`signal_msc`).
    Returns None when no tick exists at/after the close (signal in a
    halt with no subsequent data, or the last bars of the series).
    """
    entry_idx = int(np.searchsorted(msc, signal_msc, side="left"))
    if entry_idx >= len(msc):
        return None
    entry_price = float(mid[entry_idx])

    def side(direction: str) -> SideResult:
        if direction == SIDE_BUY:
            tp_price = entry_price + geometry.tp_dist
            sl_price = entry_price - geometry.sl_dist
            tp_hit = _first_pass(mid, tp_price, True, entry_idx + 1, len(mid))
            sl_hit = _first_pass(mid, sl_price, False, entry_idx + 1, len(mid))
        else:
            tp_price = entry_price - geometry.tp_dist
            sl_price = entry_price + geometry.sl_dist
            tp_hit = _first_pass(mid, tp_price, False, entry_idx + 1, len(mid))
            sl_hit = _first_pass(mid, sl_price, True, entry_idx + 1, len(mid))

        if tp_hit == -1 and sl_hit == -1:
            return SideResult(direction, None, None, None, None)
        if sl_hit == -1 or (tp_hit != -1 and tp_hit < sl_hit):
            rel, outcome = tp_hit, OUTCOME_TP
        else:
            rel, outcome = sl_hit, OUTCOME_SL
        exit_abs = entry_idx + 1 + rel
        exit_msc = int(msc[exit_abs])
        return SideResult(
            side=direction,
            outcome=outcome,
            exit_msc=exit_msc,
            seconds_to_finish=(exit_msc - int(msc[entry_idx])) / 1000.0,
            pnl_usd=_side_pnl(outcome, geometry, risk_amount, spread_per_oz),
        )

    return BracketResult(
        event_type=event_type,
        bar_index=bar_index,
        signal_msc=signal_msc,
        entry_msc=int(msc[entry_idx]),
        entry_price=entry_price,
        buy=side(SIDE_BUY),
        sell=side(SIDE_SELL),
    )


def max_streak(outcomes: list[Optional[str]], target: str) -> int:
    """Longest run of `target` outcomes in resolution order."""
    best = cur = 0
    for o in outcomes:
        cur = cur + 1 if o == target else 0
        best = max(best, cur)
    return best


def quarter_of_msc(msc: int) -> str:
    """Calendar-quarter key ('2026-Q1') of a millisecond timestamp."""
    dt = datetime.fromtimestamp(msc / 1000.0, tz=timezone.utc)
    return f"{dt.year}-Q{(dt.month - 1) // 3 + 1}"


# ── Bar-based resolution (two-edge: pes = SL-first, opt = TP-first) ─────────
#
# The deep grid's fallback mode when ticks have not finished downloading:
# resolve on closed M5 bars exactly like the farm backtest — entry at the
# open of the bar AFTER the signal bar, the entry bar's own high/low is the
# first observation, and a bar touching BOTH levels is ambiguous:
# pes books SL (conservative), opt books TP (optimistic). Differential
# tests prove equivalence with broky.bet.contract.resolve_bet_on_bars
# under POLICY_SL_FIRST / POLICY_TP_FIRST.


@dataclass(frozen=True)
class BarSideResult:
    """One ledger's bar-resolved contract, both ambiguity edges."""
    side: str
    outcome_pes: Optional[str]   # SL-first booking on ambiguous bars
    outcome_opt: Optional[str]   # TP-first booking on ambiguous bars
    bars_to_finish: int          # entry bar = 1 (0 = unresolved)
    ambiguous: bool              # some bar touched both levels
    pnl_pes_usd: Optional[float]
    pnl_opt_usd: Optional[float]


@dataclass(frozen=True)
class BarBracketResult:
    event_type: str
    bar_index: int               # signal bar (closed)
    entry_index: int             # bar after the signal bar (fill at open)
    entry_price: float
    buy: BarSideResult
    sell: BarSideResult

    def pair_class(self, edge: str) -> str:
        """Pair outcome under one ambiguity edge ('pes' | 'opt')."""
        buy_out = self.buy.outcome_pes if edge == "pes" else self.buy.outcome_opt
        sell_out = self.sell.outcome_pes if edge == "pes" else self.sell.outcome_opt
        if buy_out is None or sell_out is None:
            return PAIR_OPEN
        buy_win = buy_out == OUTCOME_TP
        sell_win = sell_out == OUTCOME_TP
        if buy_win and sell_win:
            return PAIR_BOTH_WIN
        if buy_win:
            return PAIR_BUY_WIN
        if sell_win:
            return PAIR_SELL_WIN
        return PAIR_BOTH_LOSE


def _bar_side(direction: str, entry_price: float, geometry: BetGeometry,
              high: np.ndarray, low: np.ndarray, start: int,
              risk_amount: float, spread_per_oz: float) -> BarSideResult:
    """First-passage over bars [start:] for one side, both edges."""
    if direction == SIDE_BUY:
        tp_level, sl_level = entry_price + geometry.tp_dist, entry_price - geometry.sl_dist
        tp_rel = _first_pass(high, tp_level, True, start, len(high))
        sl_rel = _first_pass(low, sl_level, False, start, len(low))
    else:
        tp_level, sl_level = entry_price - geometry.tp_dist, entry_price + geometry.sl_dist
        tp_rel = _first_pass(low, tp_level, False, start, len(low))
        sl_rel = _first_pass(high, sl_level, True, start, len(high))

    if tp_rel == -1 and sl_rel == -1:
        return BarSideResult(direction, None, None, 0, False, None, None)
    ambiguous = tp_rel != -1 and sl_rel != -1 and tp_rel == sl_rel
    if sl_rel == -1 or (tp_rel != -1 and tp_rel < sl_rel):
        outcome, rel = OUTCOME_TP, tp_rel
    elif tp_rel == -1 or sl_rel < tp_rel:
        outcome, rel = OUTCOME_SL, sl_rel
    else:                                   # same bar, ambiguous
        rel = tp_rel
        return BarSideResult(
            side=direction,
            outcome_pes=OUTCOME_SL, outcome_opt=OUTCOME_TP,
            bars_to_finish=rel + 1, ambiguous=True,
            pnl_pes_usd=_side_pnl(OUTCOME_SL, geometry, risk_amount, spread_per_oz),
            pnl_opt_usd=_side_pnl(OUTCOME_TP, geometry, risk_amount, spread_per_oz),
        )
    return BarSideResult(
        side=direction,
        outcome_pes=outcome, outcome_opt=outcome,
        bars_to_finish=rel + 1, ambiguous=False,
        pnl_pes_usd=_side_pnl(outcome, geometry, risk_amount, spread_per_oz),
        pnl_opt_usd=_side_pnl(outcome, geometry, risk_amount, spread_per_oz),
    )


def resolve_bracket_on_bars(
    event_type: str,
    bar_index: int,
    high: np.ndarray,
    low: np.ndarray,
    open_: np.ndarray,
    geometry: BetGeometry,
    risk_amount: float = 100.0,
    spread_per_oz: float = DEFAULT_SPREAD_COST_PER_OZ,
) -> Optional[BarBracketResult]:
    """Resolve one event's BUY+SELL pair on bars (two-edge).

    Entry = open of the bar after the signal bar — no-lookdown, identical
    to the farm backtest. Returns None when the signal bar is the last
    bar (no next bar to fill at).
    """
    entry_idx = bar_index + 1
    if entry_idx >= len(open_):
        return None
    entry_price = float(open_[entry_idx])
    return BarBracketResult(
        event_type=event_type,
        bar_index=bar_index,
        entry_index=entry_idx,
        entry_price=entry_price,
        buy=_bar_side(SIDE_BUY, entry_price, geometry, high, low, entry_idx,
                       risk_amount, spread_per_oz),
        sell=_bar_side(SIDE_SELL, entry_price, geometry, high, low, entry_idx,
                       risk_amount, spread_per_oz),
    )
