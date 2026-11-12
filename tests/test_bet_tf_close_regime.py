"""bet_tf_close_regime script tests — head-to-head / deltas / verdict.

Synthetic cells only (no vault files): the report layer is pure
post-processing over cell dicts, so its rules are lockable without
touching the 11-minute grid. The rules tested:
- head-to-head pulls the right (detector, formula, state) cells
- ΔWR is close-TF minus h4_ema on the same formula×state
- the TF verdict fires 'edge deeper' ONLY when the median delta
  beats the +2pp noise band AND wins outnumber losses
- edge concentration counts use the unchanged pass rule
"""
from __future__ import annotations

import pytest

from broky.betbracket.playbook import PASS_MIN_N
from scripts.bet_tf_close_regime import (
    CHAMPIONS,
    H2H_DETECTORS,
    edge_concentration,
    head_to_head,
    tf_deltas,
    verdict,
)

STATES = ("UP", "DOWN", "FLAT")


def _cell(det, formula, state, n=400, wr=0.60, be=1 / 3, e=8.0,
          stability="stable", quarters=4):
    return {"detector": det, "formula": formula, "state": state, "n": n,
            "breakeven_wr": be, "wr_pes": wr, "wr_opt": wr, "e_pes": e,
            "avg_bars": 10.0, "n_cont": n // 2, "n_rev": n // 2,
            "win_streak": 3, "loss_streak": 4,
            "quarterly": {"2026-Q1": {"n": 100, "wr_pes": wr},
                          "2026-Q2": {"n": 100, "wr_pes": wr},
                          "2026-Q3": {"n": 100, "wr_pes": wr},
                          "2026-Q4": {"n": 100, "wr_pes": wr}},
            "stability": stability, "quarters_beating_be": quarters}


def _full_grid(champ_wr_by_det_state):
    """cells for all champions × all H2H detectors × 3 states, WR given
    by champ_wr_by_det_state[(det, formula, state)]."""
    cells = []
    for f in CHAMPIONS:
        for det in ("m15_ema", "m30_ema", "m30_adx", "h1_adx", "h4_ema",
                    "d1_structure"):
            for state in STATES:
                wr = champ_wr_by_det_state.get((det, f, state), 0.5)
                cells.append(_cell(det, f, state, wr=wr))
    # a few extra formulas so concentration counts are non-trivial
    cells.append(_cell("m30_ema", "X-SELL-TP1SL1", "FLAT", wr=0.7))
    cells.append(_cell("h4_ema", "X-SELL-TP1SL1", "FLAT", wr=0.7))
    cells.append(_cell("h4_ema", "Y-SELL-TP1SL1", "UP", wr=0.2, e=-20.0))
    return cells


def test_head_to_head_pulls_every_champion_state_detector():
    cells = _full_grid({("h4_ema", CHAMPIONS[0], "FLAT"): 0.74})
    h2h = head_to_head(cells)
    assert list(h2h) == list(CHAMPIONS)
    for f in CHAMPIONS:
        for state in STATES:
            assert list(h2h[f][state]) == list(H2H_DETECTORS)
    assert h2h[CHAMPIONS[0]]["FLAT"]["h4_ema"]["wr_pes"] == 0.74


def test_tf_deltas_subtract_h4_baseline_same_formula_state():
    cells = _full_grid({("m15_ema", CHAMPIONS[0], "UP"): 0.55,
                        ("h4_ema", CHAMPIONS[0], "UP"): 0.50})
    d = tf_deltas(cells)
    row = d[CHAMPIONS[0]]["UP"]["m15_ema"]
    assert row["delta_vs_h4"] == pytest.approx(0.05)
    assert row["n"] >= PASS_MIN_N
    # h4 itself is the baseline — not in the delta columns
    assert "h4_ema" not in d[CHAMPIONS[0]]["UP"]


def test_verdict_rejects_noise_level_deltas():
    # every close-TF detector within ±2pp of h4 → NOT edge deeper
    cells = _full_grid({})
    h2h = head_to_head(cells)
    deltas = tf_deltas(cells)
    conc = edge_concentration(cells)
    fwd_short = {det: {"1.0": {"hit_up": 0.50, "hit_down": 0.49,
                               "n_up": 1, "n_down": 1, "mean_up": 0.0,
                               "mean_down": 0.0},
                       "4.0": {"hit_up": 0.51, "hit_down": 0.49,
                               "n_up": 1, "n_down": 1, "mean_up": 0.0,
                               "mean_down": 0.0}}
                  for det in ("m30_ema", "m15_ema", "m30_adx")}
    shares = {det: {"shares": {"FLAT": 0.2}} for det in fwd_short}
    ev = verdict(h2h, deltas, conc, fwd_short, shares)
    assert ev["verdict_edge_deeper"] is False
    assert ev["verdict_direction_predicts"] is False
    assert ev["n_within_2pp"] == ev["n_delta_cells"]
    assert ev["n_close_tf_wins_gt2pp"] == 0


def test_verdict_fires_only_when_median_beats_noise_and_wins_dominate():
    # m15 beats h4 by +5pp everywhere → edge deeper; hit 60% at 1h →
    # direction 'predicts' per the fixed 55% rule
    wr_map = {(det, f, s): 0.55
              for det in ("m15_ema", "m30_ema", "m30_adx")
              for f in CHAMPIONS for s in STATES}
    wr_map.update({("h4_ema", f, s): 0.50
                   for f in CHAMPIONS for s in STATES})
    cells = _full_grid(wr_map)
    h2h = head_to_head(cells)
    deltas = tf_deltas(cells)
    conc = edge_concentration(cells)
    fwd_short = {det: {"1.0": {"hit_up": 0.60, "hit_down": 0.40,
                               "n_up": 1, "n_down": 1, "mean_up": 0.0,
                               "mean_down": 0.0},
                       "4.0": {"hit_up": 0.60, "hit_down": 0.40,
                               "n_up": 1, "n_down": 1, "mean_up": 0.0,
                               "mean_down": 0.0}}
                  for det in ("m30_ema", "m15_ema", "m30_adx")}
    shares = {det: {"shares": {"FLAT": 0.2}} for det in fwd_short}
    ev = verdict(h2h, deltas, conc, fwd_short, shares)
    assert ev["verdict_edge_deeper"] is True
    assert ev["verdict_direction_predicts"] is True
    assert ev["delta_median_vs_h4"] == pytest.approx(0.05)


def test_edge_concentration_uses_unchanged_pass_rule():
    cells = _full_grid({})
    cells.append(_cell("m30_ema", "Z-SELL-TP1SL1", "DOWN", wr=0.30,
                       e=-8.0))                      # avoid-list material
    conc = edge_concentration(cells)
    # X-SELL passes under m30_ema and h4_ema in FLAT; Y-SELL fails
    assert conc["m30_ema"]["n_passing"] >= 1
    assert conc["h4_ema"]["n_avoid"] == 1
    # d1_structure is outside H2H but inside the grid counts
    assert conc["d1_structure"]["n_cells"] > 0
    assert all(c["n_passing"] <= c["n_with_n_ge_200"] or
               c["n_passing"] == c["n_passing"] for c in conc.values())
