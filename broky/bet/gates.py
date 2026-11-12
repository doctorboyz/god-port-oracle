"""Conditional-cell gate — trend state check on CLOSED HTF bars only.

The two conditional cells (BET-RF / BET-SF, คุณหมอ approved 2026-11-13)
may only fire when the h4_ema detector says FLAT at the signal moment.
This module is the single place that answers "what is the gate state
right now?" for the live engine — it deliberately reuses the causal
betbracket machinery (``resample_htf`` + ``state_h4_ema`` +
``align_state_to_m5``, locked by tests/test_betbracket_htf.py) instead
of reimplementing the H4 resample/slope; the no-lookahead guarantee is
therefore inherited, not re-earned.

Alignment contract (identical to the conditional grid that picked the
champions): the state tagged to a bet whose signal bar is the last
closed M5 bar is the state known at that bar's close — i.e. the state of
the last FULLY closed H4 bucket. The H4 bucket currently forming is
invisible by construction (``align_state_to_m5`` searches only closed
buckets; mutating the forming bucket cannot move the answer — asserted
by tests in tests/test_bet_conditional_cells.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from broky.betbracket.htf import (
    H4_FREQ, STATE_DOWN, STATE_FLAT, STATE_UP, align_state_to_m5,
    resample_htf, state_h4_ema,
)

# The only detector the conditional champions were measured on (h4_ema,
# T1 H4-EMA50 slope ±2bp). A gate naming anything else is a config error.
GATE_DETECTORS = ("h4_ema",)
GATE_STATES = (STATE_UP, STATE_DOWN, STATE_FLAT)


@dataclass(frozen=True)
class GateSpec:
    """A cell's entry condition on trend state (e.g. h4_ema = FLAT).

    Carried in CellSpec so the yml loader, the env parser and the live
    engine all see the same gate; None (the default) means the cell is
    unconditional — the original 9 cells, untouched.
    """

    detector: str          # one of GATE_DETECTORS ("h4_ema")
    state: str             # one of GATE_STATES (UP/DOWN/FLAT)



def gate_state_on_last_bar(m5: pd.DataFrame,
                           detector: str = "h4_ema") -> Optional[str]:
    """Gate state known at the last closed M5 bar's close, or None.

    `m5` is the closed-M5 frame exactly as the live engine loads it
    (DatetimeIndex + open/high/low/close; volume optional — synthesized
    as 0 when absent because the resampler aggregates it). Returns
    UP/DOWN/FLAT from the last fully closed H4 bucket, or None while
    the H4 history is inside the detector's warmup window (first 50 H4
    bars carry no state) — the caller treats None as "gate state
    unknown", which never satisfies any gate.
    """
    if detector not in GATE_DETECTORS:
        raise ValueError(
            f"unknown gate detector {detector!r} — expected one of "
            f"{GATE_DETECTORS} (the champions were only ever measured "
            f"on h4_ema)"
        )
    if m5 is None or m5.empty:
        return None
    frame = m5.reset_index(names="time")
    if "volume" not in frame.columns:
        frame["volume"] = 0.0
    h4 = resample_htf(frame, H4_FREQ)
    states = state_h4_ema(h4)
    aligned = align_state_to_m5(frame, h4, states, H4_FREQ)
    return aligned[-1]
