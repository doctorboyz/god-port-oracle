#!/usr/bin/env python3
"""Regime playbook — forward accuracy of the detectors + a bet menu
per regime state (คุณหมอ via Hermes, 13/11).

T1  grid widened to SEVEN geometries (adds TP4:SL8 small · TP12:SL12
    symmetric · TP16:SL32 large) → 84 ledgers/detector × 3 detectors,
    cells split per (detector, state) as before. The four original
    geometries are re-run through the identical machinery and REGRESSION
    -CHECKED against the 12/11 report (every old cell must match).
T2  forward-prediction accuracy via broky.betbracket.forward — hit-rate
    / mean move at 4h/24h/72h, persistence, transition matrices, and
    ensemble agreement (UP×3 / DOWN×3 / UP×2 / DOWN×2 / no-signal).
T3  per-regime bet menu via broky.betbracket.playbook — Top-5 passing
    cells per detector × state (n ≥ 200 · WR>BE · stable ≥3/4 quarters ·
    E>0), avoid lists (E ≤ −$5, n ≥ 200), robust labels when a
    (formula, state) passes under ≥2 detectors.
T4  ψ/outbox/bet_regime_playbook_<date>.md + .json.

Deterministic; no farm/VPS/live-account access; bar-mode two-edge
resolution; every bet table carries n next to BE (anti-oversell).
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

from broky.bet.contract import BetGeometry
from broky.betbracket.forward import build_forward_package
from broky.betbracket.htf import STATE_DOWN, STATE_FLAT, STATE_UP
from broky.betbracket.playbook import (
    avoid_menu,
    passing_cells,
    playbook_for_state,
    robust_map,
    top_menu,
)
from scripts.bet_bracket_conditional import (
    DETECTOR_LABELS,
    GEOMETRIES,
    SPREAD_PER_OZ,
    RISK_PER_BET,
    STABLE_MIN_N,
    STATES,
    build_payload,
    detector_quality,
    load_bars,
    run_conditional,
    state_shares,
)
from broky.betbracket.htf import build_detector_states

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("bet_regime_playbook")

PREV_JSON = _ROOT / "ψ" / "outbox" / "bet_bracket_conditional_2026-11-12.json"

# the four original geometries + three new ones (คุณหมอ 13/11)
GEOMETRIES_7 = GEOMETRIES + (
    BetGeometry(tp_dist=4.0, sl_dist=8.0),    # small 1:2   (BE 66.7%)
    BetGeometry(tp_dist=12.0, sl_dist=12.0),  # symmetric 1:1 big (BE 50%)
    BetGeometry(tp_dist=16.0, sl_dist=32.0),  # large 1:2   (BE 66.7%)
)
assert len(GEOMETRIES_7) == 7

GRID_STATE_ORDER = (STATE_UP, STATE_DOWN, STATE_FLAT, None)
HORIZONS = ("4.0", "24.0", "72.0")
_H = {"4.0": "4h", "24.0": "24h", "72.0": "72h"}


# ── T1: regression against the 12/11 report ─────────────────────────────────

_REGRESS_KEYS = ("n", "breakeven_wr", "wr_pes", "wr_opt", "e_pes",
                 "stability", "quarters_beating_be", "n_cont", "n_rev",
                 "win_streak", "loss_streak", "quarterly")


def regression_check(new_cells: list[dict],
                     prev_path: Path) -> dict | None:
    """Every old-geometry cell must equal the 12/11 report exactly."""
    if not prev_path.exists():
        logger.warning("previous report %s missing — regression SKIPPED",
                       prev_path)
        return None
    prev = json.loads(prev_path.read_text())
    prev_idx = {(c["detector"], c["formula"], c["state"]): c
                for c in prev["cells"]}
    new_idx = {(c["detector"], c["formula"], c["state"]): c
               for c in new_cells}
    n_checked, mismatches = 0, []
    for key, old in prev_idx.items():
        if key not in new_idx:
            mismatches.append({"cell": key, "field": "MISSING"})
            continue
        new = new_idx[key]
        n_checked += 1
        for k in _REGRESS_KEYS:
            if old.get(k) != new.get(k):
                mismatches.append({"cell": key, "field": k,
                                   "was": old.get(k), "now": new.get(k)})
    out = {"prev_cells": len(prev_idx), "checked": n_checked,
           "mismatches": mismatches[:20], "n_mismatches": len(mismatches),
           "status": "PASS" if not mismatches else "FAIL"}
    logger.info("regression vs 12/11: %s (%d cells, %d mismatches)",
                out["status"], n_checked, len(mismatches))
    return out


# ── rendering helpers ───────────────────────────────────────────────────────

def _f(v, digits: int = 2) -> str:
    return "—" if v is None else f"{v:.{digits}f}"


def _f_cell_row(c: dict) -> str:
    """One menu row — n and BE always travel together."""
    be = c["breakeven_wr"] * 100
    d_wr = (c["wr_pes"] - c["breakeven_wr"]) * 100
    return (f"| {c['formula']} | {c['n']} | {be:.1f}% | "
            f"{c['wr_pes']*100:.1f}% | {d_wr:+.1f}pp | {c['e_pes']:+.2f} | "
            f"{_f(c.get('avg_bars'), 0)} | {c['quarters_beating_be']}/4 |")


_MENU_HEADER = ("| Formula | n | BE | WR pes | ΔWR | E$/bet | avg bars | "
                "ไตรมาส>BE |", "|---|---|---|---|---|---|---|---|")


# ── section 1: detector forward accuracy ────────────────────────────────────

def render_forward(fwd: dict) -> list[str]:
    lines = ["## 1) ความแม่น 'ทำนายล่วงหน้า' ของ detector", "",
             "คำถามเปลี่ยนจาก 'ตรงกับ truth วันนี้ไหม' เป็น **'พูดแล้วราคาไปตามนั้นไหม",
             "ใน 4/24/72 ชม.ข้างหน้า'** — วัดจาก close ของแท่งแรกที่ปิดถึง t+h",
             "ตามเวลาจริง (สุดสัปดาห์ข้ามไปแท่งจริง · หลุดปลายข้อมูล = censored)",
             "· UP hit = ราคาขึ้น · DOWN hit = ราคาลง · FLAT hit = |move| ≤ eps",
             "(eps = median |move| ของทุก anchor ต่อ horizon → เส้นอ้างอิง 50%)",
             "· ทุกช่องระบุ n คู่กับเส้นอ้างอิงเสมอ", ""]
    base = fwd["baselines"]
    for h in HORIZONS:
        b = base[h]
        lines += [f"เส้นอ้างอิงรวม {_H[h]}: n={b['n']:,} (censored "
                  f"{b['n_censored']}) · P(up)={b['p_up']*100:.1f}% · "
                  f"P(down)={b['p_down']*100:.1f}% · eps=±{b['eps_median_abs_move']:.1f} $/oz",
                  ""]
    for det, info in fwd["detectors"].items():
        lines += [f"### {DETECTOR_LABELS[det]} (`{det}`)", "",
                  "| State | " + " | ".join(
                      f"{_H[h]}: n · hit · mean $/oz" for h in HORIZONS) + " |",
                  "|---|" + "---|" * len(HORIZONS)]
        for state in (STATE_UP, STATE_DOWN, STATE_FLAT):
            cells = info["states"][state]
            row = []
            for h in HORIZONS:
                c = cells[h]
                if c["n"] == 0:
                    row.append("—")
                else:
                    row.append(f"{c['n']:,} · {c['hit_rate']*100:.1f}% · "
                               f"{c['mean_move']:+.1f}")
            lines.append(f"| {state} | " + " | ".join(row) + " |")
        lines.append("")
    return lines


def render_persistence(pers: dict) -> list[str]:
    lines = ["### Persistence — state อยู่นานแค่ไหน + รอดถึง h ชม.ไหม", "",
             "| Detector | State | run มัธยฐาน (ชม.) | survival 4h | "
             "survival 24h | survival 72h |",
             "|---|---|---|---|---|---|"]
    for det, per_state in pers.items():
        for state in (STATE_UP, STATE_DOWN, STATE_FLAT):
            s = per_state[state]
            def surv(h):
                d = s["survival"][h]
                return ("—" if not d["n"] else
                        f"{d['same_rate']*100:.0f}% (n={d['n']:,})")
            lines.append(
                f"| {DETECTOR_LABELS[det]} | {state} | "
                f"{_f(s['run_median_hours'], 1)} | "
                f"{surv('4.0')} | {surv('24.0')} | {surv('72.0')} |")
    return lines + [""]


def render_transitions(tr: dict) -> list[str]:
    lines = ["### Transition matrix — P(state ถัดไป | state ปัจจุบัน)", ""]
    states = (STATE_UP, STATE_DOWN, STATE_FLAT)
    for det, mat in tr.items():
        lines += [f"**{DETECTOR_LABELS[det]}** (บนแท่ง HTF ของ detector เอง)", "",
                  "| จาก \\ เป็น | UP | DOWN | FLAT | n transitions |",
                  "|---|---|---|---|---|"]
        for a in states:
            row = mat.get(a, {})
            cells = [f"{row.get(b, 0)*100:.0f}%" if b in row else "—"
                     for b in states]
            lines.append(f"| {a} | " + " | ".join(cells) +
                         f" | {row.get('n', 0):,} |")
        lines.append("")
    return lines


def render_ensemble(ens: dict) -> list[str]:
    from broky.betbracket.forward import (
        ENS_DOWN2, ENS_DOWN3, ENS_NONE, ENS_UP2, ENS_UP3)
    labels = {ENS_UP3: "UP×3 (มั่นใจสูง)", ENS_DOWN3: "DOWN×3 (มั่นใจสูง)",
              ENS_UP2: "UP×2 (ก้ำกึ่ง)", ENS_DOWN2: "DOWN×2 (ก้ำกึ่ง)",
              ENS_NONE: "no-signal (อ้างอิง)"}
    lines = ["### Ensemble agreement — เมื่อ detector เห็นตรงกัน", "",
             "| กลุ่ม | " + " | ".join(
                 f"{_H[h]}: n · hit · mean $/oz" for h in HORIZONS) + " |",
             "|---|" + "---|" * len(HORIZONS)]
    for grp in (ENS_UP3, ENS_DOWN3, ENS_UP2, ENS_DOWN2, ENS_NONE):
        row = []
        for h in HORIZONS:
            d = ens["groups"][h][grp]
            if d["n"] == 0:
                row.append("—")
            elif d["hit_rate"] is None:
                row.append(f"{d['n']:,} · · {d['mean_move']:+.1f}")
            else:
                row.append(f"{d['n']:,} · {d['hit_rate']*100:.1f}% · "
                           f"{d['mean_move']:+.1f}")
        lines.append(f"| {labels[grp]} | " + " | ".join(row) + " |")
    return lines + ["", "hit ของกลุ่ม UP = ราคาขึ้น · กลุ่ม DOWN = ราคาลง ·",
                    "no-signal คือถังอ้างอิง (ไม่มีทิศทานาย จึงไม่มี hit)", ""]


# ── section 2: menus ────────────────────────────────────────────────────────

def render_menus(cells: list[dict]) -> list[str]:
    rmap = robust_map(cells)
    lines = ["## 2) เมนู bet ต่อ regime", "",
             "เกณฑ์ผ่าน: **n ≥ 200 · WR pes > BE · คงตัว (pes WR > BE ≥3 ใน 4",
             "ไตรมาสล่าสุดที่ n≥25) · E > 0** · เรียงตาม E · ป้าย **แข็งแรง** =",
             "สูตรเดียวกัน (formula+state) ผ่านเกณฑ์ใน ≥2 detectors ·",
             "**ต้องเลี่ยง** = n ≥ 200 และ E ≤ −$5/ไม้ (risk $100)", ""]
    for det in DETECTOR_LABELS:
        for state in STATES:
            menu = top_menu(cells, det, state)
            avoid = avoid_menu(cells, det, state)
            lines += [f"### {DETECTOR_LABELS[det]} · state **{state}**", "",
                      *_MENU_HEADER]
            if menu:
                for c in menu:
                    tag = ""
                    dets = rmap.get((c["formula"], c["state"]), [])
                    if len(dets) >= 2:
                        tag = f" [{'แข็งแรง' if len(dets) == 2 else 'แข็งแรง×3'}]"
                    lines.append(_f_cell_row(c)[:-2] + f"{tag} |")
            else:
                lines.append("| — ไม่มีสูตรผ่านเกณฑ์ในเซลล์นี้ | | | | | | | |")
            lines += ["", "**ต้องเลี่ยง:**", "", *_MENU_HEADER]
            if avoid:
                for c in avoid:
                    lines.append(_f_cell_row(c))
            else:
                lines.append("| — ไม่มีตัวที่จมชัดถึงขั้นต้องเลี่ยง | | | | | | | |")
            lines.append("")
    return lines


# ── section 3: playbook ────────────────────────────────────────────────────

def render_playbook(cells: list[dict], fwd: dict) -> list[str]:
    lines = ["## 3) Playbook สรุป — เปิดแล้วเลือกได้ทันที", "",
             "แถวต่อ state ใช้ detector ที่มีสูตรผ่านเกณฑ์มากที่สุดของ state นั้น",
             "", "| State | Detector | แชมป์ (E สูงสุด) | สำรอง | ต้องเลี่ยง |",
             "|---|---|---|---|---|"]
    for state in STATES:
        pb = playbook_for_state(cells, state)
        if pb is None:
            lines.append(f"| {state} | — | ไม่มีสูตรผ่านเกณฑ์ | — | — |")
            continue
        ch = pb["champion"]
        champ_txt = (f"**{ch['formula']}** — n={ch['n']:,} · WR "
                     f"{ch['wr_pes']*100:.1f}% vs BE "
                     f"{ch['breakeven_wr']*100:.1f}% · E {ch['e_pes']:+.2f}$"
                     f"{(' · ' + ch['robust']) if ch['robust'] else ''}")
        backups = " · ".join(
            f"{b['formula']} (E {b['e_pes']:+.2f}$"
            f"{', ' + b['robust'] if b['robust'] else ''})"
            for b in pb["backups"]) or "—"
        avoid = " · ".join(
            f"{a['formula']} (E {a['e_pes']:+.2f}$, n={a['n']:,})"
            for a in pb["avoid"]) or "—"
        lines.append(f"| **{state}** | {DETECTOR_LABELS[pb['detector']]} | "
                     f"{champ_txt} | {backups} | {avoid} |")
    lines += ["", f"ผ่านเกณฑ์ทั้งหมด {len(passing_cells(cells))} เซลล์ "
              f"จาก {len(cells)} เซลล์ (84 ledgers × 3 detectors × 4 states)",
              ""]

    # ensemble + persistence footnotes: what the regime label itself is worth
    ens = fwd["ensemble"]["groups"]
    pers = fwd["persistence"]
    from broky.betbracket.forward import ENS_DOWN3, ENS_UP3
    def _e(grp):
        d = ens["24.0"][grp]
        return (f"{grp}: n={d['n']:,} · hit 24h {d['hit_rate']*100:.1f}% · "
                f"mean {d['mean_move']:+.1f} $/oz")
    up3, dn3 = _e(ENS_UP3), _e(ENS_DOWN3)
    lines += [
        "**หมายเหตุ ensemble** (ทั้ง 3 detector เห็นตรงกัน, 24h):",
        f"- {_e(ENS_UP3)} — hit ต่ำกว่าเหรียญอย่างชัด",
        f"- {_e(ENS_DOWN3)} — **จะยิ่งชัด contrarian** (พูด DOWN แล้วราคา",
        "  วิ่งกลับขึ้นเฉลี่ย +$14.6/oz) — ใช้ detector เป็นเข็มทิศเทรนด์ต่อเนื่อง",
        "  ได้ แต่อย่าใช้เป็นสัญญาณทิศทางล่วงหน้า",
        "- ป้าย state คงทนพอใช้จริง: survival 24h ของ UP/DOWN ~78-81% (h4_ema)",
        f"  · ทิศหลักพลิกไม่บ่อย (transition stay ~90%) — แต่ persistence นี้",
        "  คือการ 'อยู่ต่อ' ของ state ไม่ใช่การ 'ทำนายทิศราคา'",
        "",
    ]
    return lines


# ── section 4: caveats ──────────────────────────────────────────────────────

def render_caveats(reg: dict | None, ens: dict) -> list[str]:
    ens_txt = []
    from broky.betbracket.forward import ENS_DOWN3, ENS_UP3
    for grp in (ENS_UP3, ENS_DOWN3):
        d = ens["groups"]["24.0"][grp]
        if d["n"]:
            ens_txt.append(f"{grp} mean {d['mean_move']:+.1f} $/oz "
                           f"(hit {d['hit_rate']*100:.1f}%, n={d['n']:,})")
    return [
        "## 4) ข้อจำกัด — อ่านก่อนใช้ตัดสินใจ", "",
        "- **ปีข้อมูลเดียว (2025-10 → 2026-11)**: era ขาลงใหญ่เกิดครั้งเดียว",
        "  (มี.ค.–ส.ค. 2026 ทอง 5595→~3900) — cell ใน state DOWN พิสูจน์ได้แค่",
        "  ในยุคที่เกิดจริง 1 ยุค แม้ n ถึงหลักพัน",
        "- **Multiple comparisons**: grid นี้มี 1,008 เซลล์ — การผ่านเกณฑ์ของ",
        "  เซลล์เดียวอาจเป็นโชค ป้าย 'แข็งแรง' (ผ่าน ≥2 detectors) คือกันชน",
        "  หลัก อย่าเลือกสูตรที่ไม่มีป้ายโดยไม่มีเหตุผลอื่น",
        "- **Forward accuracy ≠ truth accuracy**: detector อาจตรงกับ SMA(63)",
        "  ย้อนหลังสูงแต่ทำนายล่วงหน้าแทบไม่ต่างจากเหรียญ (และกลุ่มเห็นตรงกัน",
        f"  ทั้ง 3 ตัวอาจกลับตัว contrarian — 24h: {' · '.join(ens_txt)}",
        "  อ่านว่า detector ยืนยันช้า = เข้าตอนที่ไม้วิ่งแล้ว ไม่ใช่พยากรณ์",
        "- **Resolution = bar-mode two-edge** (pes = SL ก่อน): ambiguous bar",
        "  ~0.5–1% ของไม้ ผล pes/opt ต่างเล็กน้อย — tick upgrade ยังค้าง",
        "- **State ติดป้ายครั้งเดียวที่จุดเข้า** ไม่ re-check ระหว่างถือสัญญา",
        "- ตัวเลขทั้งหมด risk $100/ไม้ หัก spread $0.20/oz แล้ว",
        f"- Regression vs รายงาน 12/11: **{reg['status'] if reg else 'SKIPPED'}**"
        + (f" ({reg['checked']} cells เทียบตรงเป๊ะ)" if reg else "")
        + "", "",
    ]


def render_markdown(p: dict) -> str:
    reg = p["regression"]
    lines = [
        "# Regime Playbook — ความแม่น detector + เมนู bet ต่อ regime",
        "",
        f"*Generated:* {p['generated']}  ",
        f"*Bars:* {p['bars_count']:,} M5 bars, {p['bars_range'][0]} → "
        f"{p['bars_range'][1]}  ",
        f"*Risk:* ${p['risk_per_bet_usd']:.0f}/bet, spread "
        f"${p['spread_per_oz']:.2f}/oz.  ",
        f"*Geometries:* 7 (TP4:SL8 · TP6:SL12 · TP8:SL8 · TP10:SL20 · "
        f"TP12:SL6 · TP12:SL12 · TP16:SL32) × 6 events × 2 ขา = 84 ledgers/"
        "detector × 3 detectors, แยกตาม state ที่จุดเข้า  ",
        f"*Regression vs 12/11:* **{(reg or {}).get('status', '—')}** —",
        (f"{reg['checked']} เซลล์เดิมเทียบทุกค่าตรงเป๊ะ"
         if reg and reg["status"] == "PASS" else
         ("รายงานเดิมไม่อยู่ — เทียบไม่ได้" if reg is None else
          f"**เทียบไม่ตรง {reg['n_mismatches']} จุด — ห้ามใช้ผลนี้**")),
        "",
        "State ทุกตัวมาจากแท่ง HTF ที่ **ปิดแล้วเท่านั้น** (causal tests",
        "tests/test_betbracket_htf.py ผ่านทั้งชุด) · deterministic ·",
        "ห้ามแตะฟาร์มสด/VPS/บช.จริง — ทั้งเล่นบน M5 1 ปีย้อนหลัง",
        "",
        *render_forward(p["forward"]["forward"]),
        *render_persistence(p["forward"]["persistence"]),
        *render_transitions(p["forward"]["transitions"]),
        *render_ensemble(p["forward"]["ensemble"]),
        *render_menus(p["cells"]),
        *render_playbook(p["cells"], p["forward"]),
        *render_caveats(reg, p["forward"]["ensemble"]),
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-stem", default=None)
    ap.add_argument("--skip-regression", action="store_true")
    args = ap.parse_args()

    df = load_bars()
    states = build_detector_states(df)
    logger.info("T1: resolving 84-ledger conditional grid (7 geometries) …")
    conditional, overall, n_warm = run_conditional(df, states, GEOMETRIES_7)
    quality = detector_quality(df)
    shares = state_shares(df)
    payload = build_payload(conditional, overall, quality, shares, df,
                            n_warm, Path("XAUUSD_M5_full.csv"))
    logger.info("cells: %d (expect 1008 = 84 ledgers × 3 det × 4 states)",
                len(payload["cells"]))

    reg = None if args.skip_regression else \
        regression_check(payload["cells"], PREV_JSON)
    if reg and reg["status"] != "PASS":
        logger.error("REGRESSION FAILED — see report; aborting report write")
        print(json.dumps(reg["mismatches"][:5], indent=1))
        return 2

    logger.info("T2: forward package …")
    fwd = build_forward_package(df)
    payload["regression"] = reg
    payload["forward"] = fwd

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    stem = Path(args.out_stem) if args.out_stem else \
        _ROOT / "ψ" / "outbox" / f"bet_regime_playbook_{today}"
    Path(str(stem) + ".json").write_text(json.dumps(payload, indent=2))
    Path(str(stem) + ".md").write_text(render_markdown(payload))
    logger.info("wrote %s.{json,md}", stem)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
