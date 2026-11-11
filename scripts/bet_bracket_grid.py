#!/usr/bin/env python3
"""Deep bracket grid — 48 theory-free ledgers over the full M5 year.

At every indicator event, open BUY and SELL ledgers with the SAME fixed
geometry and resolve on closed M5 bars (two-edge: pes = SL-first booking
on ambiguous bars, opt = TP-first). Tick-based resolution replaces this
bar mode once the deep tick archive finishes downloading — the bar grid
is the immediately-runnable answer to คุณหมอ's question.

Ledgers: 6 event types × 4 geometries × 2 sides = 48.
Per ledger: n, WR pes/opt, E$/bet (risk $100, spread $0.20/oz booked),
avg bars to finish, win/loss streaks, quarterly WR, CONT/REV split.
Stability rule (คุณหมอ 2026-11-12): pes WR > breakeven in ≥3 of the 4
most recent calendar quarters (quarters with n ≥ 25) and n ≥ 200 →
"สูตรคงตัว" (stable); beats BE only in some quarters → "ของยุคสมัย".

Output: ψ/outbox/bet_bracket_deep_<date>.md + .json
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
from broky.betbracket import (
    EVENT_TYPES,
    event_polarity_series,
    max_streak,
    resolve_bracket_on_bars,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("bet_bracket_grid")

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

RISK_PER_BET = 100.0
SPREAD_PER_OZ = 0.20
STABLE_MIN_N = 200
STABLE_MIN_QUARTER_N = 25
STABLE_MIN_QUARTERS = 3
# Indicator warmup (farm convention MIN_HISTORY_BARS): EMA50/RSI/MACD
# values in the first ~60 bars are seed-biased — fires there are noise.
WARMUP_BARS = 60


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


def ledger_stats(side_results: list[dict]) -> dict:
    """Aggregate one ledger's per-event side records."""
    outcomes_pes = [r["outcome_pes"] for r in side_results]
    outcomes_opt = [r["outcome_opt"] for r in side_results]
    resolved_pes = [o for o in outcomes_pes if o is not None]

    def pnl_mean(edge: str) -> float | None:
        pnls = [r[f"pnl_{edge}"] for r in side_results
                if r[f"pnl_{edge}"] is not None]
        return sum(pnls) / len(pnls) if pnls else None

    cont = [r for r in side_results if r["cont"]]
    rev = [r for r in side_results if not r["cont"]]
    return {
        "n": len(side_results),
        "n_open": sum(1 for o in outcomes_pes if o is None),
        "wr_pes": _wr(outcomes_pes),
        "wr_opt": _wr(outcomes_opt),
        "e_pes": pnl_mean("pes"),
        "e_opt": pnl_mean("opt"),
        "avg_bars": _avg([r["bars"] for r in side_results if r["outcome_pes"] is not None]),
        "win_streak": max_streak(outcomes_pes, OUTCOME_TP),
        "loss_streak": max_streak(outcomes_pes, OUTCOME_SL),
        "n_ambiguous": sum(1 for r in side_results if r["ambiguous"]),
        "n_cont": len(cont),
        "n_rev": len(rev),
        "wr_pes_cont": _wr([r["outcome_pes"] for r in cont]),
        "wr_pes_rev": _wr([r["outcome_pes"] for r in rev]),
        "quarterly": {},          # filled by caller
    }


def run_grid(df: pd.DataFrame) -> tuple[dict, dict]:
    """Resolve every event × geometry × side. Returns (records, pair_stats)."""
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    open_ = df["open"].to_numpy(float)
    times = df["time"]

    records: dict[tuple, dict] = {}
    pair_stats: dict[tuple, dict] = {}
    for ev in EVENT_TYPES:
        polarity = event_polarity_series(ev, df)
        fires = [(int(i), polarity.iloc[i])
                 for i in range(WARMUP_BARS, len(df))
                 if polarity.iloc[i] in ("BUY", "SELL")]
        logger.info("%s: %d fires", ev, len(fires))
        for geo in GEOMETRIES:
            key = (ev, geo.tp_dist, geo.sl_dist)
            buys: list[dict] = []
            sells: list[dict] = []
            pairs: list[str] = []
            for bar_idx, pol in fires:
                res = resolve_bracket_on_bars(
                    ev, bar_idx, high, low, open_, geo,
                    risk_amount=RISK_PER_BET, spread_per_oz=SPREAD_PER_OZ)
                if res is None:
                    continue
                q = quarter_of(times.iloc[bar_idx])
                cont_buy = pol == "BUY"
                buys.append({
                    "outcome_pes": res.buy.outcome_pes,
                    "outcome_opt": res.buy.outcome_opt,
                    "pnl_pes": res.buy.pnl_pes_usd,
                    "pnl_opt": res.buy.pnl_opt_usd,
                    "bars": res.buy.bars_to_finish,
                    "ambiguous": res.buy.ambiguous,
                    "cont": cont_buy,
                    "quarter": q,
                })
                sells.append({
                    "outcome_pes": res.sell.outcome_pes,
                    "outcome_opt": res.sell.outcome_opt,
                    "pnl_pes": res.sell.pnl_pes_usd,
                    "pnl_opt": res.sell.pnl_opt_usd,
                    "bars": res.sell.bars_to_finish,
                    "ambiguous": res.sell.ambiguous,
                    "cont": not cont_buy,
                    "quarter": q,
                })
                pairs.append(res.pair_class("pes"))

            for side, rows in (("BUY", buys), ("SELL", sells)):
                stats = ledger_stats(rows)
                by_q: dict[str, list] = {}
                for r in rows:
                    by_q.setdefault(r["quarter"], []).append(r["outcome_pes"])
                stats["quarterly"] = {
                    q: {"n": len(o), "wr_pes": _wr(o)}
                    for q, o in sorted(by_q.items())
                }
                records[(ev, side, geo.tp_dist, geo.sl_dist)] = stats

            pair_stats[key] = {
                "n": len(pairs),
                "buy_win": pairs.count("buy_win"),
                "sell_win": pairs.count("sell_win"),
                "both_win": pairs.count("both_win"),
                "both_lose": pairs.count("both_lose"),
                "open": pairs.count("open"),
            }
        logger.info("%s: all geometries done", ev)
    return records, pair_stats


def formula_name(ev: str, side: str, geo: BetGeometry) -> str:
    return f"{EVENT_NAMES[ev]}-{side}-TP{geo.tp_dist:g}SL{geo.sl_dist:g}"


def classify_stability(stats: dict, geo: BetGeometry) -> dict:
    """คุณหมอ's rule: pes WR > BE in ≥3 of the last 4 valid quarters, n≥200.

    Valid quarter = n ≥ 25. 'era' label when BE is beaten in some but not
    enough quarters — a strategy that only wins in certain regimes.
    """
    quarters = sorted(stats["quarterly"].items())
    valid = [(q, d) for q, d in quarters if d["n"] >= STABLE_MIN_QUARTER_N]
    recent = valid[-4:]
    be = geo.breakeven_wr
    beaten = [q for q, d in recent if d["wr_pes"] is not None and d["wr_pes"] > be]
    if stats["n"] >= STABLE_MIN_N and len(recent) >= 4 and len(beaten) >= STABLE_MIN_QUARTERS:
        label = "stable"
    elif beaten:
        label = "era"
    else:
        label = "none"
    return {
        "label": label,
        "quarters_beating_be": len(beaten),
        "quarters_evaluated": [q for q, _ in recent],
        "breakeven_wr": be,
    }


def build_report(records: dict, pair_stats: dict, df: pd.DataFrame,
                 bars_csv: Path, tick_note: str) -> tuple[dict, str]:
    ledgers = []
    for (ev, side, tp, sl), stats in sorted(records.items()):
        geo = BetGeometry(tp_dist=tp, sl_dist=sl)
        stab = classify_stability(stats, geo)
        ledgers.append({
            "formula": formula_name(ev, side, geo),
            "event": ev, "side": side,
            "geometry": f"TP{tp:g}/SL{sl:g}",
            "breakeven_wr": round(geo.breakeven_wr, 4),
            **{k: (round(v, 4) if isinstance(v, float) else v)
               for k, v in stats.items() if k != "quarterly"},
            "quarterly": {
                q: {"n": d["n"],
                    "wr_pes": round(d["wr_pes"], 4) if d["wr_pes"] is not None else None}
                for q, d in stats["quarterly"].items()
            },
            "stability": stab["label"],
            "quarters_beating_be": stab["quarters_beating_be"],
        })

    stable = [l for l in ledgers if l["stability"] == "stable"]
    stable.sort(key=lambda l: -((l["wr_pes"] or 0) - l["breakeven_wr"]))

    payload = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "mode": "bars_two_edge",
        "bars_source": str(bars_csv),
        "bars_range": [str(df["time"].iloc[0]), str(df["time"].iloc[-1])],
        "bars_count": len(df),
        "risk_per_bet_usd": RISK_PER_BET,
        "spread_per_oz": SPREAD_PER_OZ,
        "tick_note": tick_note,
        "ledgers": ledgers,
        "pair_stats": {
            f"{ev}-TP{tp:g}SL{sl:g}": stats
            for (ev, tp, sl), stats in sorted(pair_stats.items())
        },
    }
    return payload, _render_markdown(payload, stable)


def _fmt(v, pct=False):
    if v is None:
        return "—"
    if pct:
        return f"{v * 100:.1f}%"
    return f"{v:.2f}" if isinstance(v, float) else str(v)


def _render_markdown(p: dict, stable: list[dict]) -> str:
    lines = [
        "# Bet Bracket Deep — theory-free grid (bar mode, two-edge)",
        "",
        f"*Generated:* {p['generated']}  ",
        f"*Bars:* {p['bars_count']} M5 bars, {p['bars_range'][0]} → {p['bars_range'][1]}  ",
        f"*Mode:* closed-bar resolution, two ambiguity edges — **pes** books SL first (conservative), **opt** books TP first.  ",
        f"*Risk:* ${p['risk_per_bet_usd']:.0f}/bet, spread ${p['spread_per_oz']:.2f}/oz booked at resolution.  ",
        f"*Tick archive:* {p['tick_note']}",
        "",
        "Every event fires BOTH a BUY and a SELL ledger with the same fixed geometry —",
        "no directional theory enters the resolution. A formula 'wins' when its ledger's",
        "WR beats the geometry's breakeven WR across quarters.",
        "",
        "## 48 ledgers",
        "",
        "| Formula | n | open | BE WR | WR pes | WR opt | E$/bet pes | E$/bet opt |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for l in p["ledgers"]:
        lines.append(
            f"| {l['formula']} | {l['n']} | {l['n_open']} | "
            f"{l['breakeven_wr']*100:.1f}% | {_fmt(l['wr_pes'], True)} | "
            f"{_fmt(l['wr_opt'], True)} | {_fmt(l['e_pes'])} | {_fmt(l['e_opt'])} |")
    lines += [
        "",
        "### Per-ledger detail (pes edge) — bars to finish, streaks, CONT/REV, quarters",
        "",
        "| Formula | avg bars | W-strk | L-strk | ambig | n CONT (WR) | n REV (WR) | quarterly WR pes |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for l in p["ledgers"]:
        qtxt = " · ".join(
            f"{q}:{_fmt(d['wr_pes'], True)}(n{d['n']})"
            for q, d in l["quarterly"].items())
        lines.append(
            f"| {l['formula']} | {_fmt(l['avg_bars'])} | {l['win_streak']} | "
            f"{l['loss_streak']} | {l['n_ambiguous']} | "
            f"{l['n_cont']} ({_fmt(l['wr_pes_cont'], True)}) | "
            f"{l['n_rev']} ({_fmt(l['wr_pes_rev'], True)}) | {qtxt} |")
    lines += ["", "### Pair outcomes (both ledgers of one event, pes edge)", "",
              "| Event-geometry | n | BUY win | SELL win | both win | both lose | open |",
              "|---|---|---|---|---|---|---|"]
    for key, s in p["pair_stats"].items():
        lines.append(f"| {key} | {s['n']} | {s['buy_win']} | {s['sell_win']} | "
                     f"{s['both_win']} | {s['both_lose']} | {s['open']} |")
    lines += [
        "",
        "## สูตรที่ 'มักจะ win' — ranking (คุณหมอ's stability rule)",
        "",
        "คงตัว = pes WR > breakeven ใน ≥3 ใน 4 ไตรมาสล่าสุดที่มี n≥25 และ n≥200 ·",
        "ของยุคสมัย = ชนะ BE เฉพาะบางไตรมาส",
        "",
        "| # | Formula | n | BE WR | WR pes | WR opt | ΔWR vs BE | E$/bet pes | ไตรมาสที่ชนะ BE | ป้าย |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    # rank candidates: n>=200, sort by WR pes minus BE
    cands = [l for l in p["ledgers"] if l["n"] >= STABLE_MIN_N]
    cands.sort(key=lambda l: -((l["wr_pes"] or 0) - l["breakeven_wr"]))
    for i, l in enumerate(cands[:20], 1):
        d_wr = (l["wr_pes"] or 0) - l["breakeven_wr"]
        label = {"stable": "คงตัว", "era": "ของยุคสมัย",
                 "none": "ไม่มี edge"}[l["stability"]]
        lines.append(
            f"| {i} | {l['formula']} | {l['n']} | {l['breakeven_wr']*100:.1f}% | "
            f"{_fmt(l['wr_pes'], True)} | {_fmt(l['wr_opt'], True)} | "
            f"{d_wr*100:+.1f}pp | {_fmt(l['e_pes'])} | "
            f"{l['quarters_beating_be']}/4 | {label} |")
    lines += ["", f"*Stable formulas: {len(stable)}*",
              "",
              "หมายเหตุ: ป้าย 'คงตัว' ใช้เกณฑ์ WR>BE ตามคำสั่ง — แต่ E$/bet (pes) คือ",
              "กำไรจริงหัก spread แล้ว สูตรที่ 'มักจะ win' จริง = คงตัว **และ** E$/bet > 0:",
              ""]
    profitable = [l for l in stable if (l["e_pes"] or 0) > 0]
    for l in profitable:
        lines.append(f"- **{l['formula']}** — WR pes {_fmt(l['wr_pes'], True)} "
                     f"(BE {l['breakeven_wr']*100:.1f}%), "
                     f"E$/bet +{_fmt(l['e_pes'])}, "
                     f"ชนะ BE {l['quarters_beating_be']}/4 ไตรมาส")
    if not profitable:
        lines.append("- (ไม่มีสูตรที่คงตัวและ E>0 พร้อมกัน)")
    lines += ["", "*คงตัวแต่ E$/bet ≤ 0 (ชนะ BE แต่ไม่พอหัก spread):*"]
    for l in stable:
        if (l["e_pes"] or 0) <= 0:
            lines.append(f"- {l['formula']} — E$/bet {_fmt(l['e_pes'])}")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", default=str(BARS_CSV))
    ap.add_argument("--out-stem", default=None,
                    help="output stem (default ψ/outbox/bet_bracket_deep_<today>)")
    args = ap.parse_args()

    bars_csv = Path(args.bars)
    df = load_bars()
    logger.info("resolving grid …")
    records, pair_stats = run_grid(df)

    tick_days = sorted((_ROOT / "data" / "paper-feed" / "ticks").glob("ticks_*.parquet"))
    tick_note = (f"{len(tick_days)} day archive "
                 f"({tick_days[0].stem[6:]} → {tick_days[-1].stem[6:]}) downloading "
                 "in background; tick-resolved upgrade pending"
                 if tick_days else "tick archive not started")

    payload, md = build_report(records, pair_stats, df, bars_csv, tick_note)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    stem = Path(args.out_stem) if args.out_stem else \
        _ROOT / "ψ" / "outbox" / f"bet_bracket_deep_{today}"
    Path(str(stem) + ".md").write_text(md)
    Path(str(stem) + ".json").write_text(json.dumps(payload, indent=2))
    logger.info("wrote %s.md / .json", stem)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
