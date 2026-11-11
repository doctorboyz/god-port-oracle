#!/usr/bin/env python3
"""Backtest the 9 true-bet cells over the full stored M5 history.

Runs the SAME engine primitives as the live container (entries +
contract + engine.run_backtest_cell) over every closed M5 bar in the
paper feed store — Exness XAUUSD, ~74 days (2026-08-16 →). No lookahead:
signals come from closed bars only, fills are the NEXT bar's open, one
bet per cell at a time (1 ไม้/cell เหมือน live).

Ambiguous bars (one bar touching both contract levels) are reported as
TWO edges, per the approved design:
  - pessimistic (sl_first): the loss is booked
  - optimistic (tp_first): the win is booked

Per cell: n bets · WR (both edges) · expectancy $/bet vs breakeven WR ·
avg bars to finish · longest losing streak · MFE/MAE · open-at-end.

Output: markdown report (default ψ/outbox/bet_backtest_2026-11-07.md)
for Hermes to present to คุณหมอ BEFORE the live farm starts collecting.

Run:
    python3 scripts/bet_backtest.py                       # real feed store
    python3 scripts/bet_backtest.py --feed path/to.csv   # explicit CSV
    python3 scripts/bet_backtest.py --json out.json      # machine copy
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from broky.bet import cells, engine  # noqa: E402
from broky.bet.contract import POLICY_SL_FIRST, POLICY_TP_FIRST  # noqa: E402

DEFAULT_FEED = _ROOT / "data" / "paper-feed" / "XAUUSD_M5.csv"
DEFAULT_REPORT = _ROOT / "ψ" / "outbox" / "bet_backtest_2026-11-07.md"


def load_feed(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise SystemExit(f"feed not found: {path}")
    df = pd.read_csv(
        path, header=None,
        names=["date", "open", "high", "low", "close", "volume"],
        parse_dates=["date"],
    ).set_index("date")
    df = df.dropna().sort_index()
    if df.index.has_duplicates:
        df = df[~df.index.duplicated(keep="last")]
    return df


def run_all(df: pd.DataFrame, specs: list[engine.CellSpec]) -> dict:
    """Backtest every cell under both ambiguity edges."""
    results: dict = {}
    for spec in specs:
        per_policy = {}
        for policy in (POLICY_SL_FIRST, POLICY_TP_FIRST):
            records = engine.run_backtest_cell(df, spec, policy)
            balance = spec.seed_balance + sum(
                r.pnl for r in records if r.status == engine.STATUS_RESOLVED
            )
            stats = engine.cell_stats(
                records, spec.geometry,
                spread_cost_per_oz=spec.spread_cost_per_oz,
                final_balance=balance,
            )
            per_policy[policy] = {
                "stats": stats,
                "bets": [record_to_dict(r) for r in records],
            }
        results[spec.cell_id] = {
            "family": spec.family,
            "geometry": {
                "tp_dist": spec.geometry.tp_dist,
                "sl_dist": spec.geometry.sl_dist,
            },
            "risk_fraction": spec.risk_fraction,
            "seed_balance": spec.seed_balance,
            "edges": per_policy,
        }
    return results


def record_to_dict(r: engine.BetRecord) -> dict:
    return {
        "direction": r.direction,
        "signal_ts": r.signal_ts.isoformat(),
        "entry_ts": r.entry_ts.isoformat(),
        "entry_price": r.entry_price,
        "qty_oz": round(r.qty_oz, 4),
        "status": r.status,
        "outcome": r.outcome,
        "exit_ts": r.exit_ts.isoformat() if r.exit_ts else None,
        "pnl": round(r.pnl, 4) if r.pnl is not None else None,
        "bars_to_finish": r.bars_to_finish,
        "mfe_price": round(r.mfe_price, 2),
        "mae_price": round(r.mae_price, 2),
        "ambiguous": r.ambiguous,
    }


def fmt_pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def build_report(results: dict, df: pd.DataFrame, feed_path: Path) -> str:
    first, last = df.index[0], df.index[-1]
    calendar_days = (last - first).total_seconds() / 86400
    # XAUUSD trades ~23h/day → 276 M5 bars per trading day
    trading_days = len(df) / 276.0
    lines = [
        "# True-Bet Farm — Backtest ประวัติ M5 ทั้งหมดใน feed store",
        "",
        f"*สร้าง: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · "
        f"engine เดียวกับ live (broky/bet) · จุดเข้าจากแท่งปิดเท่านั้น · "
        f"เข้าที่ open แท่งถัดไป · 1 ไม้/cell เหมือน live*",
        "",
        "## ข้อมูล",
        "",
        f"- Feed: `{feed_path}` (era Exness XAUUSD realtime — สีเดียวกับ demo B/C/D และฟาร์มกระดาษ)",
        f"- ช่วง: **{first} → {last}** — {len(df):,} แท่ง M5 ≈ **{trading_days:.0f} วันเทรด** "
        f"({calendar_days:.0f} วันปฏิทิน) — คือประวัติทั้งหมดที่มีใน feed store "
        f"(โจทย์เรียก ~74 วันตาม seed 15k แท่งยุคแรก ตอนรันจริง store มี 17.3k แท่ง)",
        "- Seed **$1,000/cell** · bet = 1.5% ของแบล็อต ($15 ที่ seed — compounding ตามแบล็อตจริง)",
        "- ค่าใช้จ่าย: spread $0.20/oz ต่อ round trip หักจาก pnl ทุกไม้ (farm parity) — "
        "TP/SL ตัดสินจากระดับ geometry เป๊ะ ไม่บิดเบี้ยวด้วย spread",
        "- Ambiguous bar (แท่งเดียวทะลุทั้ง TP+SL): รายงาน 2 ขอบ — **pes** = SL ก่อน, "
        "**opt** = TP ก่อน",
        "",
        "## ตารางหลัก — ต่อ cell",
        "",
        "| Cell | Family | Contract | n bets | open | WR (pes) | WR (opt) | "
        "BE WR | E$/bet (pes) | E$/bet (opt) | แถวเฉลี่ยถึงจบ | Loss streak (pes) | amb |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]

    family_names = {
        "A": "MR-Boll extreme",
        "B": "Breakout 20",
        "C": "Momentum-pullback",
    }
    for cell_id, res in results.items():
        pes = res["edges"][POLICY_SL_FIRST]["stats"]
        opt = res["edges"][POLICY_TP_FIRST]["stats"]
        g = res["geometry"]
        contract = f"TP{g['tp_dist']:.0f}:SL{g['sl_dist']:.0f}"
        lines.append(
            f"| {cell_id} | {family_names.get(res['family'], res['family'])} "
            f"| {contract} | {pes['n_bets']} | {pes['n_open']} "
            f"| {fmt_pct(pes['wr'])} | {fmt_pct(opt['wr'])} "
            f"| {fmt_pct(pes['breakeven_wr'])} "
            f"| {pes['expectancy']:+.2f} | {opt['expectancy']:+.2f} "
            f"| {pes['avg_bars_to_finish']:.1f} | {pes['longest_losing_streak']} "
            f"| {pes['n_ambiguous']} |"
        )

    lines += [
        "",
        "## รายละเอียดต่อ cell (ขอบ pessimistic — ตัวเลขที่ควรใช้ตัดสินใจ)",
        "",
    ]
    for cell_id, res in results.items():
        pes = res["edges"][POLICY_SL_FIRST]["stats"]
        opt = res["edges"][POLICY_TP_FIRST]["stats"]
        g = res["geometry"]
        lines += [
            f"### {cell_id} — TP{g['tp_dist']:.0f}:SL{g['sl_dist']:.0f} "
            f"(family {res['family']})",
            "",
            f"- ไม้ทั้งหมด **{pes['n_bets']}** (ยังเปิดค้างท้ายข้อมูล {pes['n_open']} — "
            f"ไม่นับใน WR)",
            f"- WR: **{fmt_pct(pes['wr'])}** pes / {fmt_pct(opt['wr'])} opt · "
            f"breakeven **{fmt_pct(pes['breakeven_wr'])}** "
            f"(บวก spread: {fmt_pct(pes['breakeven_wr_with_spread'])})",
            f"- Expectancy: **{pes['expectancy']:+.2f} $/ไม้** pes / "
            f"{opt['expectancy']:+.2f} opt · รวม {pes['total_pnl']:+.2f}$ pes",
            f"- แถวเฉลี่ยถึงจบ: {pes['avg_bars_to_finish']:.1f} แท่ง M5 "
            f"(≈ {pes['avg_bars_to_finish'] * 5:.0f} นาที)",
            f"- Losing streak ยาวสุด (pes): **{pes['longest_losing_streak']}** ไม้ติด",
            f"- MFE เฉลี่ย {pes['avg_mfe']:.2f}$ · MAE เฉลี่ย {pes['avg_mae']:.2f}$ "
            f"(ระยะราคาจากราคาเข้า)",
            f"- Ambiguous bar: {pes['n_ambiguous']} ไม้ "
            f"({fmt_pct(pes['n_ambiguous'] / pes['n_resolved']) if pes['n_resolved'] else 'n/a'})",
            f"- แบล็อตจบ: {pes['final_balance']:.2f}$ pes "
            f"(จาก seed {res['seed_balance']:.0f}$)",
            "",
        ]

    lines += [
        "## อ่านผลอย่างไร",
        "",
        "- **WR ต้องเทียบกับ BE WR ของ geometry เสมอ** — TP6:SL12 และ TP10:SL20 "
        "ต้องชนะ ~66.7% จึงคุ้ม, TP8:SL8 ต้องชนะ 50% (บวก spread อีกนิด)",
        "- ขอบ pes/opt ห่างกันเท่าไร = ความไม่แน่นอนจาก ambiguous bar เท่านั้น — "
        "ความจริงอยู่ระหว่างสองขอบนี้ (สดจะตัดสินด้วย tick จึงไม่มี ambiguous)",
        "- จำนวนไม้ (n) สำคัญไม่แพ้ WR — n น้อย = ข้อมูลยังไม่พอสรุปเรื่อง edge",
        "",
        "---",
        "",
        "*รายงานนี้สร้างโดย `scripts/bet_backtest.py` — Hermes นำเสนอคุณหมอก่อนเปิดเก็บสด "
        "(โจทย์ ada-task-20261107-true-bet-farm.md T4)*",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="True-bet farm 74-day backtest")
    parser.add_argument("--feed", type=Path, default=DEFAULT_FEED,
                        help="M5 CSV (default: the live paper-feed store)")
    parser.add_argument("--out", type=Path, default=DEFAULT_REPORT,
                        help="markdown report path")
    parser.add_argument("--json", type=Path, default=None,
                        help="optional machine-readable copy")
    args = parser.parse_args()

    specs = cells.load_bet_cells()
    if not specs:
        raise SystemExit("no bet cells found in farm_variants.yml (engine: bet group)")
    df = load_feed(args.feed)
    print(f"feed: {args.feed} — {len(df):,} bars "
          f"({df.index[0]} → {df.index[-1]})")
    print(f"cells: {len(specs)} — {', '.join(s.cell_id for s in specs)}")

    results = run_all(df, specs)

    report = build_report(results, df, args.feed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report, encoding="utf-8")
    print(f"report → {args.out}")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8",
        )
        print(f"json → {args.json}")

    # console summary (same numbers as the report)
    for cell_id, res in results.items():
        pes = res["edges"][POLICY_SL_FIRST]["stats"]
        opt = res["edges"][POLICY_TP_FIRST]["stats"]
        g = res["geometry"]
        print(
            f"{cell_id} TP{g['tp_dist']:.0f}:SL{g['sl_dist']:.0f} "
            f"n={pes['n_bets']}(open {pes['n_open']}) "
            f"WR {fmt_pct(pes['wr'])}/{fmt_pct(opt['wr'])} "
            f"(BE {fmt_pct(pes['breakeven_wr'])}) "
            f"E {pes['expectancy']:+.2f}/{opt['expectancy']:+.2f} "
            f"bars~{pes['avg_bars_to_finish']:.0f} "
            f"streak {pes['longest_losing_streak']} amb {pes['n_ambiguous']}"
        )


if __name__ == "__main__":
    main()
