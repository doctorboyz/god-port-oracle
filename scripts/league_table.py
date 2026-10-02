#!/usr/bin/env python3
"""League table for the paper trade farm — read farm DBs, rank variants.

Writes ψ/outbox/league_table_<date>.md so Hermes (scope AI Investor) can
analyze results on its own schedule. Numbers come straight from the DB:

    trades      — closed trades (is_open=0 AND pnl IS NOT NULL)
    wins        — pnl > 0
    WR          — wins / trades
    Σpnl        — total closed pnl ($)
    PF          — Σ|wins| / Σ|losses| (∞ shown as — when no losses)
    MaxDD       — max peak-to-trough of the closed-trade equity curve,
                  as % of running peak equity (seed 100 = INITIAL_EQUITY)
    open        — currently open trades

Usage:
    python3 scripts/league_table.py                     # default: data/farm/*.db
    python3 scripts/league_table.py db1.db db2.db      # explicit DBs
"""

from __future__ import annotations

import glob
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTBOX = ROOT / "ψ" / "outbox"
SEED_EQUITY = 100.0  # matches INITIAL_EQUITY in farm_variants.yml common


def _db_paths(args: list[str]) -> list[Path]:
    if args:
        return [Path(a) for a in args]
    found = sorted(glob.glob(str(ROOT / "data" / "farm" / "*" / "*.db")))
    # Skip daily backups (data/farm/backup/*.db) — same schema as the live DB
    # (it's a copy), so _is_farm_db alone can't tell them apart; reading them
    # would double-count or resurrect stale variants in the table.
    return [Path(p) for p in found if Path(p).parent.name != "backup"]


def _max_dd(equity: list[float]) -> float:
    """Max peak-to-trough drawdown as fraction of the running peak."""
    peak = equity[0] if equity else 0.0
    max_dd = 0.0
    for v in equity:
        peak = max(peak, v)
        if peak > 0:
            max_dd = max(max_dd, (peak - v) / peak)
    return max_dd


def collect(db_path: Path) -> dict[str, dict]:
    """Per (account, strategy_id) stats from one farm DB."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        has_schema = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='live_trades'"
        ).fetchone()
        if not has_schema:
            return {}  # stray/pre-init file — not a farm DB
        rows = conn.execute(
            """
            SELECT a.name AS account, t.strategy_id, t.pnl, t.exit_time,
                   t.timestamp, t.is_open
            FROM live_trades t
            JOIN accounts a ON a.id = t.account_id
            WHERE t.is_open = 0 AND t.pnl IS NOT NULL
            ORDER BY t.exit_time
            """
        ).fetchall()
        open_rows = conn.execute(
            "SELECT a.name AS account, t.strategy_id, COUNT(*) AS n "
            "FROM live_trades t JOIN accounts a ON a.id = t.account_id "
            "WHERE t.is_open = 1 GROUP BY a.name, t.strategy_id"
        ).fetchall()
        open_counts = {r["account"]: r["n"] for r in open_rows}
    finally:
        conn.close()

    stats: dict[str, dict] = {}

    # Variants holding only open trades must stay visible (they exist, they
    # just haven't closed anything yet).
    for r in open_rows:
        stats.setdefault(r["account"], {
            "strategy_id": r["strategy_id"] or "",
            "trades": 0, "wins": 0, "pnl_sum": 0.0,
            "gross_win": 0.0, "gross_loss": 0.0,
            "equity": [SEED_EQUITY],
            "first": None, "last": None,
        })

    for r in rows:
        key = r["account"]
        s = stats.setdefault(key, {
            "strategy_id": r["strategy_id"] or "",
            "trades": 0, "wins": 0, "pnl_sum": 0.0,
            "gross_win": 0.0, "gross_loss": 0.0,
            "equity": [SEED_EQUITY],
            "first": r["timestamp"], "last": r["exit_time"],
        })
        pnl = float(r["pnl"])
        s["trades"] += 1
        s["pnl_sum"] += pnl
        if pnl > 0:
            s["wins"] += 1
            s["gross_win"] += pnl
        else:
            s["gross_loss"] += -pnl
        s["equity"].append(s["equity"][-1] + pnl)
        s["last"] = r["exit_time"] or s["last"]

    for key, s in stats.items():
        s["open"] = open_counts.get(key, 0)
        s["wr"] = s["wins"] / s["trades"] if s["trades"] else 0.0
        s["pf"] = (s["gross_win"] / s["gross_loss"]) if s["gross_loss"] > 0 else None
        s["max_dd"] = _max_dd(s["equity"])
        s["equity_now"] = s["equity"][-1]
    return stats


def render(stats: dict[str, dict], sources: list[Path]) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Paper Farm League Table",
        f"",
        f"อัปเดต: {now} · แหล่ง: {', '.join(p.name for p in sources)} · seed equity {SEED_EQUITY:.0f}$",
        "",
        "เรียงตาม Σpnl มาก → น้อย (จำนวน trade น้อย = ข้อมูลยังไม่พอตัดสิน — ดูคอลัมน์ trades)",
        "",
        "| Variant | Strategy | Trades | Open | WR | Σpnl ($) | Equity ($) | PF | MaxDD | เก็บข้อมูลถึง |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    ranked = sorted(stats.items(), key=lambda kv: kv[1]["pnl_sum"], reverse=True)
    for name, s in ranked:
        pf = "—" if s["pf"] is None else f"{s['pf']:.2f}"
        lines.append(
            f"| {name} | {s['strategy_id']} | {s['trades']} | {s['open']} "
            f"| {s['wr']*100:.0f}% | {s['pnl_sum']:+.2f} | {s['equity_now']:.2f} "
            f"| {pf} | {s['max_dd']*100:.1f}% | {(s['last'] or '—')} |"
        )
    if not ranked:
        lines.append("| _ยังไม่มี trade ที่ปิดแล้ว_ | | | | | | | | | |")
    lines += [
        "",
        "หมายเหตุสำหรับ Hermes (ผู้วิเคราะห์):",
        "- trades < 20 → อย่าตัดสิน kill/promote จากตัวเลขชุดนี้ (noise ยังเยอะ)",
        "- กฎ out-of-sample: variant ที่ชนะ selection window ต้องผ่านข้อมูลช่วงใหม่ก่อนเลื่อนขั้น",
        "- เสนอ variant ใหม่/แก้ knob: แก้ farm_variants.yml แล้วรัน scripts/paper_farm_apply.py --up",
        "",
    ]
    return "\n".join(lines)


def _is_farm_db(path: Path) -> bool:
    """True when the file has the live_trades schema (stray connect-only
    artifacts like an import-time oracle.db placeholder are skipped)."""
    try:
        conn = sqlite3.connect(str(path))
        try:
            return bool(conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='live_trades'"
            ).fetchone())
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def main() -> None:
    paths = [p for p in _db_paths(sys.argv[1:]) if _is_farm_db(p)]
    if not paths:
        print("No farm DBs found — farm may not have run yet.")
        sys.exit(0)

    stats: dict[str, dict] = {}
    for p in paths:
        stats.update(collect(p))

    report = render(stats, paths)
    OUTBOX.mkdir(parents=True, exist_ok=True)
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = OUTBOX / f"league_table_{date}.md"
    out.write_text(report)
    print(report)
    print(f"\nWritten to {out}")


if __name__ == "__main__":
    main()