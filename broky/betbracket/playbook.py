"""Per-regime bet menu (regime playbook T3) — pure functions on cells.

Input = the conditional-grid cell dicts (one per detector × formula ×
state, as built by scripts/bet_bracket_conditional.build_payload).
Every table this module produces carries n and breakeven next to each
other (คุณหมอ's anti-oversell rule) and the pass rule is unchanged:

pass   n ≥ 200 · WR(pes) > BE · stability 'stable' (pes WR > BE in
       ≥3 of the last 4 quarters with n≥25) · E(pes) > 0
robust the same (formula, state) passes under ≥2 detectors — guards
       against a single lucky cell (multiple-comparison)
avoid  n ≥ 200 and E(pes) ≤ −AVOID_MAX_E_DOLLARS (a clearly losing
       menu entry, −$5 per $100 risked by default)

Deterministic: no randomness, no wall-clock, no data access — sorting
is by (−E, formula, detector) so ties are stable.
"""
from __future__ import annotations

from broky.betbracket.htf import STATE_DOWN, STATE_FLAT, STATE_UP

STATES = (STATE_UP, STATE_DOWN, STATE_FLAT)

PASS_MIN_N = 200
AVOID_MAX_E_DOLLARS = -5.0     # E ≤ this → avoid list
ROBUST_MIN_DETECTORS = 2
TOP_K = 5
AVOID_K = 3


def passes(cell: dict) -> bool:
    """คุณหมอ's unchanged rule, plus E>0 so ranking never shows a
    negative-expectancy 'pass'."""
    return (cell["n"] >= PASS_MIN_N
            and cell["wr_pes"] is not None
            and cell["wr_pes"] > cell["breakeven_wr"]
            and cell["stability"] == "stable"
            and (cell["e_pes"] or 0.0) > 0.0)


def is_avoid(cell: dict) -> bool:
    return (cell["n"] >= PASS_MIN_N
            and cell["e_pes"] is not None
            and cell["e_pes"] <= AVOID_MAX_E_DOLLARS)


def _key(cell: dict) -> tuple:
    return (-(cell["e_pes"] or 0.0), cell["formula"], cell["detector"])


def passing_cells(cells: list[dict]) -> list[dict]:
    return sorted((c for c in cells if passes(c)), key=_key)


def top_menu(cells: list[dict], detector: str, state: str,
             k: int = TOP_K) -> list[dict]:
    """Top-k passing cells of one detector × state, ranked by E."""
    sel = [c for c in cells if c["detector"] == detector
           and c["state"] == state and passes(c)]
    return sorted(sel, key=_key)[:k]


def avoid_menu(cells: list[dict], detector: str, state: str,
               k: int = AVOID_K) -> list[dict]:
    """Worst-k clearly-losing cells (n≥200) of one detector × state."""
    sel = [c for c in cells if c["detector"] == detector
           and c["state"] == state and is_avoid(c)]
    return sorted(sel, key=lambda c: (c["e_pes"], c["formula"]))[:k]


def robust_map(cells: list[dict]) -> dict[tuple[str, str], list[str]]:
    """(formula, state) → detectors where it passes. len ≥ 2 = robust."""
    out: dict[tuple[str, str], list[str]] = {}
    for c in passing_cells(cells):
        out.setdefault((c["formula"], c["state"]), []).append(c["detector"])
    return out


def robust_label(formula: str, state: str, rmap: dict) -> str:
    dets = rmap.get((formula, state), [])
    if len(dets) >= 3:
        return "แข็งแรง×3"
    if len(dets) == ROBUST_MIN_DETECTORS:
        return "แข็งแรง"
    return ""


def best_detector_for_state(cells: list[dict], state: str) -> str | None:
    """Detector with the most passing cells for `state` (tie → higher
    top E). This is the detector the playbook row for the state uses."""
    best, best_key = None, None
    for det in sorted({c["detector"] for c in cells}):
        all_pass = [c for c in passing_cells(cells)
                    if c["detector"] == det and c["state"] == state]
        if not all_pass:
            continue
        key = (len(all_pass), all_pass[0]["e_pes"])
        if best_key is None or key > best_key:
            best, best_key = det, key
    return best


def playbook_for_state(cells: list[dict], state: str,
                       k_backups: int = 3) -> dict | None:
    """One playbook row: champion + backups + avoid for one state."""
    det = best_detector_for_state(cells, state)
    if det is None:
        return None
    rmap = robust_map(cells)
    menu = top_menu(cells, det, state)
    champ = menu[0]
    backups = menu[1:1 + k_backups]
    return {
        "state": state,
        "detector": det,
        "champion": champ | {"robust": robust_label(
            champ["formula"], champ["state"], rmap)},
        "backups": [b | {"robust": robust_label(
            b["formula"], b["state"], rmap)} for b in backups],
        "avoid": avoid_menu(cells, det, state),
        "n_passing": len(menu),
    }
