"""Playbook menu tests — pass/avoid/robust rules on synthetic cells.

Every rule here is the reported contract (คุณหมอ 13/11): n ≥ 200 ·
WR pes > BE · stability 'stable' · E > 0 → pass; n ≥ 200 and E ≤ −$5
→ avoid; the same (formula, state) passing under ≥2 detectors → robust.
"""
from __future__ import annotations

from broky.betbracket.playbook import (
    AVOID_MAX_E_DOLLARS,
    PASS_MIN_N,
    avoid_menu,
    best_detector_for_state,
    is_avoid,
    passing_cells,
    playbook_for_state,
    robust_label,
    robust_map,
    top_menu,
)


def cell(det="h4_ema", formula="F-SELL-TP1SL1", state="FLAT",
         n=250, be=0.5, wr=0.55, e=5.0, stability="stable", qb=4):
    return {"detector": det, "formula": formula, "state": state, "n": n,
            "breakeven_wr": be, "wr_pes": wr, "e_pes": e,
            "stability": stability, "quarters_beating_be": qb,
            "avg_bars": 10}


def test_pass_needs_all_four_gates():
    assert passing_cells([cell()])
    assert not passing_cells([cell(n=199)])                    # n < 200
    assert not passing_cells([cell(wr=0.5)])                   # WR == BE
    assert not passing_cells([cell(stability="era")])          # not stable
    assert not passing_cells([cell(e=-0.01)])                  # E <= 0
    assert not passing_cells([cell(e=None)])


def test_pass_min_n_matches_reported_rule():
    assert PASS_MIN_N == 200


def test_avoid_needs_clear_negative_e_and_n():
    assert is_avoid(cell(e=-5.0, wr=0.4))
    assert not is_avoid(cell(e=-4.99, wr=0.4))       # not clearly negative
    assert not is_avoid(cell(e=-50.0, wr=0.4, n=199))
    assert AVOID_MAX_E_DOLLARS == -5.0


def test_top_menu_ranks_by_e_and_caps_k():
    cells = [cell(formula="A", e=3.0), cell(formula="B", e=9.0),
             cell(formula="C", e=6.0), cell(formula="D", e=1.0),
             cell(formula="E", e=12.0), cell(formula="F", e=0.5)]
    menu = top_menu(cells, "h4_ema", "FLAT", k=5)
    assert [c["formula"] for c in menu] == ["E", "B", "C", "A", "D"]


def test_robust_map_counts_detectors_not_cells():
    cells = [cell(det="h4_ema"), cell(det="d1_structure"),
             cell(det="d1_structure", formula="X", state="UP")]
    rmap = robust_map(cells)
    assert rmap[("F-SELL-TP1SL1", "FLAT")] == ["d1_structure", "h4_ema"]
    assert robust_label("F-SELL-TP1SL1", "FLAT", rmap) == "แข็งแรง"
    assert robust_label("X", "UP", rmap) == ""


def test_robust_label_three_detectors():
    cells = [cell(det="h4_ema"), cell(det="d1_structure"),
             cell(det="h1_adx")]
    rmap = robust_map(cells)
    assert robust_label("F-SELL-TP1SL1", "FLAT", rmap) == "แข็งแรง×3"


def test_best_detector_most_passing_then_top_e():
    a = [cell(det="a", formula="A", e=2.0), cell(det="a", formula="B", e=1.0)]
    b = [cell(det="b", formula="C", e=8.0)]
    assert best_detector_for_state(a + b, "FLAT") == "a"   # 2 passing vs 1
    assert best_detector_for_state(b, "FLAT") == "b"


def test_playbook_row_shape():
    cells = [cell(det="a", formula="Champ", e=20.0),
             cell(det="a", formula="Back1", e=10.0),
             cell(det="a", formula="Back2", e=5.0),
             cell(det="a", formula="Back3", e=2.0),
             cell(det="a", formula="Avoid", e=-15.0, wr=0.3)]
    pb = playbook_for_state(cells, "FLAT")
    assert pb["detector"] == "a"
    assert pb["champion"]["formula"] == "Champ"
    assert [b["formula"] for b in pb["backups"]] == ["Back1", "Back2", "Back3"]
    assert pb["avoid"][0]["formula"] == "Avoid"
    # robust = passes under ≥2 DETECTORS — everything here is det "a" only,
    # so the champion carries no robust tag (multiple-comparison guard)
    assert pb["champion"]["robust"] == ""
    assert pb["n_passing"] == 4


def test_playbook_none_when_nothing_passes():
    cells = [cell(det="a", n=10)]                    # n too small to pass
    assert best_detector_for_state(cells, "FLAT") is None
    assert playbook_for_state(cells, "FLAT") is None


def test_ranking_is_deterministic_on_ties():
    cells = [cell(det="a", formula="B", e=5.0),
             cell(det="b", formula="A", e=5.0)]
    r1 = [c["formula"] for c in passing_cells(cells)]
    r2 = [c["formula"] for c in passing_cells(cells)]
    assert r1 == r2 == ["A", "B"]       # tie broken by formula name, stable
