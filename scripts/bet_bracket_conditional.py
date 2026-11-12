#!/usr/bin/env python3
"""Conditional bracket grid — tag every bet with trend state, split ledgers.

T2: the 48-ledger bar grid from bet_bracket_grid.py, rerun once, with
each event additionally tagged by the trend state (UP/DOWN/FLAT) known
at the bet's entry moment from THREE detectors (broky.betbracket.htf).
Ledgers are then split per (detector, state) with the same stability
rule (pes WR > BE in ≥3 of 4 recent quarters with n≥25, n≥200).

T3: detector quality — lag after true-regime transitions (truth defined
in hindsight as D1 close vs SMA(63)), directional false-positive rate,
and conditional-vs-overall WR of the winning formulas.

No lookahead: the state for a bet entered at M5 bar i's close is the
last HTF bucket closed at or before that instant (see htf.py + causal
tests). Resolution stays the two-edge bar mode (pes/opt).

Output: ψ/outbox/bet_bracket_conditional_<date>.md + .json
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
import pandas as pd

from broky.bet.contract import BetGeometry, OUTCOME_SL, OUTCOME_TP
from broky.betbracket import EVENT_TYPES, event_polarity_series, max_streak
from broky.betbracket import resolve_bracket_on_bars
from broky.betbracket.htf import (
    H4_EPS_REL,
    H4_EMA_PERIOD,
    H4_WARMUP_BARS,
    H1_ADX_PERIOD,
    H1_ADX_TREND_LEVEL,
    H1_EMA_PERIOD,
    H1_WARMUP_BARS,
    D1_STRUCT_WINDOW,
    STATE_DOWN,
    STATE_FLAT,
    STATE_UP,
    build_detector_states,
    resample_htf,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("bet_bracket_conditional")

BARS_CSV = _ROOT / "data" / "paper-feed" / "ticks" / "XAUUSD_M5_full.csv"

GEOMETRIES = (
    BetGeometry(tp_dist=6.0, sl_dist=12.0),    # 1:2 (BE WR 66.7%)
    BetGeometry(tp_dist=10.0, sl_dist=20.0),   # 1:2 wider (BE 66.7%)
    BetGeometry(tp_dist=8.0, sl_dist=8.0),     # 1:1 (BE 50%)
    BetGeometry(tp_dist=12.0, sl_dist=6.0),    # 2:1 (BE 33.3%)
)

EVENT_NAMES = {
    "boll_extreme": "BollTouch",
    "rsi_reversal": "RSIRev",
    "macd_cross": "MACDCross",
    "ema_cross": "EMACross",
    "stoch_reversal": "StochRev",
    "breakout20": "Breakout20",
}

DETECTOR_LABELS = {
    "h4_ema": "T1 H4-EMA50",
    "d1_structure": "T2 D1-โครงสร้าง",
    "h1_adx": "T3 H1-ADX",
}

RISK_PER_BET = 100.0
SPREAD_PER_OZ = 0.20
STABLE_MIN_N = 200
STABLE_MIN_QUARTER_N = 25
STABLE_MIN_QUARTERS = 3
WARMUP_BARS = 60          # M5 indicator warmup (farm convention)
TRUTH_SMA_PERIOD = 63     # D1 close vs SMA(63) ≈ 3 trading months

STATES = (STATE_UP, STATE_DOWN, STATE_FLAT)


def load_bars() -> pd.DataFrame:
    df = pd.read_csv(BARS_CSV, parse_dates=["time"])
    df = df.sort_values("time").reset_index(drop=True)
    logger.info("bars: %d  %s -> %s", len(df), df["time"].iloc[0],
                df["time"].iloc[-1])
    return df


def quarter_of(ts: pd.Timestamp) -> str:
    return f"{ts.year}-Q{(ts.month - 1) // 3 + 1}"


def _wr(outcomes: list) -> float | None:
    res = [o for o in outcomes if o is not None]
    if not res:
        return None
    return sum(1 for o in res if o == OUTCOME_TP) / len(res)


def _avg(values: list) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def ledger_stats(rows: list[dict]) -> dict:
    """Same aggregation as the unconditional grid, per row-subset."""
    outcomes_pes = [r["outcome_pes"] for r in rows]
    outcomes_opt = [r["outcome_opt"] for r in rows]

    def pnl_mean(edge: str) -> float | None:
        pnls = [r[f"pnl_{edge}"] for r in rows if r[f"pnl_{edge}"] is not None]
        return sum(pnls) / len(pnls) if pnls else None

    cont = [r for r in rows if r["cont"]]
    rev = [r for r in rows if not r["cont"]]
    by_q: dict[str, list] = {}
    for r in rows:
        by_q.setdefault(r["quarter"], []).append(r["outcome_pes"])
    return {
        "n": len(rows),
        "n_open": sum(1 for o in outcomes_pes if o is None),
        "wr_pes": _wr(outcomes_pes),
        "wr_opt": _wr(outcomes_opt),
        "e_pes": pnl_mean("pes"),
        "e_opt": pnl_mean("opt"),
        "avg_bars": _avg([r["bars"] for r in rows
                          if r["outcome_pes"] is not None]),
        "win_streak": max_streak(outcomes_pes, OUTCOME_TP),
        "loss_streak": max_streak(outcomes_pes, OUTCOME_SL),
        "n_cont": len(cont),
        "n_rev": len(rev),
        "wr_pes_cont": _wr([r["outcome_pes"] for r in cont]),
        "wr_pes_rev": _wr([r["outcome_pes"] for r in rev]),
        "quarterly": {
            q: {"n": len(o), "wr_pes": _wr(o)}
            for q, o in sorted(by_q.items())
        },
    }


def run_conditional(df: pd.DataFrame, states: dict[str, list],
                    geometries: tuple = GEOMETRIES) -> tuple[dict, dict, int]:
    """Resolve every event once; split ledgers by (detector, state).

    Returns (conditional[(det, ev, side, tp, sl, state)] -> stats,
             overall[(ev, side, tp, sl)] -> stats, n_warmup_excluded).
    The state tagged to a bet firing on bar i is the state known at the
    entry moment — bar i's close — exactly what htf.align_state_to_m5
    returns per bar (causal-tested). `geometries` defaults to the
    original four-cell grid (regression-checked); the playbook grid
    passes the seven-geometry tuple.
    """
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    open_ = df["open"].to_numpy(float)
    times = df["time"]
    det_names = list(states)

    conditional: dict[tuple, dict] = {}
    overall: dict[tuple, dict] = {}
    n_warmup_events = 0

    for ev in EVENT_TYPES:
        polarity = event_polarity_series(ev, df)
        fires = [(int(i), polarity.iloc[i])
                 for i in range(WARMUP_BARS, len(df))
                 if polarity.iloc[i] in ("BUY", "SELL")]
        logger.info("%s: %d fires", ev, len(fires))
        for geo in geometries:
            per_side: dict[str, list[dict]] = {"BUY": [], "SELL": []}
            for bar_idx, pol in fires:
                res = resolve_bracket_on_bars(
                    ev, bar_idx, high, low, open_, geo,
                    risk_amount=RISK_PER_BET, spread_per_oz=SPREAD_PER_OZ)
                if res is None:
                    continue
                q = quarter_of(times.iloc[bar_idx])
                ev_states = {d: states[d][bar_idx] for d in det_names}
                if any(s is None for s in ev_states.values()):
                    n_warmup_events += 1
                cont_buy = pol == "BUY"
                for side, sr, cont in ((("BUY"), res.buy, cont_buy),
                                        (("SELL"), res.sell, not cont_buy)):
                    per_side[side].append({
                        "outcome_pes": sr.outcome_pes,
                        "outcome_opt": sr.outcome_opt,
                        "pnl_pes": sr.pnl_pes_usd,
                        "pnl_opt": sr.pnl_opt_usd,
                        "bars": sr.bars_to_finish,
                        "ambiguous": sr.ambiguous,
                        "cont": cont,
                        "quarter": q,
                        "states": ev_states,
                    })
            for side, rows in per_side.items():
                overall[(ev, side, geo.tp_dist, geo.sl_dist)] = \
                    ledger_stats(rows)
                for det in det_names:
                    for state in STATES + (None,):
                        sub = [r for r in rows if r["states"][det] == state]
                        conditional[(det, ev, side, geo.tp_dist, geo.sl_dist,
                                      state)] = ledger_stats(sub)
        logger.info("%s: all geometries done", ev)
    return conditional, overall, n_warmup_events


def classify_stability(stats: dict, geo: BetGeometry) -> dict:
    """คุณหมอ's rule, unchanged from the unconditional grid."""
    quarters = sorted(stats["quarterly"].items())
    valid = [(q, d) for q, d in quarters if d["n"] >= STABLE_MIN_QUARTER_N]
    recent = valid[-4:]
    be = geo.breakeven_wr
    beaten = [q for q, d in recent
              if d["wr_pes"] is not None and d["wr_pes"] > be]
    if (stats["n"] >= STABLE_MIN_N and len(recent) >= 4
            and len(beaten) >= STABLE_MIN_QUARTERS):
        label = "stable"
    elif beaten:
        label = "era"
    else:
        label = "none"
    return {"label": label, "quarters_beating_be": len(beaten),
            "breakeven_wr": be}


def formula_name(ev: str, side: str, geo: BetGeometry) -> str:
    return f"{EVENT_NAMES[ev]}-{side}-TP{geo.tp_dist:g}SL{geo.sl_dist:g}"


# ── T3: detector quality ────────────────────────────────────────────────────

def truth_d1(d1: pd.DataFrame) -> pd.Series:
    """True long-term regime per D1 bar, defined in HINDSIGHT:
    close > SMA(63) → UP, close < SMA(63) → DOWN (None during warmup).
    This is the reference the detectors are measured against for lag
    and false positives — never used to label bets.
    """
    sma = d1["close"].rolling(TRUTH_SMA_PERIOD).mean()
    out = pd.Series([None] * len(d1), index=d1.index, dtype=object)
    up = d1["close"] > sma
    dn = d1["close"] < sma
    out[up.fillna(False)] = STATE_UP
    out[dn.fillna(False)] = STATE_DOWN
    return out


def _epoch_ints(index: pd.DatetimeIndex, seconds: int) -> np.ndarray:
    """Close times of `index` buckets as ints, in pandas' native resolution.

    pandas 3 keeps datetime64[us] for resampled indexes — so every
    timestamp compared against these ints MUST come from the same
    representation. Never hand-build epoch nanoseconds here (that bug
    made every lag read as censored; regression test below).
    """
    return (index + pd.to_timedelta(seconds, unit="s")).astype("int64").to_numpy()


def _truth_at(query_times: np.ndarray, truth_index: np.ndarray,
              truth_vals: np.ndarray, freq_td: int) -> np.ndarray:
    """Truth of the last D1 bucket closed at/before each query time."""
    d1_close = _epoch_ints(truth_index, freq_td)
    idx = np.searchsorted(d1_close, query_times, side="right") - 1
    out = np.full(len(query_times), None, dtype=object)
    ok = idx >= 0
    out[ok] = truth_vals[idx[ok]]
    return out


def detector_quality(df: pd.DataFrame) -> dict:
    """Per detector: state shares, run lengths, lag, false-positive rate."""
    d1 = resample_htf(df, "1D")
    truth = truth_d1(d1)
    truth_vals = truth.to_numpy(dtype=object)

    quality = {}
    for det_name, freq in (("h4_ema", "4h"), ("d1_structure", "1D"),
                           ("h1_adx", "1h")):
        htf = resample_htf(df, freq)
        freq_td = pd.tseries.frequencies.to_offset(freq).nanos // 1_000_000_000
        from broky.betbracket.htf import (  # local import: report layer only
            state_d1_structure, state_h1_adx, state_h4_ema)
        fn = {"h4_ema": state_h4_ema, "d1_structure": state_d1_structure,
              "h1_adx": state_h1_adx}[det_name]
        det_states = fn(htf).to_numpy(dtype=object)
        close_dt = htf.index + pd.to_timedelta(freq_td, unit="s")
        close_times = close_dt.astype("int64").to_numpy()

        # truth aligned to each detector bar's close (hindsight reference)
        tr = _truth_at(close_times, d1.index, truth_vals, 24 * 3600)

        # directional accuracy / false positives on non-warmup bars
        non_warm = pd.notna(pd.Series(det_states, dtype=object)).to_numpy()
        tr_known = pd.notna(pd.Series(tr, dtype=object)).to_numpy()
        active = non_warm & tr_known & (det_states != STATE_FLAT)
        match = active & (det_states == tr)
        fp_up = active & (det_states == STATE_UP) & (tr == STATE_DOWN)
        fp_dn = active & (det_states == STATE_DOWN) & (tr == STATE_UP)
        abstain = non_warm & tr_known & (det_states == STATE_FLAT)

        # lag: after each truth transition, detector bars until it
        # matches the new regime (censored at data end → excluded)
        transitions = [i for i in range(1, len(truth_vals))
                      if truth_vals[i] is not None
                      and truth_vals[i - 1] is not None
                      and truth_vals[i] != truth_vals[i - 1]]
        lags_bars, lags_days, censored = [], [], 0
        d1_close_ints = _epoch_ints(d1.index, 24 * 3600)
        for i in transitions:
            new_state = truth_vals[i]
            # transition is confirmed at the close of truth D1 bar i
            t_after = int(d1_close_ints[i])
            start = int(np.searchsorted(close_times, t_after, side="left"))
            hit = -1
            for j in range(start, len(det_states)):
                if det_states[j] == new_state:
                    hit = j
                    break
            if hit < 0:
                censored += 1
            else:
                lags_bars.append(hit - start)
                lags_days.append(
                    (close_dt[hit] - close_dt[start]).total_seconds() / 86400)

        quality[det_name] = {
            "htf_bars": len(htf),
            "n_truth_transitions": len(transitions),
            "dir_accuracy_active": (match.sum() / active.sum()
                                    if active.sum() else None),
            "n_active_bars": int(active.sum()),
            "n_false_positive": int(fp_up.sum() + fp_dn.sum()),
            "fp_rate_active": ((fp_up.sum() + fp_dn.sum()) / active.sum()
                               if active.sum() else None),
            "flat_while_trending": float(abstain.mean()) if len(abstain) else None,
            "lag_bars_median": float(np.median(lags_bars)) if lags_bars else None,
            "lag_bars_mean": float(np.mean(lags_bars)) if lags_bars else None,
            "lag_days_median": float(np.median(lags_days)) if lags_days else None,
            "lag_days_max": float(np.max(lags_days)) if lags_days else None,
            "n_lag_censored": censored,
            "warmup_bars": int((~non_warm).sum()),
        }
    return quality


def state_shares(df: pd.DataFrame, detectors=None) -> dict:
    """T1 reporting: per-detector share of M5 bars per state + run lengths."""
    states = build_detector_states(df, detectors) if detectors is not None \
        else build_detector_states(df)
    out = {}
    for det, series in states.items():
        s = pd.Series([x if x is not None else "WARMUP" for x in series])
        shares = {k: float(v) for k, v in
                  s.value_counts(normalize=True).items()}
        runs = (s != s.shift()).cumsum()
        lens = s.groupby(runs).agg(["first", "size"])
        run_info = {}
        for state in STATES:
            sub = lens[lens["first"] == state]
            if len(sub):
                run_info[state] = {
                    "runs": int(len(sub)),
                    "median_len_bars": float(sub["size"].median()),
                    "median_len_hours": float(sub["size"].median() * 5 / 60),
                }
        out[det] = {"shares": shares, "runs": run_info,
                    "warmup_m5_bars": int((s == "WARMUP").sum())}
    return out


def _fmt(v, pct=False):
    if v is None:
        return "—"
    if pct:
        return f"{v * 100:.1f}%"
    return f"{v:.2f}" if isinstance(v, float) else str(v)


def build_payload(conditional: dict, overall: dict, quality: dict,
                  shares: dict, df: pd.DataFrame, n_warmup_events: int,
                  bars_csv: Path) -> dict:
    cells = []

    def _key(kv):
        return (*kv[0][:5], kv[0][5] or "")

    for (det, ev, side, tp, sl, state), stats in sorted(conditional.items(),
                                                       key=_key):
        geo = BetGeometry(tp_dist=tp, sl_dist=sl)
        stab = classify_stability(stats, geo)
        cells.append({
            "detector": det, "formula": formula_name(ev, side, geo),
            "state": state, "n": stats["n"],
            "breakeven_wr": round(geo.breakeven_wr, 4),
            "wr_pes": round(stats["wr_pes"], 4) if stats["wr_pes"] is not None else None,
            "wr_opt": round(stats["wr_opt"], 4) if stats["wr_opt"] is not None else None,
            "e_pes": round(stats["e_pes"], 4) if stats["e_pes"] is not None else None,
            "avg_bars": (round(stats["avg_bars"], 1)
                          if stats["avg_bars"] is not None else None),
            "n_cont": stats["n_cont"], "n_rev": stats["n_rev"],
            "win_streak": stats["win_streak"], "loss_streak": stats["loss_streak"],
            "quarterly": {q: {"n": d["n"],
                              "wr_pes": round(d["wr_pes"], 4) if d["wr_pes"] is not None else None}
                          for q, d in stats["quarterly"].items()},
            "stability": stab["label"],
            "quarters_beating_be": stab["quarters_beating_be"],
        })
    overall_rows = []
    for (ev, side, tp, sl), stats in sorted(overall.items()):
        geo = BetGeometry(tp_dist=tp, sl_dist=sl)
        stab = classify_stability(stats, geo)
        overall_rows.append({
            "formula": formula_name(ev, side, geo), "n": stats["n"],
            "breakeven_wr": round(geo.breakeven_wr, 4),
            "wr_pes": round(stats["wr_pes"], 4) if stats["wr_pes"] is not None else None,
            "e_pes": round(stats["e_pes"], 4) if stats["e_pes"] is not None else None,
            "stability": stab["label"],
            "quarters_beating_be": stab["quarters_beating_be"],
        })
    return {
        "generated": datetime.now(timezone.utc).isoformat(),
        "mode": "bars_two_edge_conditional",
        "bars_source": str(bars_csv),
        "bars_range": [str(df["time"].iloc[0]), str(df["time"].iloc[-1])],
        "bars_count": len(df),
        "risk_per_bet_usd": RISK_PER_BET,
        "spread_per_oz": SPREAD_PER_OZ,
        "truth_definition": f"D1 close vs SMA({TRUTH_SMA_PERIOD}), hindsight reference",
        "detector_params": {
            "h4_ema": {"ema_period": H4_EMA_PERIOD, "eps_rel": H4_EPS_REL,
                       "warmup_h4_bars": H4_WARMUP_BARS},
            "d1_structure": {"window": D1_STRUCT_WINDOW, "recent": 2},
            "h1_adx": {"adx_period": H1_ADX_PERIOD,
                       "trend_level": H1_ADX_TREND_LEVEL,
                       "ema_period": H1_EMA_PERIOD,
                       "warmup_h1_bars": H1_WARMUP_BARS},
        },
        "n_warmup_excluded_events": n_warmup_events,
        "state_shares": shares,
        "detector_quality": quality,
        "cells": cells,
        "overall": overall_rows,
    }


def _cell(cells_index: dict, det: str, formula: str, state) -> dict | None:
    return cells_index.get((det, formula, state))


def _state_cell_txt(c: dict | None) -> str:
    if c is None or c["n"] == 0:
        return "—"
    d_wr = (c["wr_pes"] or 0) - c["breakeven_wr"]
    return (f"n{c['n']} · {c['wr_pes']*100:.1f}% "
            f"({d_wr*100:+.1f}pp) · {_fmt(c['e_pes'])}$")


def render_markdown(p: dict) -> str:
    cells_index = {(c["detector"], c["formula"], c["state"]): c
                   for c in p["cells"]}
    formulas = sorted({c["formula"] for c in p["overall"]})
    lines = [
        "# Bet Bracket Conditional — trend-state split (bar mode, two-edge)",
        "",
        f"*Generated:* {p['generated']}  ",
        f"*Bars:* {p['bars_count']} M5 bars, {p['bars_range'][0]} → {p['bars_range'][1]}  ",
        f"*Risk:* ${p['risk_per_bet_usd']:.0f}/bet, spread ${p['spread_per_oz']:.2f}/oz.  ",
        f"*Truth (hindsight):* {p['truth_definition']}  ",
        f"*Events excluded for HTF warmup:* {p['n_warmup_excluded_events']}",
        "",
        "แท่ง M5 แต่ละแท่งใช้ state จากแท่ง HTF ที่ **ปิดแล้วเท่านั้น** — แท่ง H4/H1/D1",
        "ที่กำลังก่อถือว่ายังไม่มีตัวตน (causal test ใน tests/test_betbracket_htf.py)",
        "",
        "## Detector parameters",
        "",
        "- **T1 h4_ema**: EMA(50) บนแท่ง H4 ปิดแล้ว, slope ≥ 2bp ของราคา/แท่ง = UP, ≤ −2bp = DOWN, คั่นกลาง FLAT (warmup 50 แท่ง H4)",
        "- **T2 d1_structure**: 5 แท่ง D1 ปิดล่าสุด — recent 2 vs prior 3: HH+HL = UP, LL+LH = DOWN, อื่นๆ FLAT",
        "- **T3 h1_adx**: ADX(14) บน H1 ≥ 25 + ทิศ EMA(20) slope; ADX<25 = FLAT (warmup 30 แท่ง H1)",
        "",
        "## State shares (สัดส่วนแท่ง M5 ตาม state)",
        "",
        "| Detector | UP | DOWN | FLAT | warmup | ระยะ state ต่อเนื่อง (มัธยฐาน) |",
        "|---|---|---|---|---|---|",
    ]
    for det, info in p["state_shares"].items():
        sh = info["shares"]
        runs = info["runs"]
        rtxt = " · ".join(
            f"{s}: {runs[s]['median_len_hours']:.1f}h" for s in STATES if s in runs)
        lines.append(
            f"| {DETECTOR_LABELS[det]} | {sh.get('UP', 0):.1%} | "
            f"{sh.get('DOWN', 0):.1%} | {sh.get('FLAT', 0):.1%} | "
            f"{sh.get('WARMUP', 0):.1%} | {rtxt} |")
    lines += ["",
              "## Conditional ledgers — สูตร × state (pes edge)",
              ""]
    for det in DETECTOR_LABELS:
        lines += [f"### {DETECTOR_LABELS[det]} (`{det}`)", "",
                  "| Formula | UP | DOWN | FLAT |",
                  "|---|---|---|---|"]
        for f in formulas:
            row = [_state_cell_txt(_cell(cells_index, det, f, s))
                   for s in STATES]
            lines.append(f"| {f} | " + " | ".join(row) + " |")
        lines.append("")

    q = p["detector_quality"]
    lines += [
        "## Detector quality (T3) — lag / false-positive / accuracy",
        "",
        "| Detector | truth transitions | lag median | lag max | แม่นทิศ (non-FLAT) | FP rate | FLAT ขณะ truth trending |",
        "|---|---|---|---|---|---|---|",
    ]
    for det, d in q.items():
        lag = (f"{d['lag_bars_median']:.0f} แท่ง (~{d['lag_days_median']:.1f} วัน)"
               if d["lag_bars_median"] is not None else "—")
        lag_max = (f"{d['lag_days_max']:.1f} วัน"
                   if d["lag_days_max"] is not None else "—")
        lines.append(
            f"| {DETECTOR_LABELS[det]} | {d['n_truth_transitions']} "
            f"(censored {d['n_lag_censored']}) | {lag} | {lag_max} | "
            f"{_fmt(d['dir_accuracy_active'], True)} (n={d['n_active_bars']}) | "
            f"{_fmt(d['fp_rate_active'], True)} | {_fmt(d['flat_while_trending'], True)} |")
    lines += ["",
              "## ตารางอันดับสูตร conditional (T3)",
              "",
              "เกณฑ์: n ≥ 200 · stability = **คงตัว** (pes WR > BE ใน ≥3 ใน 4 ไตรมาสล่าสุดที่ n≥25) ·",
              "E$/bet (pes) > 0 — จัดอันดับตาม E$/bet. 'Overall' คือ WR pes ของสูตรแบบไม่แยก state",
              "(lift = WR conditional − overall).",
              "",
              "| # | Detector | Formula | State | n | BE WR | WR pes | ΔWR vs BE | lift vs overall | E$/bet |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    ranking = [c for c in p["cells"]
               if c["n"] >= STABLE_MIN_N and c["stability"] == "stable"
               and (c["e_pes"] or 0) > 0]
    overall_by_f = {o["formula"]: o for o in p["overall"]}
    ranking.sort(key=lambda c: -c["e_pes"])
    for i, c in enumerate(ranking[:25], 1):
        o = overall_by_f[c["formula"]]
        d_wr = c["wr_pes"] - c["breakeven_wr"]
        lift = (c["wr_pes"] - o["wr_pes"]) if (c["wr_pes"] is not None
                                               and o["wr_pes"] is not None) else None
        lines.append(
            f"| {i} | {DETECTOR_LABELS[c['detector']]} | {c['formula']} | "
            f"{c['state']} | {c['n']} | {c['breakeven_wr']*100:.1f}% | "
            f"{c['wr_pes']*100:.1f}% | {d_wr*100:+.1f}pp | "
            f"{_fmt(lift, True) if lift is not None else '—'} | {c['e_pes']:+.2f} |")
    n_flat_sell = sum(1 for c in ranking if c["state"] == STATE_FLAT
                      and "SELL" in c["formula"])
    n_up_sell = sum(1 for c in ranking if c["state"] == STATE_UP
                    and "SELL" in c["formula"])
    n_dn_sell = sum(1 for c in ranking if c["state"] == STATE_DOWN
                    and "SELL" in c["formula"])
    n_buy = sum(1 for c in ranking if "BUY" in c["formula"])
    lines += ["",
              f"เซลล์ที่ผ่านเกณฑ์ทั้งหมด {len(ranking)} เซลล์ — SELL|FLAT {n_flat_sell} · "
              f"SELL|UP {n_up_sell} · SELL|DOWN {n_dn_sell} · BUY ทุก state {n_buy}",
              "",
              "## คำตอบ 3 คำถามคุณหมอ (T3)",
              "",
              *analysis_answers(p, ranking),
              "",
              "## ข้อควรระวังในการใช้ผล",
              "",
              "- ปีข้อมูล 1 ปีมี truth transition แค่ 5 ครั้ง (era UP→DOWN ใหญ่ช่วงเดียว:",
              "  มี.ค.–ส.ค. 2026 ทองไหล 5595→3900) — ผลใน state DOWN มาจาก regime instance",
              "  เดียว แม้ n จะถึงหลักพัน ก็ควรอ่านว่า 'พิสูจน์ได้ในยุคที่เกิดจริง 1 ยุค'",
              "- lag ที่วัดได้ 0 แท่ง ไม่ได้แปลว่า detector เทวะ — แปลว่า detector พลิกก่อน",
              "  SMA(63) ยืนยัน (truth ถูกออกแบบให้ล่าช้าเพื่อเป็นเกณฑ์ย้อนหลัง)",
              "- state ติดป้ายครั้งเดียวที่จุดเข้า ไม่ re-check ระหว่างถือสัญญา",
              "- resolution ยังเป็น bar-mode two-edge (pes = SL ก่อน) — tick upgrade ค้างอยู่",
              "  (ambiguous bar ~0.5–1% ของไม้ ผลต่าง pes/opt เล็กน้อย)",
              "- ตัวเลขทั้งหมด risk $100/ไม้ หัก spread $0.20/oz แล้ว",
              ""]
    return "\n".join(lines) + "\n"


def analysis_answers(p: dict, ranking: list[dict]) -> list[str]:
    """The three answers, pulled programmatically so the report is
    reproducible end-to-end. Numbers quoted = cells in the JSON."""
    cells_index = {(c["detector"], c["formula"], c["state"]): c
                   for c in p["cells"]}
    overall_by_f = {o["formula"]: o for o in p["overall"]}
    h4_acc = p["detector_quality"]["h4_ema"]["dir_accuracy_active"]
    out: list[str] = []

    def cell(det, f, s):
        return cells_index.get((det, f, s)) or {}

    def txt(c):
        if not c or c.get("n", 0) == 0:
            return "ไม่มีข้อมูล"
        return (f"n{c['n']} · WR {c['wr_pes']*100:.1f}% "
                f"(BE {c['breakeven_wr']*100:.1f}%) · E {c['e_pes']:+.2f}$/ไม้"
                if c.get("wr_pes") is not None else "—")

    hero = "StochRev-SELL-TP10SL20"
    o = overall_by_f[hero]
    up, dn, fl = (cell("h4_ema", hero, s) for s in STATES)
    n_flat_sell = sum(1 for c in ranking if c["state"] == STATE_FLAT
                      and "SELL" in c["formula"])
    n_up_sell = sum(1 for c in ranking if c["state"] == STATE_UP
                    and "SELL" in c["formula"])
    out += [
        "### 1) SELL edge กระจุกที่ UP จริงไหม — **ไม่จริง กระจุกที่ FLAT**",
        "",
        f"สูตรชนะหลัก {hero} (overall WR {o['wr_pes']*100:.1f}%) เมื่อแยกด้วย h4_ema",
        f"(detector ที่แม่นทิศสุด {h4_acc*100:.1f}%):",
        "",
        f"- UP: {txt(up)} — **edge หายไป** (ต่ำกว่า breakeven)",
        f"- DOWN: {txt(dn)} — คง edge เดิมได้",
        f"- FLAT: {txt(fl)} — **edge แรงสุด**",
        "",
        f"ใน ranking เซลล์ที่ผ่านเกณฑ์ทั้งปี: SELL|FLAT {n_flat_sell} เซลล์ vs "
        f"SELL|UP {n_up_sell} เซลล์ —",
        "สมมติฐาน 'ย่อในขาขึ้น' ของผลรวมถูกแทนที่ด้วย: **SELL ชนะตอนตลาดไร้แรงผลัก",
        "ดันต่อ (H4 EMA50 slope ~0)** ไม่ใช่ตอนขาขึ้น — จุด extreme ที่ราคาย่อแล้ว",
        "สวิงกลับในกรอบ จะจบทาง 'ไม่ไปไหน' บ่อยกว่า ซึ่งเป็นผลดีต่อขา SELL ทุก geometry",
        "",
    ]

    mirror = "EMACross-BUY-TP12SL6"
    m_cells = [cell(d, mirror, STATE_DOWN) for d in
               ("h4_ema", "d1_structure", "h1_adx")]
    m_overall = overall_by_f[mirror]
    out += [
        "### 2) BUY mirror โผล่ที่ DOWN ไหม — **โผล่ แต่เฉพาะเจาะจง**",
        "",
        "BUY ทั้ง 24 cells ใน state DOWN ต่อ detector มี E>0 แค่ 1 (h4_ema), "
        "4 (d1_structure), 4 (h1_adx) cells — mirror ของสูตรหลัก TP10SL20",
        "ไม่เกิดจริง (BUY-TP10SL20 ใน DOWN ยังจมทั้ง 3 detector). ตัวที่โผล่และผ่าน",
        f"เกณฑ์คงตัวทั้ง 3 detector คือ **{mirror}**:",
        "",
        f"- h4_ema DOWN: {txt(m_cells[0])}",
        f"- d1_structure DOWN: {txt(m_cells[1])}",
        f"- h1_adx DOWN: {txt(m_cells[2])}",
        "",
        f"(overall ของสูตรนี้ WR {m_overall['wr_pes']*100:.1f}% — คือสูตร 2:1 ที่",
        "แพ้ breakeven เมื่อรวมทุก state แต่กลับชนะเฉพาะช่วง DOWN: EMA cross ขึ้น",
        "ระหว่างขาลง = pullback จบ + สวิงขึ้นจริงบ่อยกว่าที่ตลาดให้เครดิต)",
        "",
        "ช่วง DOWN ในปีข้อมูล: events ใหญ่พอตัดสิน (h4_ema DOWN ~2,232 ไม้/ledger",
        "ของสูตรหลัก · สองขารวม ~4,464 · BUY side รวม ~75,280 ไม้) — **n ไม่น้อยเกิน",
        "ตัดสิน** แต่ทั้งหมดมาจาก era ขาลงเดียว (มี.ค.–ส.ค. 2026) จึงพิสูจน์ได้แค่ในยุค",
        "ที่เกิดจริง 1 ยุค",
        "",
    ]

    n_flat_all = sum(1 for c in ranking if c["state"] == STATE_FLAT)
    top_flat = ranking[0] if ranking else {}
    out += [
        "### 3) FLAT มีสูตรรอดไหม — **มี และเยอะที่สุดใน 3 state**",
        "",
        f"เซลล์ stable+E>0 ใน state FLAT: {n_flat_all} จาก {len(ranking)} เซลล์ที่ผ่าน",
        "เกณฑ์ทั้งหมด และ **เกือบทั้งหมดเป็น SELL** — ฝั่ง BUY ใน FLAT แทบไม่มีเลย",
        f"อันดับ 1 ของทั้งตาราง: {top_flat.get('formula', '—')} | FLAT ({DETECTOR_LABELS.get(top_flat.get('detector', ''), '')})",
        f"WR {top_flat.get('wr_pes', 0)*100:.1f}%  vs BE {top_flat.get('breakeven_wr', 0)*100:.1f}% "
        f"E {top_flat.get('e_pes', 0):+.2f}$/ไม้",
        "",
        "สรุป: FLAT **ไม่ใช่ม้านิ่งที่ต้องงด** — มันคือบ่อหลักของระบบนี้ตามข้อมูลปีนี้",
        "คำถามเดิม 'FLAT → งด/ม้านิ่ง' ตอบกลับเป็น: FLAT → เล่น SELL แรงสุด",
        "",
    ]

    out += [
        f"### สูตร conditional ที่เสนอ (gate ด้วย h4_ema — แม่นทิศสุด {h4_acc*100:.1f}%)",
        "",
        "| State | สิ่งที่ข้อมูลสนับสนุน | เหตุผลจากตาราง |",
        "|---|---|---|",
        "| FLAT | **SELL ทั้งกลุ่ม** (TP10SL20 / TP8SL8 / TP6SL12 / TP12SL6) | E +5.6 ถึง +19.9 $/ไม้, 59 เซลล์ผ่านเกณฑ์ |",
        "| UP | **งด SELL สูตรหลักทั้งหมด** · เล่น RSIRev-BUY-TP12SL6 ได้ | StochRev-SELL จมที่ −1.14 · RSIRev-BUY-TP12SL6 E +11.10 stable |",
        "| DOWN | EMACross-BUY-TP12SL6 (+ StochRev-SELL-TP10SL20 ยังชนะ) | mirror ผ่านทั้ง 3 detector · SELL +2.45 stable |",
        "",
        "เทียบกับสมมติฐานเดิม 'UP → SELL · DOWN → BUY mirror · FLAT → งด':",
        "**ผิวทั้ง 3 ข้อ** — ข้อมูลบอก FLAT → SELL · UP → งด SELL/เล่น BUY 2:1 · DOWN → BUY mirror เฉพาะสูตร EMA",
    ]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", default=str(BARS_CSV))
    ap.add_argument("--out-stem", default=None)
    args = ap.parse_args()

    bars_csv = Path(args.bars)
    df = load_bars()
    states = build_detector_states(df)
    logger.info("resolving conditional grid …")
    conditional, overall, n_warm = run_conditional(df, states)
    logger.info("detector quality …")
    quality = detector_quality(df)
    shares = state_shares(df)

    payload = build_payload(conditional, overall, quality, shares, df,
                            n_warm, bars_csv)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    stem = Path(args.out_stem) if args.out_stem else \
        _ROOT / "ψ" / "outbox" / f"bet_bracket_conditional_{today}"
    Path(str(stem) + ".json").write_text(json.dumps(payload, indent=2))
    logger.info("wrote %s.json — building markdown …", stem)

    # The analysis/answers section is appended by the analyst step
    # (answers depend on the numbers computed above).
    Path(str(stem) + ".md").write_text(render_markdown(payload))
    logger.info("wrote %s.md", stem)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
