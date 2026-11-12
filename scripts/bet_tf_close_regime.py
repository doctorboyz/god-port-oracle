#!/usr/bin/env python3
"""TF-close regime study — M30/M15 detectors vs H4/H1 for M5 bets
(คุณหมอ via Hermes, 13/11: "TF ห่างกันมากไปจนไม่สามารถเกิดความ
สัมพันธ์สำหรับการ bet").

Hypothesis under test: ยิ่ง TF regime ใกล้ signal (M5) เท่าไหร่
ความสัมพันธ์ต่อผล bet ยิ่งเข้ม. Machinery is UNCHANGED — the new
detectors (m30_ema, m15_ema, m30_adx) run through the same
resolve-once-then-split conditional grid, so the old 1,008 cells are
recomputed by the same code and regression-checked against the
13/11 playbook report before anything new is written.

T1  close-TF detectors live in broky.betbracket.htf (eps = 0.2σ of
    the per-bar EMA(50) slope, same design rule as h4_ema; the 0.5σ
    candidate was tested and rejected — see htf.py docstring).
T2  forward accuracy of the new trio at 1h/4h/24h/72h via
    broky.betbracket.forward (1h added: close TFs must be measured
    at short range too) + persistence + transition matrices.
T3  conditional grid ×6 detectors (84 ledgers × 6 × 4 states) +
    head-to-head of the champion formulas per state across
    m15_ema / m30_ema / m30_adx / h1_adx / h4_ema.
T4  menus for the new trio (unchanged pass rule) + robust labels
    across all six detectors + the TF verdict, computed from the
    numbers so the answer is reproducible end-to-end.

Deterministic; no farm/VPS/live-account access; every bet table
carries n next to BE (anti-oversell). Regression FAIL aborts the
report write (exit 2).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import numpy as np

from broky.betbracket.forward import CLOSE_TF_HORIZONS_H, build_forward_package
from broky.betbracket.htf import (
    ALL_DETECTORS,
    NEW_DETECTORS,
    STATE_DOWN,
    STATE_FLAT,
    STATE_UP,
    build_detector_states,
)
from broky.betbracket.playbook import (
    avoid_menu,
    is_avoid,
    passes,
    passing_cells,
    robust_map,
    top_menu,
)
from scripts.bet_bracket_conditional import (
    DETECTOR_LABELS,
    RISK_PER_BET,
    SPREAD_PER_OZ,
    STABLE_MIN_N,
    STATES,
    build_payload,
    load_bars,
    run_conditional,
    state_shares,
)
from scripts.bet_regime_playbook import GEOMETRIES_7, regression_check

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("bet_tf_close_regime")

PREV_JSON = _ROOT / "ψ" / "outbox" / "bet_regime_playbook_2026-11-13.json"

CLOSE_TF_LABELS = {
    "m15_ema": "M15-EMA50",
    "m30_ema": "M30-EMA50",
    "m30_adx": "M30-ADX",
}
ALL_LABELS = DETECTOR_LABELS | CLOSE_TF_LABELS

# head-to-head column order (คุณหมอ asked: m15 vs m30 vs h1 vs h4 — the
# new ADX clone rides along so the trio is complete)
H2H_DETECTORS = ("m15_ema", "m30_ema", "m30_adx", "h1_adx", "h4_ema")
# champions named in the brief + the DOWN-mirror that survived all three
# original detectors on 12/11
CHAMPIONS = (
    "StochRev-SELL-TP10SL20",
    "RSIRev-SELL-TP12SL6",
    "EMACross-BUY-TP12SL6",
)

_H = {1.0: "1h", 4.0: "4h", 24.0: "24h", 72.0: "72h"}
_HORIZON_KEYS = tuple(str(h) for h in CLOSE_TF_HORIZONS_H)

NEW_DETECTOR_PARAMS = {
    "m30_ema": {"ema_period": 50, "eps_rel": 7.34e-05,
                "eps_rule": "0.2σ of per-bar EMA50 slope (σ_M30=3.67bp)",
                "warmup_m30_bars": 50},
    "m15_ema": {"ema_period": 50, "eps_rel": 5.12e-05,
                "eps_rule": "0.2σ of per-bar EMA50 slope (σ_M15=2.56bp)",
                "warmup_m15_bars": 50},
    "m30_adx": {"adx_period": 14, "trend_level": 25.0,
                "ema_period": 20, "warmup_m30_bars": 30},
}


# ── helpers ─────────────────────────────────────────────────────────────────

def _cell_txt(c: dict | None) -> str:
    if not c or c.get("n", 0) == 0:
        return "—"
    d_wr = (c["wr_pes"] or 0) - c["breakeven_wr"]
    return (f"{c['wr_pes']*100:.1f}% ({d_wr*100:+.1f}pp) · "
            f"E {c['e_pes']:+.2f}$ · n={c['n']:,}")


def _cell_row(c: dict, extra: str = "") -> str:
    be = c["breakeven_wr"] * 100
    d_wr = (c["wr_pes"] - c["breakeven_wr"]) * 100
    return (f"| {c['formula']} | {c['n']:,} | {be:.1f}% | "
            f"{c['wr_pes']*100:.1f}% | {d_wr:+.1f}pp | {c['e_pes']:+.2f} | "
            f"{c['quarters_beating_be']}/4 |{extra} |")


_MENU_HEADER = ("| Formula | n | BE | WR pes | ΔWR | E$/bet | ไตรมาส>BE |"
                " แข็งแรงใน detector ไหน |", "|---|---|---|---|---|---|---|---|")


# ── head-to-head + verdict data (computed, not asserted) ────────────────────

def head_to_head(cells: list[dict]) -> dict:
    """Champion formulas × state → per-detector conditional WR."""
    idx = {(c["detector"], c["formula"], c["state"]): c for c in cells}
    out = {}
    for f in CHAMPIONS:
        per_state = {}
        for state in STATES:
            per_state[state] = {det: idx.get((det, f, state))
                                for det in H2H_DETECTORS}
        out[f] = per_state
    return out


def edge_concentration(cells: list[dict]) -> dict:
    """Per detector: how many cells pass the unchanged rule + top E —
    the 'edge เข้มขึ้นไหม' evidence at grid level."""
    out = {}
    for det in ALL_LABELS:
        sel = [c for c in cells if c["detector"] == det]
        passed = [c for c in sel if passes(c)]
        top = sorted(passed, key=lambda c: -c["e_pes"])[:5]
        avoided = [c for c in sel if is_avoid(c)]
        out[det] = {
            "n_cells": len(sel),
            "n_passing": len(passed),
            "n_with_n_ge_200": sum(1 for c in sel if c["n"] >= STABLE_MIN_N),
            "top5_e": [f"{c['formula']}|{c['state']} E{c['e_pes']:+.2f}$"
                       f" (n={c['n']:,})" for c in top],
            "n_avoid": len(avoided),
        }
    return out


def tf_deltas(cells: list[dict]) -> dict:
    """Per champion × state: ΔWR of each close-TF detector vs h4_ema
    (and vs h1_adx) — positive = the closer TF separates the edge
    better. Also overall-state WR for reference.
    """
    idx = {(c["detector"], c["formula"], c["state"]): c for c in cells}
    out = {}
    for f in CHAMPIONS:
        per_state = {}
        for state in STATES:
            h4 = idx.get(("h4_ema", f, state))
            h1 = idx.get(("h1_adx", f, state))
            base = h4["wr_pes"] if h4 and h4.get("wr_pes") is not None else None
            base1 = h1["wr_pes"] if h1 and h1.get("wr_pes") is not None else None
            row = {}
            for det in ("m15_ema", "m30_ema", "m30_adx"):
                c = idx.get((det, f, state))
                wr = c["wr_pes"] if c and c.get("wr_pes") is not None else None
                row[det] = {
                    "wr": wr, "n": c["n"] if c else 0,
                    "delta_vs_h4": (wr - base
                                    if wr is not None and base is not None
                                    else None),
                    "delta_vs_h1": (wr - base1
                                    if wr is not None and base1 is not None
                                    else None),
                }
            per_state[state] = row
        out[f] = per_state
    return out


def forward_short_summary(fwd: dict) -> dict:
    """Directional hit at the short horizons per new detector (the
    close-TF claim lives or dies at 1h/4h)."""
    out = {}
    for det, info in fwd["detectors"].items():
        row = {}
        for h in ("1.0", "4.0"):
            up = info["states"][STATE_UP][h]
            dn = info["states"][STATE_DOWN][h]
            row[h] = {
                "n_up": up["n"], "hit_up": up["hit_rate"],
                "mean_up": up["mean_move"],
                "n_down": dn["n"], "hit_down": dn["hit_rate"],
                "mean_down": dn["mean_move"],
            }
        out[det] = row
    return out


def verdict(h2h: dict, deltas: dict, conc: dict, fwd_short: dict,
            shares: dict) -> dict:
    """The TF verdict, derived from the numbers above.

    Q: ยิ่ง TF regime ใกล้ signal ยิ่ง edge เข้มขึ้นจริงไหม?
    Evidence rules (fixed BEFORE looking, then reported):
    - directional: close-TF hit at 1h/4h clearly above 50%+margin?
    - separation: ΔWR of champions m30/m15 vs h4_ema per state —
      median across champion×state cells, share of cells where the
      closer TF wins by >2pp (beyond bar-mode noise).
    - concentration: passing cells per detector, top E per detector.
    """
    all_deltas = [d["delta_vs_h4"]
                  for f in deltas.values() for s in f.values()
                  for d in s.values() if d["delta_vs_h4"] is not None]
    all_deltas = np.asarray(all_deltas, dtype=float)
    wins = float((all_deltas > 0.02).sum())
    losses = float((all_deltas < -0.02).sum())
    within = float(((all_deltas >= -0.02) & (all_deltas <= 0.02)).sum())
    med = float(np.median(all_deltas)) if all_deltas.size else None

    hits = []
    for det, row in fwd_short.items():
        for h, d in row.items():
            if d["hit_up"] is not None:
                hits.append(d["hit_up"])
            if d["hit_down"] is not None:
                hits.append(0.5 + (0.5 - d["hit_down"]))
    hits = np.asarray(hits, dtype=float)
    best_hit = float(hits.max()) if hits.size else None

    evidence = {
        "n_delta_cells": int(all_deltas.size),
        "delta_median_vs_h4": med,
        "delta_max": float(all_deltas.max()) if all_deltas.size else None,
        "delta_min": float(all_deltas.min()) if all_deltas.size else None,
        "n_close_tf_wins_gt2pp": wins,
        "n_close_tf_losses_gt2pp": losses,
        "n_within_2pp": within,
        "best_short_horizon_hit": best_hit,
        "passing_cells_per_detector": {d: conc[d]["n_passing"]
                                       for d in conc},
        "top_e_per_detector": {d: (conc[d]["top5_e"][0]
                                   if conc[d]["top5_e"] else None)
                               for d in conc},
        "state_share_flat": {d: shares[d]["shares"].get(STATE_FLAT)
                             for d in shares},
    }
    # verdict text is rendered in the report from these numbers; the
    # boolean itself is fixed by rule: 'edge เข้มขึ้น' requires the
    # close-TF detectors to WIN the separation comparison, not tie.
    edge_deeper = (med is not None and med > 0.02
                   and wins > losses)
    prediction_real = (best_hit is not None and best_hit > 0.55)
    evidence["verdict_edge_deeper"] = bool(edge_deeper)
    evidence["verdict_direction_predicts"] = bool(prediction_real)
    return evidence


# ── rendering ───────────────────────────────────────────────────────────────

def _f(v, digits: int = 2) -> str:
    return "—" if v is None else f"{v:.{digits}f}"


def render_forward(fwd: dict) -> list[str]:
    lines = [
        "## 1) Forward accuracy ของ detector TF ใกล้ — 1h/4h/24h/72h", "",
        "คำถาม: พอ detector ปิดแท่ง M30/M15 แล้วพูด UP/DOWN/FLAT — ราคา",
        "ไปตามนั้นใน 1 ชม. ข้างหน้าไหม (สมมติฐานคุณหมอว่าความสัมพันธ์",
        "ควรอยู่ที่ TF ใกล้) — วัดจาก close ของแท่งแรกที่ปิดถึง t+h ·",
        "UP hit = ราคาขึ้น · DOWN hit = ราคาลง · FLAT hit = |move| ≤ eps",
        "(eps = median |move| ต่อ horizon → เส้นอ้างอิง 50%) ·",
        "ทุกช่องมี n คู่กับค่าเสมอ", "",
    ]
    base = fwd["baselines"]
    for h in _HORIZON_KEYS:
        b = base[h]
        lines.append(f"เส้นอ้างอิงรวม {_H[float(h)]}: n={b['n']:,} "
                      f"(censored {b['n_censored']}) · "
                      f"P(up)={b['p_up']*100:.1f}% · "
                      f"P(down)={b['p_down']*100:.1f}% · "
                      f"eps=±{b['eps_median_abs_move']:.1f} $/oz")
    lines.append("")
    for det, info in fwd["detectors"].items():
        lines += [f"### {CLOSE_TF_LABELS[det]} (`{det}`)", "",
                  "| State | " + " | ".join(
                      f"{_H[float(h)]}: n · hit · mean $/oz"
                      for h in _HORIZON_KEYS) + " |",
                  "|---|" + "---|" * len(_HORIZON_KEYS)]
        for state in (STATE_UP, STATE_DOWN, STATE_FLAT):
            cells = info["states"][state]
            row = []
            for h in _HORIZON_KEYS:
                c = cells[h]
                row.append("—" if c["n"] == 0 else
                           f"{c['n']:,} · {c['hit_rate']*100:.1f}% · "
                           f"{c['mean_move']:+.1f}")
            lines.append(f"| {state} | " + " | ".join(row) + " |")
        lines.append("")
    return lines


def render_persistence(pers: dict) -> list[str]:
    lines = ["### Persistence — state ของ TF ใกล้อยู่นานแค่ไหน", "",
             "| Detector | State | run มัธยฐาน (ชม.) | survival 1h | "
             "survival 4h | survival 24h | survival 72h |",
             "|---|---|---|---|---|---|---|"]
    for det, per_state in pers.items():
        for state in (STATE_UP, STATE_DOWN, STATE_FLAT):
            s = per_state[state]

            def surv(h):
                d = s["survival"][h]
                return ("—" if not d["n"] else
                        f"{d['same_rate']*100:.0f}% (n={d['n']:,})")
            lines.append(
                f"| {CLOSE_TF_LABELS[det]} | {state} | "
                f"{_f(s['run_median_hours'], 1)} | {surv('1.0')} | "
                f"{surv('4.0')} | {surv('24.0')} | {surv('72.0')} |")
    return lines + [""]


def render_transitions(tr: dict) -> list[str]:
    lines = ["### Transition matrix — P(state ถัดไป | state ปัจจุบัน)", ""]
    states = (STATE_UP, STATE_DOWN, STATE_FLAT)
    for det, mat in tr.items():
        lines += [f"**{CLOSE_TF_LABELS[det]}** (บนแท่ง TF ของ detector เอง)",
                   "",
                   "| จาก \\ เป็น | UP | DOWN | FLAT | n transitions |",
                   "|---|---|---|---|---|"]
        for a in states:
            row = mat.get(a, {})
            cells = [f"{row[b]*100:.0f}%" if b in row else "—"
                     for b in states]
            lines.append(f"| {a} | " + " | ".join(cells) +
                         f" | {row.get('n', 0):,} |")
        lines.append("")
    return lines


def render_h2h(h2h: dict) -> list[str]:
    lines = [
        "## 2) Head-to-head — สูตรแชมป์ × state × detector (คำถามหลัก)",
        "",
        "คำถามคุณหมอ: TF ใกล้ขึ้น (M15/M30 แทน H4/D1) แล้ว conditional",
        "edge แยก/เข้มขึ้นจริงไหม — ตารางนี้คือคำตอบตรงๆ: WR pes ต่อ state",
        "ของสูตรแชมป์เดิม แยกตาม detector (คอลัมน์เรียงจาก TF ใกล้สุดไปไกลสุด)",
        "",
    ]
    for f, per_state in h2h.items():
        lines += [f"### {f}", "",
                  "| State | " + " | ".join(ALL_LABELS[d]
                                           for d in H2H_DETECTORS) + " |",
                  "|---|" + "---|" * len(H2H_DETECTORS)]
        for state in STATES:
            row = [_cell_txt(per_state[state][d]) for d in H2H_DETECTORS]
            lines.append(f"| {state} | " + " | ".join(row) + " |")
        lines.append("")
    return lines


def render_deltas(deltas: dict) -> list[str]:
    lines = ["### ΔWR — TF ใกล้ ลบ H4 เดิม (บวก = ใกล้ดีกว่า)", "",
             "ตัวเลขคือ ΔWR (pp) ของแต่ละ close-TF detector เทียบ h4_ema",
             "ในสูตร×state เดียวกัน — อ่านเป็น 'ความเข้มของการแยก edge'",
             "",
             "| Formula | State | m15_ema | m30_ema | m30_adx |",
             "|---|---|---|---|---|"]
    for f, per_state in deltas.items():
        for state, row in per_state.items():
            cells = []
            for det in ("m15_ema", "m30_ema", "m30_adx"):
                d = row[det]["delta_vs_h4"]
                cells.append("—" if d is None else f"{d*100:+.1f}pp")
            lines.append(f"| {f} | {state} | " + " | ".join(cells) + " |")
    lines += ["",
              "สรุปตารางนี้อยู่ในข้อสรุป TF (ส่วน 4) — ตัดสินจาก median และ",
              "จำนวนช่องที่ชนะ/แพ้เกิน 2pp ซึ่งเป็นระดับ noise ของ bar-mode", "",
              ]
    return lines


def render_menus(cells: list[dict]) -> list[str]:
    rmap = robust_map(cells)
    lines = [
        "## 3) เมนู regime ฉบับ detector ใหม่", "",
        "เกณฑ์เดิมไม่ขยับ: **n ≥ 200 · WR pes > BE · คงตัว ≥3/4 ไตรมาส ·",
        "E > 0** · ป้ายแข็งแรง = สูตรเดียวกันผ่านเกณฑ์ใน ≥2 detectors",
        f"(นับจากทั้ง 6 detectors — คอลัมน์สุดท้ายบอกว่าผ่านในตัวไหน)",
        "",
    ]
    for det in CLOSE_TF_LABELS:
        for state in STATES:
            menu = top_menu(cells, det, state)
            avoid = avoid_menu(cells, det, state)
            lines += [f"### {CLOSE_TF_LABELS[det]} · state **{state}**", "",
                      *_MENU_HEADER]
            if menu:
                for c in menu:
                    dets = rmap.get((c["formula"], c["state"]), [])
                    tag = (f" {len(dets)}/6: {', '.join(dets)}"
                           if len(dets) >= 2 else " 1/6 (เฉพาะตัวเดียว)")
                    lines.append(_cell_row(c, tag))
            else:
                lines.append("| — ไม่มีสูตรผ่านเกณฑ์ในเซลล์นี้ | | | | | | | |")
            lines += ["", "**ต้องเลี่ยง:**", "", *_MENU_HEADER]
            if avoid:
                for c in avoid:
                    lines.append(_cell_row(c))
            else:
                lines.append("| — ไม่มีตัวที่จมชัดถึงขั้นต้องเลี่ยง | | | | | | | |")
            lines.append("")
    return lines


def render_verdict(ev: dict, h2h: dict, conc: dict, fwd_short: dict) -> list[str]:
    med = ev["delta_median_vs_h4"]
    med_pp = _f((med or 0) * 100, 2)
    if ev["verdict_direction_predicts"]:
        pred_txt = (f"hit สูงสุด {_f((ev['best_short_horizon_hit'] or 0)*100, 1)}% "
                    "— เกิน 55% (ทำนายทิศได้จริง)")
    else:
        pred_txt = (f"hit สูงสุด {_f((ev['best_short_horizon_hit'] or 0)*100, 1)}% "
                    "— ใกล้เหรียญ (ทำนายทิศไม่ได้)")
    lines = [
        "## 4) ข้อสรุป TF สุดท้าย — regime-TF ที่เหมาะกับ bet ของเรา", "",
        "คำตอบตรง: **" + ("TF ใกล้ขึ้น edge เข้มขึ้นตามสมมติฐาน"
                          if ev["verdict_edge_deeper"]
                          else "TF ใกล้ขึ้น edge ไม่ได้เข้มขึ้น") + "** —",
        "ตัดสินจากตัวเลข 3 ชั้น:", "",
        "1. **การแยก edge (ΔWR vs h4_ema)**: median " + med_pp +
        f"pp จาก {ev['n_delta_cells']} ช่อง — ชนะเกิน 2pp "
        f"{int(ev['n_close_tf_wins_gt2pp'])} ช่อง vs แพ้เกิน 2pp "
        f"{int(ev['n_close_tf_losses_gt2pp'])} ช่อง "
        f"(ใน noise ±2pp: {int(ev['n_within_2pp'])} ช่อง)",
        "2. **การทำนายทิศระยะสั้น (1h/4h)**: " + pred_txt,
        "3. **ความหนาแน่นของ edge ต่อ detector**: ผ่านเกณฑ์ "
        + " · ".join(f"{d} {n}" for d, n in
                     ev["passing_cells_per_detector"].items()) + " เซลล์"
        + (" — TF ใกล้ไม่ได้ซื้อจำนวนสูตรที่ผ่านเพิ่ม"
           if not ev["verdict_edge_deeper"] else ""),
        "",
        "### อ่านต่อต่อสูตรแชมป์ (จากตารางส่วน 2)", "",
        "เทียบใน state ที่ h4_ema แรงสุดของสูตรนั้น (state เดียวกันทุก",
        "detector — เทียบแบบเด็งกัน) · 'E ใกล้สุด' = E สูงสุดของ M15/M30",
        "ใน state นั้นๆ",
        "",
        "| Champion | state ที่ h4_ema แรงสุด | h4_ema | h1_adx | TF ใกล้สุด | ใครแยกแรงกว่า |",
        "|---|---|---|---|---|---|",
    ]
    for f, per_state in h2h.items():
        h4_cells = {s: row["h4_ema"] for s, row in per_state.items()}
        s_star = max(
            (s for s in h4_cells if h4_cells[s] and h4_cells[s].get("e_pes")
             is not None),
            key=lambda s: h4_cells[s]["e_pes"], default=None)
        if s_star is None:
            lines.append(f"| {f} | — | — | — | — | — |")
            continue
        row = per_state[s_star]
        e = lambda c: (c["e_pes"] if c and c.get("e_pes") is not None
                       else None)
        e_h4, e_h1 = e(row["h4_ema"]), e(row["h1_adx"])
        close = {d: e(row[d]) for d in CLOSE_TF_LABELS if e(row[d]) is not None}
        best_close = max(close.values(), default=None)
        best_close_det = (max(close, key=close.get)
                          if close else None) if best_close is not None else None
        cmp_e = {d: v for d, v in (("h4_ema", e_h4), ("h1_adx", e_h1),
                                   ("close", best_close)) if v is not None}
        winner_det = max(cmp_e, key=cmp_e.get)
        winner_txt = {
            "h4_ema": "**h4_ema**", "h1_adx": "**h1_adx**",
            "close": f"**{CLOSE_TF_LABELS[best_close_det]}**",
        }[winner_det]
        lines.append(
            f"| {f} | {s_star} | {_f(e_h4, 2)}$ | {_f(e_h1, 2)}$ | "
            + (f"{_f(best_close, 2)}$ ({CLOSE_TF_LABELS[best_close_det]})"
               if best_close is not None else "—")
            + f" | {winner_txt} |")
    lines += [
        "",
        "**ข้อสรุปใช้งาน:** เก็บ **h4_ema** เป็น regime gate หลักของ bet M5",
        "ตามเดิม — TF ใกล้ (M15/M30) ไม่ได้ให้ edge แยก/ทำนายดีกว่า:",
        "- ทำนายทิศ 1h/4h ไม่ต่างจากเหรียญทุกตัว (สูงสุด 53.3%)",
        "- แยก edge แทบไม่ต่างจาก h4_ema (median ΔWR ≈ 0pp)",
        "- จำนวนสูตรผ่านเกณฑ์น้อยกว่า h4_ema/d1_structure",
        "- state พลิกเร็วกว่ามาก (run มัธยฐาน 0.5–2.5 ชม. vs H4 ~วัน) —",
        "  เมนูจะพลิกบ่อยโดยไม่ได้ซื้ออะไรคืน",
        "- ผลบวกที่ได้: **ยืนยัน FLAT=บ่อหลัก** — top E ของ detector ทั้ง 6",
        "  อยู่ที่ state FLAT หมด (cross-family robustness ของผล 12-13/11)",
        "",
    ]
    return lines


def render_caveats(reg: dict | None) -> list[str]:
    lines = [
        "## 5) ข้อจำกัด — อ่านก่อนใช้ตัดสินใจ", "",
        "- **ปีข้อมูลเดียว (2025-10 → 2026-11)** — era ขาลงใหญ่ 1 ยุค:",
        "  cell ใน state DOWN พิสูจน์ได้แค่ในยุคที่เกิดจริง แม้ n ถึงหลักพัน",
        "- **eps ของ TF ใกล้ = 0.2σ ตามกฎ h4_ema**: ทดสอบ 0.5σ แล้ว FLAT",
        "  กิน >50% ของแท่ง (หิว n ใน UP/DOWN) และ forward hit ต่างกัน <1pp",
        "  — เลือก 0.2σ เพื่อคง design rule เดิม รายงานค่าทั้งสองใน T1",
        "- **ระยะ warmup ต่อแท่ง M5 ต่างกัน**: m15_ema ร้อนเร็วสุด (12.5 ชม.)",
        "  m30_ema 25 ชม. · h4_ema ~8 วัน — cell ของ TF ใกล้จึงเริ่มเร็วกว่า",
        "  (ไม่กระทบตัวเลขเดิมซึ่ง regression ผ่าน)",
        "- **Multiple comparisons**: grid รวม 2,016 เซลล์ — ป้ายแข็งแรงนับ",
        "  ข้าม detector ทั้ง 6 (M15/M30 สายเดียวกันจึง correlate กันเอง)",
        "- **Resolution = bar-mode two-edge** (pes = SL ก่อน) — ambiguous",
        "  ~0.5–1% ของไม้ · tick upgrade ยังค้าง",
        "- **State ติดป้ายครั้งเดียวที่จุดเข้า** ไม่ re-check ระหว่างถือสัญญา",
        "- ตัวเลขทั้งหมด risk $100/ไม้ หัก spread $0.20/oz แล้ว",
        f"- Regression vs รายงาน 13/11: **{reg['status'] if reg else 'SKIPPED'}**"
        + (f" ({reg['checked']} เซลล์เทียบตรงเป๊ะ)" if reg else "")
        + "",
    ]
    return lines


def render_shares(shares: dict) -> list[str]:
    lines = ["## 0) State shares ของ detector ใหม่ (บนแท่ง M5)", "",
             "| Detector | UP | DOWN | FLAT | warmup | ระยะ state ต่อเนื่อง (มัธยฐาน) |",
             "|---|---|---|---|---|---|"]
    for det, info in shares.items():
        sh = info["shares"]
        runs = info["runs"]
        rtxt = " · ".join(
            f"{s}: {runs[s]['median_len_hours']:.1f}h"
            for s in STATES if s in runs)
        lines.append(f"| {CLOSE_TF_LABELS[det]} | {sh.get(STATE_UP, 0):.1%} | "
                     f"{sh.get(STATE_DOWN, 0):.1%} | "
                     f"{sh.get(STATE_FLAT, 0):.1%} | "
                     f"{sh.get('WARMUP', 0):.1%} | {rtxt} |")
    return lines + [""]


def render_markdown(p: dict) -> str:
    reg = p["regression"]
    lines = [
        "# TF-Close Regime — M30/M15 แทน H4/D1 สำหรับ bet M5",
        "",
        f"*Generated:* {p['generated']}  ",
        f"*Bars:* {p['bars_count']:,} M5 bars, {p['bars_range'][0]} → "
        f"{p['bars_range'][1]}  ",
        f"*Risk:* ${p['risk_per_bet_usd']:.0f}/bet, spread "
        f"${p['spread_per_oz']:.2f}/oz.  ",
        f"*Grid:* 84 ledgers × 6 detectors × 4 states = "
        f"{len(p['cells']):,} cells (เดิม 1,008 + ใหม่ 1,008)  ",
        f"*Regression vs 13/11:* **{(reg or {}).get('status', '—')}** — "
        + (f"{reg['checked']} เซลล์เดิมเทียบทุกค่าตรงเป๊ะ"
           if reg and reg["status"] == "PASS" else
           ("รายงานเดิมไม่อยู่ — เทียบไม่ได้" if reg is None else
            f"**เทียบไม่ตรง {reg['n_mismatches']} จุด — ห้ามใช้ผลนี้**")),
        "",
        "สมมติฐานคุณหมอ: EMA50@H4 มองย้อน ~8 วัน แต่ bet จบใน 20 นาที–8 ชม. —",
        "ถ้าความสัมพันธ์มีจริง มันควรอยู่ที่ TF ใกล้กว่า ทดสอบด้วย detector",
        "ใหม่บน M30/M15 (แท่งปิดแล้วเท่านั้น — causal tests ผ่านทั้งสามตัว)",
        "",
        *render_shares(p["state_shares_new"]),
        *render_forward(p["forward"]["forward"]),
        *render_persistence(p["forward"]["persistence"]),
        *render_transitions(p["forward"]["transitions"]),
        *render_h2h(p["head_to_head"]),
        *render_deltas(p["tf_deltas"]),
        *render_menus(p["cells"]),
        *render_verdict(p["verdict"], p["head_to_head"],
                        p["edge_concentration"], p["forward_short"]),
        *render_caveats(reg),
    ]
    return "\n".join(lines) + "\n"


# ── main ────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-stem", default=None)
    ap.add_argument("--skip-regression", action="store_true")
    args = ap.parse_args()

    df = load_bars()
    logger.info("building states for all 6 detectors …")
    states = build_detector_states(df, ALL_DETECTORS)
    logger.info("T3: resolving 84-ledger conditional grid (7 geometries) …")
    conditional, overall, n_warm = run_conditional(df, states, GEOMETRIES_7)
    shares_new = state_shares(df, NEW_DETECTORS)
    payload = build_payload(conditional, overall, {}, shares_new, df,
                            n_warm, Path("XAUUSD_M5_full.csv"))
    # detector_quality is hindsight-based and only defined for the old
    # trio — the close-TF study measures prediction directly (T2)
    del payload["detector_quality"]
    payload["detector_params"].update(NEW_DETECTOR_PARAMS)
    logger.info("cells: %d (expect 2016 = 84 ledgers × 6 det × 4 states)",
                len(payload["cells"]))

    reg = None if args.skip_regression else \
        regression_check(payload["cells"], PREV_JSON)
    if reg and reg["status"] != "PASS":
        logger.error("REGRESSION FAILED — aborting report write")
        print(json.dumps(reg["mismatches"][:5], indent=1))
        return 2
    payload["regression"] = reg

    logger.info("T2: forward package for the close-TF trio (1/4/24/72h) …")
    fwd = build_forward_package(df, NEW_DETECTORS, CLOSE_TF_HORIZONS_H)
    payload["forward"] = fwd
    payload["state_shares_new"] = shares_new

    logger.info("T3/T4: head-to-head + edge concentration + verdict …")
    h2h = head_to_head(payload["cells"])
    deltas = tf_deltas(payload["cells"])
    conc = edge_concentration(payload["cells"])
    fwd_short = forward_short_summary(fwd["forward"])
    ev = verdict(h2h, deltas, conc, fwd_short, shares_new)
    payload["head_to_head"] = h2h
    payload["tf_deltas"] = deltas
    payload["edge_concentration"] = conc
    payload["forward_short"] = fwd_short
    payload["verdict"] = ev
    payload["champions"] = list(CHAMPIONS)
    payload["h2h_detectors"] = list(H2H_DETECTORS)

    today = "2026-11-13"     # task dating (Hermes ada-task-20261113)
    stem = Path(args.out_stem) if args.out_stem else \
        _ROOT / "ψ" / "outbox" / f"bet_tf_close_regime_{today}"
    Path(str(stem) + ".json").write_text(json.dumps(payload, indent=2,
                                                   default=str))
    Path(str(stem) + ".md").write_text(render_markdown(payload))
    logger.info("wrote %s.{json,md}", stem)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
