"""Forward-prediction accuracy of the trend detectors (regime playbook T2).

The conditional grid answers "given the label, how did bets do?". This
module answers the question BEFORE it: when a detector says UP/DOWN/FLAT
at time t, does the price actually go that way over the NEXT 4h / 24h /
72h — measured against calendar time, never against the hindsight truth
used for lag/FP in bet_bracket_conditional.

Determinism + no-lookahead posture:
- The states fed in here are the M5-aligned, causal-tested labels from
  htf.build_detector_states (the forming HTF bucket is invisible).
- The future side is BY DESIGN future data (that is the thing measured);
  the lookup is calendar-time honest: the future close of horizon h is
  the close of the FIRST M5 bar that closes at/after t+h. Weekend/halt
  gaps push the lookup to the next real bar — no synthetic prices, and
  an anchor whose horizon falls past the data end is censored
  (excluded and counted, never imputed).
- All timestamp arithmetic goes through pandas datetime64 arithmetic and
  a single `.astype("int64")` representation per array (the pandas-3
  µs-resolution trap is locked by tests in test_betbracket_forward.py).

FLAT hit definition (theory-free, data-driven): FLAT predicts "no
meaningful direction", so a FLAT anchor is a hit when |forward move| is
at or below eps_h = the median |forward move| over ALL valid anchors at
that horizon (one global threshold per horizon, detector-independent).
By construction the all-anchors reference share is 50% — every FLAT
hit-rate is read against that 50% line.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from broky.betbracket.htf import (
    STATE_DOWN,
    STATE_FLAT,
    STATE_UP,
    build_detector_states,
)

HORIZONS_H: tuple[float, ...] = (4.0, 24.0, 72.0)

# close-TF study (คุณหมอ 13/11): TF ใกล้ signal ต้องโดนวัดระยะสั้นด้วย —
# 1h เข้ามานำหน้า 4/24/72h เดิม
CLOSE_TF_HORIZONS_H: tuple[float, ...] = (1.0, 4.0, 24.0, 72.0)

M5_MIN = 5.0                    # M5 bar length in minutes
_ANCHOR_TD = pd.Timedelta(minutes=M5_MIN)

# ensemble group labels (also used by the report)
ENS_UP3 = "UP×3"
ENS_DOWN3 = "DOWN×3"
ENS_UP2 = "UP×2"
ENS_DOWN2 = "DOWN×2"
ENS_NONE = "no-signal"
ENS_GROUPS = (ENS_UP3, ENS_DOWN3, ENS_UP2, ENS_DOWN2, ENS_NONE)


def _close_times_ints(m5: pd.DataFrame) -> np.ndarray:
    """M5 bar close times as ints in pandas' native resolution.

    Built ONLY via pandas datetime arithmetic + one astype("int64") —
    never hand-converted epoch seconds (unit-trap regression guard).
    """
    return (m5["time"] + _ANCHOR_TD).astype("int64").to_numpy()


def _target_times_ints(m5: pd.DataFrame, hours: float) -> np.ndarray:
    """Anchor close times shifted by `hours`, same representation."""
    return (m5["time"] + _ANCHOR_TD
            + pd.Timedelta(hours=hours)).astype("int64").to_numpy()


def _horizon_lookups(m5: pd.DataFrame, hours: float) -> np.ndarray:
    """Index (per anchor bar) of the first bar closing at/after t+h.

    -1 marks a censored anchor: no bar in the data closes that late.
    Vectorised: close times are strictly increasing (bars sorted), so a
    left-sided searchsorted gives the first close-time >= target.
    """
    close_times = _close_times_ints(m5)
    targets = _target_times_ints(m5, hours)
    idx = np.searchsorted(close_times, targets, side="left")
    idx = np.where(idx < len(close_times), idx, -1)
    return idx


def _hit_mask(state: Optional[str], ret: np.ndarray,
              eps: float) -> np.ndarray:
    """Per definition: UP hit = ret>0, DOWN hit = ret<0, FLAT = |ret|<=eps."""
    if state == STATE_UP:
        return ret > 0
    if state == STATE_DOWN:
        return ret < 0
    return np.abs(ret) <= eps


def forward_stats(m5: pd.DataFrame, states: dict[str, list[Optional[str]]],
                  horizons: tuple[float, ...] = HORIZONS_H) -> dict:
    """Forward hit-rate / mean move per detector × state × horizon.

    Anchor i = M5 bar i's close time (the exact moment the state is
    known to a bet filling at that bar's close — same convention as the
    conditional grid). Returns baselines so every hit-rate is read
    against its all-anchors reference, plus censored counts.
    """
    closes = m5["close"].to_numpy(float)
    n_bars = len(m5)
    anchor_idx = np.arange(n_bars)

    per_h: dict[float, dict] = {}
    for h in horizons:
        idx = _horizon_lookups(m5, h)
        valid = idx >= 0
        fut = idx[valid]
        anc = anchor_idx[valid]
        ret = closes[fut] - closes[anc]
        eps = float(np.median(np.abs(ret))) if ret.size else None
        per_h[h] = {"idx": idx, "valid": valid, "ret": ret, "eps": eps,
                     "n_censored": int((~valid).sum())}

    def _agg(ret: np.ndarray, eps: float | None, state: Optional[str]):
        if ret.size == 0:
            return {"n": 0, "hit_rate": None, "mean_move": None,
                    "mean_abs_move": None}
        hits = _hit_mask(state, ret, eps)
        return {"n": int(ret.size),
                "hit_rate": float(hits.mean()),
                "mean_move": float(ret.mean()),
                "mean_abs_move": float(np.abs(ret).mean())}

    baselines = {}
    for h in horizons:
        d = per_h[h]
        ret, eps = d["ret"], d["eps"]
        baselines[str(h)] = {
            "n": int(ret.size), "n_censored": d["n_censored"],
            "p_up": float((ret > 0).mean()) if ret.size else None,
            "p_down": float((ret < 0).mean()) if ret.size else None,
            "p_abs_le_eps": (float((np.abs(ret) <= eps).mean())
                              if ret.size and eps is not None else None),
            "eps_median_abs_move": eps,
        }

    detectors = {}
    for det, series in states.items():
        arr = np.asarray([s if s is not None else "" for s in series],
                         dtype=object)
        per_state = {}
        for state in (STATE_UP, STATE_DOWN, STATE_FLAT):
            sel = arr == state
            per_state[state] = {}
            for h in horizons:
                d = per_h[h]
                rows = d["valid"] & sel
                ret = np.where(rows[d["valid"]], d["ret"], np.nan)
                ret = ret[~np.isnan(ret)]
                per_state[state][str(h)] = _agg(ret, d["eps"], state)
        n_known = int((arr != "").sum())
        detectors[det] = {"n_anchor_bars": n_known, "states": per_state}
    return {"horizons_h": list(horizons), "baselines": baselines,
            "detectors": detectors}


def persistence_stats(m5: pd.DataFrame,
                       states: dict[str, list[Optional[str]]],
                       horizons: tuple[float, ...] = HORIZONS_H) -> dict:
    """How long does a state stick: survival + run length per detector.

    survival(h) = P(state at t+h == state at t), measured on the same
    causal M5-aligned labels (state at t+h = the aligned label of the
    first bar closing at/after t+h). run_median_hours = median length
    of a state's contiguous run on the aligned series.
    """
    out = {}
    for det, series in states.items():
        arr = np.asarray([s if s is not None else "" for s in series],
                         dtype=object)
        per_state = {}
        for state in (STATE_UP, STATE_DOWN, STATE_FLAT):
            sel = arr == state
            surv = {}
            for h in horizons:
                idx = _horizon_lookups(m5, h)
                ok = (idx >= 0) & sel
                if ok.sum() == 0:
                    surv[str(h)] = {"n": 0, "same_rate": None}
                    continue
                j = idx[ok]
                same = arr[j] == state
                surv[str(h)] = {"n": int(ok.sum()),
                                "same_rate": float(same.mean())}
            # contiguous runs on the M5-aligned series
            runs = []
            cur = 0
            for s in sel:
                if s:
                    cur += 1
                elif cur:
                    runs.append(cur)
                    cur = 0
            if cur:
                runs.append(cur)
            med = float(np.median(runs)) if runs else None
            per_state[state] = {
                "survival": surv,
                "runs": len(runs),
                "run_median_bars": med,
                "run_median_hours": (med * M5_MIN / 60.0
                                     if med is not None else None),
            }
        out[det] = per_state
    return out


def transition_matrix(series: pd.Series) -> dict:
    """P(next state | current state) over consecutive bars of `series`.

    Pairs where either side is a warmup/NaN label are skipped. Rows are
    the current state, normalised to sum to 1 (row with no transitions
    is reported as empty dict, never fabricated).
    """
    vals = series.to_numpy(dtype=object)
    counts: dict[str, dict[str, int]] = {}
    for i in range(1, len(vals)):
        a, b = vals[i - 1], vals[i]
        # pd.isna covers BOTH None and float NaN — pandas string/None
        # inference can hand back NaN where the caller wrote None
        if pd.isna(a) or pd.isna(b):
            continue
        counts.setdefault(a, {})
        counts[a][b] = counts[a].get(b, 0) + 1
    out = {}
    for a, row in counts.items():
        total = sum(row.values())
        out[a] = {b: c / total for b, c in row.items()} | {"n": total}
    return out


def _ensemble_label(triple: tuple) -> str:
    """Group label for one bar's (h4_ema, d1_structure, h1_adx) triple."""
    votes = [s for s in triple if s in (STATE_UP, STATE_DOWN)]
    n_up = sum(1 for s in votes if s == STATE_UP)
    n_down = len(votes) - n_up
    if n_up == 3:
        return ENS_UP3
    if n_down == 3:
        return ENS_DOWN3
    if n_up == 2:
        return ENS_UP2
    if n_down == 2:
        return ENS_DOWN2
    return ENS_NONE


def ensemble_stats(m5: pd.DataFrame,
                   states: dict[str, list[Optional[str]]],
                   horizons: tuple[float, ...] = HORIZONS_H) -> dict:
    """Forward accuracy when 2-3 detectors agree, vs the no-signal group.

    Bars still inside ANY detector's warmup are excluded (a group label
    needs all three opinions). UP×3/UP×2 hit = forward move > 0,
    DOWN×3/DOWN×2 hit = forward move < 0; the no-signal group is the
    reference bucket.
    """
    det_names = list(states)
    triples = list(zip(*(states[d] for d in det_names)))
    labels = np.asarray([_ensemble_label(t) if all(s is not None for s in t)
                         else "" for t in triples], dtype=object)

    closes = m5["close"].to_numpy(float)
    out: dict[str, dict] = {}
    for h in horizons:
        idx = _horizon_lookups(m5, h)
        valid = idx >= 0
        ret = closes[idx[valid]] - closes[np.flatnonzero(valid)]
        lab = labels[valid]
        per_group = {}
        for grp in ENS_GROUPS:
            sel = lab == grp
            if grp in (ENS_UP3, ENS_UP2):
                hit = ret[sel] > 0
            elif grp in (ENS_DOWN3, ENS_DOWN2):
                hit = ret[sel] < 0
            else:
                hit = np.zeros(int(sel.sum()), dtype=bool)
            per_group[grp] = {
                "n": int(sel.sum()),
                "hit_rate": float(hit.mean()) if sel.sum() else None,
                "mean_move": (float(ret[sel].mean()) if sel.sum() else None),
            }
        out[str(h)] = per_group
    return {"horizons_h": list(horizons), "groups": out}


def build_forward_package(m5: pd.DataFrame,
                          detectors: tuple | None = None,
                          horizons: tuple[float, ...] = HORIZONS_H,
                          with_ensemble: bool | None = None) -> dict:
    """Convenience: everything T2 needs, states built internally.

    Defaults keep the original package byte-identical (3 original
    detectors, 4/24/72h, ensemble on). The close-TF study passes
    NEW_DETECTORS + CLOSE_TF_HORIZONS_H explicitly; ensemble is only
    computed for a 3-detector set (its ×3/×2 labels are defined for
    exactly three votes) — for any other set size it is skipped.
    """
    import broky.betbracket.htf as _htf
    if detectors is None:
        detectors = _htf.DETECTORS
    states = build_detector_states(m5, detectors)
    state_fns = {det.name: vars(_htf)[det.state_fn]
                 for det in detectors}
    transitions = {}
    for det in detectors:
        htf = _htf.resample_htf(m5, det.freq)
        transitions[det.name] = transition_matrix(state_fns[det.name](htf))
    if with_ensemble is None:
        with_ensemble = len(detectors) == 3
    out = {
        "forward": forward_stats(m5, states, horizons),
        "persistence": persistence_stats(m5, states, horizons),
        "transitions": transitions,
    }
    if with_ensemble:
        out["ensemble"] = ensemble_stats(m5, states, horizons)
    return out
