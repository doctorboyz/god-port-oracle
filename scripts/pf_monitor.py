#!/usr/bin/env python3
"""Persistent PF monitor for mr-bet B/C/D on oracle-engine-train (ISSUE-094).

Replaces the session-only Claude CronCreate job ("เฝ้า PF mr-bet รายวัน") that
died with every session. Runs INSIDE the container (direct sqlite access to
/app/data/oracle_train.db in the oracle-train-data named volume) — the VPS
host crontab orchestrates via docker cp + docker exec, the same pattern as
scripts/verify_deploy.py, so the cron always uses the latest committed script
without waiting for an image rebuild:

    17 10 * * * cd /opt/god-port-oracle && set -a && . ./.env && set +a && \
      docker cp scripts/pf_monitor.py oracle-engine-train:/tmp/pf_monitor.py && \
      docker exec -i -e TG_BOT_TOKEN -e TG_CHAT_ID -e REPORTING_ENABLED=1 \
      oracle-engine-train python3 /tmp/pf_monitor.py >> /var/log/pf-monitor.log 2>&1

Anomaly criteria (from the 2026-09-25 retrospective procedure):
    - PF < 0.5            (with the "<200 trades = ยังสรุปไม่ได้" caveat attached)
    - no new trade > 24h   (suppressed while the market is closed: Sat, Sun < 20:00 UTC)
    - balance drop > 5%    vs the previous run's snapshot (state file, JSON)

Thai report goes to stdout (cron log) ALWAYS; Telegram fires only on anomaly
(or --always). Gating follows the scripts/portfolio_manager.py pattern:
REPORTING_ENABLED=1 master switch, TG_BOT_TOKEN/TELEGRAM_BOT_TOKEN,
TG_CHAT_ID/TELEGRAM_CHAT_ID. Best-effort everywhere — a cron job must never
crash: the whole main() is wrapped, exit code is always 0.

Trades are filtered to timestamp >= --since (post RR-fix cutoff
2026-09-23T19:00) — mixing pre-fix trades fakes the PF.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_DB = "/app/data/oracle_train.db"
DEFAULT_ACCOUNTS = "B,C,D"
DEFAULT_SINCE = "2026-09-23T19:00"  # post RR-fix cutoff (learning 2026-09-24)
PF_ALERT_THRESHOLD = 0.5
NO_TRADE_HOURS_ALERT = 24.0
BALANCE_DROP_ALERT = 0.05  # >5% vs previous run's snapshot
MIN_TRADES_FOR_VERDICT = 200  # retro judgment gate: below this, no conclusions

logger = None


def _log_setup() -> None:
    global logger
    if logger is None:
        import logging
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        logger = logging.getLogger("pf_monitor")


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    # DB timestamps are stored naive — treat them as UTC so comparisons with
    # datetime.now(timezone.utc) don't raise (caught by the causal tests).
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def market_open(now_utc: datetime) -> bool:
    """XAUUSD roughly closed Sat all day and Sun before ~20:00 UTC."""
    if now_utc.weekday() == 5:
        return False  # Saturday
    if now_utc.weekday() == 6 and now_utc.hour < 20:
        return False  # Sunday before open
    return True


def collect_stats(db_path: str, accounts: list[str], since: str) -> dict[str, dict]:
    """Per-account closed-trade stats from live_trades, filtered to >= since."""
    stats: dict[str, dict] = {a: {
        "trades": 0, "wins": 0, "pnl_sum": 0.0,
        "gross_win": 0.0, "gross_loss": 0.0,
        "open": 0, "last_trade_ts": None,
    } for a in accounts}
    if not Path(db_path).exists():
        return {}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        has_schema = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='live_trades'"
        ).fetchone()
        if not has_schema:
            return {}
        rows = conn.execute(
            """
            SELECT a.name AS account, t.pnl, t.timestamp, t.exit_time, t.is_open
            FROM live_trades t
            JOIN accounts a ON a.id = t.account_id
            WHERE t.timestamp >= ?
            ORDER BY t.timestamp
            """,
            (since,),
        ).fetchall()
    finally:
        conn.close()

    for r in rows:
        account = r["account"]
        if account not in stats:
            continue
        s = stats[account]
        ts = _parse_ts(r["exit_time"] or r["timestamp"])
        if ts is not None and (s["last_trade_ts"] is None or ts > s["last_trade_ts"]):
            s["last_trade_ts"] = ts
        if r["is_open"]:
            s["open"] += 1
            continue
        if r["pnl"] is None:
            continue
        pnl = float(r["pnl"])
        s["trades"] += 1
        s["pnl_sum"] += pnl
        if pnl > 0:
            s["wins"] += 1
            s["gross_win"] += pnl
        else:
            s["gross_loss"] += -pnl

    for s in stats.values():
        s["wr"] = s["wins"] / s["trades"] if s["trades"] else 0.0
        s["pf"] = (s["gross_win"] / s["gross_loss"]) if s["gross_loss"] > 0 else None
    return stats


def fetch_balances(accounts: list[str]) -> dict[str, float | None]:
    """Live balance per account via the MT5 bridge (RPyC). Best-effort."""
    balances: dict[str, float | None] = {}
    for name in accounts:
        try:
            from metty.core.account_registry import get_bridge_config
            from metty.bridge.client import MT5Bridge
            bridge = MT5Bridge(get_bridge_config(name))
            info = bridge.fetch_account_info_sync()
            if info is None:
                balances[name] = None
                continue
            if isinstance(info, dict):
                balances[name] = info.get("balance")
            else:
                balances[name] = getattr(info, "balance", None)
        except Exception as e:
            if logger:
                logger.warning(f"[{name}] balance fetch failed: {e}")
            balances[name] = None
    return balances


def _load_state(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(path: str, state: dict) -> None:
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as e:
        if logger:
            logger.warning(f"state file write failed: {e}")


def evaluate_anomalies(
    stats: dict[str, dict],
    balances: dict[str, float | None],
    prev_state: dict,
    now_utc: datetime,
) -> list[str]:
    """Return Thai alert lines per tripped criterion. Empty = all quiet."""
    alerts: list[str] = []
    is_open_market = market_open(now_utc)
    for name, s in sorted(stats.items()):
        lines: list[str] = []
        if s["pf"] is not None and s["pf"] < PF_ALERT_THRESHOLD:
            lines.append(
                f"PF={s['pf']:.2f} < {PF_ALERT_THRESHOLD} "
                f"({s['trades']} ไม้ — ต่ำกว่า {MIN_TRADES_FOR_VERDICT} ยังสรุปอะไรไม่ได้)"
            )
        if is_open_market and s["last_trade_ts"] is not None:
            hours = (now_utc - s["last_trade_ts"]).total_seconds() / 3600.0
            if hours > NO_TRADE_HOURS_ALERT:
                lines.append(f"ไม่มีไม้ใหม่ {hours:.1f} ชม. (> {NO_TRADE_HOURS_ALERT:.0f})")
        if is_open_market and s["last_trade_ts"] is None and s["open"] == 0 and s["trades"] == 0:
            # Zero trades at all since cutoff — only worth a note while the
            # market is open; on weekends this is normal.
            lines.append(f"ยังไม่มี trade เลยตั้งแต่ cutoff")
        bal = balances.get(name)
        prev_bal = (prev_state.get("accounts") or {}).get(name, {}).get("balance")
        if bal is not None and prev_bal is not None and prev_bal > 0:
            drop = (prev_bal - bal) / prev_bal
            if drop > BALANCE_DROP_ALERT:
                lines.append(
                    f"balance ลด {drop*100:.1f}% จากรอบก่อน "
                    f"({prev_bal:.2f} → {bal:.2f} > {BALANCE_DROP_ALERT*100:.0f}%)"
                )
        if lines:
            alerts.append(f"⚠️ [{name}] " + " · ".join(lines))
    return alerts


def render_report(
    stats: dict[str, dict],
    balances: dict[str, float | None],
    now_utc: datetime,
    alerts: list[str],
    db_path: str,
) -> str:
    now = now_utc.strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# mr-bet PF Monitor — {now}",
        f"แหล่ง: {db_path}",
        "",
        "| Account | Trades | Open | WR | PF | Σpnl ($) | ไม้ล่าสุด | Balance ($)|",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name, s in sorted(stats.items()):
        pf = "—" if s["pf"] is None else f"{s['pf']:.2f}"
        last = s["last_trade_ts"].strftime("%m-%d %H:%M") if s["last_trade_ts"] else "—"
        hours = ""
        if s["last_trade_ts"]:
            h = (now_utc - s["last_trade_ts"]).total_seconds() / 3600.0
            hours = f" ({h:.1f} ชม.)"
        bal = balances.get(name)
        bal_txt = "—" if bal is None else f"{bal:.2f}"
        lines.append(
            f"| {name} | {s['trades']} | {s['open']} | {s['wr']*100:.0f}% "
            f"| {pf} | {s['pnl_sum']:+.2f} | {last}{hours} | {bal_txt} |"
        )
    lines.append("")
    lines.append(
        f"หมายเหตุ: ต่ำกว่า {MIN_TRADES_FOR_VERDICT} ไม้ ยังสรุปอะไรไม่ได้ · "
        "no-trade alert ปิดช่วงตลาดปิด (ส. ทั้งวัน, อา. ก่อน 20:00 UTC)"
    )
    if alerts:
        lines.append("")
        lines.append("## 🔴 Anomaly")
        lines.extend(f"- {a}" for a in alerts)
    else:
        lines.append("")
        lines.append("✅ ไม่มี anomaly — ทุกเกณฑ์ปกติ")
    return "\n".join(lines)


def _reporting_enabled() -> bool:
    return os.environ.get("REPORTING_ENABLED", "0") == "1"


def _telegram_credentials() -> tuple[str, str]:
    token = os.environ.get("TG_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TG_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID", "")
    return token, chat


def send_telegram(text: str) -> bool:
    """Telegram send, portfolio_manager gating pattern. Best-effort."""
    if not _reporting_enabled():
        if logger:
            logger.info("reporting disabled — telegram skipped")
        return False
    token, chat = _telegram_credentials()
    if not token or not chat:
        if logger:
            logger.info("telegram not configured — send skipped")
        return False
    try:
        from metty.notify.telegram_bot import TelegramNotifier
        return TelegramNotifier(token, chat).send(text)
    except Exception as e:
        if logger:
            logger.warning(f"telegram send failed: {e}")
        return False


def main(argv: list[str] | None = None) -> int:
    _log_setup()
    try:
        parser = argparse.ArgumentParser(description="mr-bet persistent PF monitor")
        parser.add_argument("--db-path", default=DEFAULT_DB)
        parser.add_argument("--accounts", default=DEFAULT_ACCOUNTS,
                            help="comma-separated account names")
        parser.add_argument("--since", default=DEFAULT_SINCE,
                            help="ISO cutoff — trades before this are excluded (post RR-fix)")
        parser.add_argument("--state-file", default=None,
                            help="JSON state path (default: <db dir>/pf_monitor_state.json)")
        parser.add_argument("--with-balance", action="store_true",
                            help="fetch live balance/equity via the MT5 bridge")
        parser.add_argument("--always", action="store_true",
                            help="telegram the report even with no anomaly")
        parser.add_argument("--no-telegram", action="store_true")
        parser.add_argument("--now", default=None,
                            help="override 'now' as ISO datetime (UTC) — for tests/replay")
        args = parser.parse_args(argv)

        accounts = [a.strip() for a in args.accounts.split(",") if a.strip()]
        state_path = args.state_file or str(Path(args.db_path).parent / "pf_monitor_state.json")

        if not Path(args.db_path).exists():
            print(f"DB not found: {args.db_path} — nothing to monitor, exiting quietly")
            return 0

        if args.now:
            now_utc = _parse_ts(args.now)
            assert now_utc is not None, f"bad --now: {args.now}"
        else:
            now_utc = datetime.now(timezone.utc)
        stats = collect_stats(args.db_path, accounts, args.since)
        balances = fetch_balances(accounts) if args.with_balance else {a: None for a in accounts}
        prev_state = _load_state(state_path)
        alerts = evaluate_anomalies(stats, balances, prev_state, now_utc)

        # Persist this run's balances for the next run's drop comparison.
        _save_state(state_path, {
            "updated_at": now_utc.isoformat(),
            "accounts": {a: {"balance": balances.get(a)} for a in accounts},
        })

        report = render_report(stats, balances, now_utc, alerts, args.db_path)
        print(report)

        if args.no_telegram:
            return 0
        if alerts:
            body = "mr-bet PF Monitor 🔴\n\n" + "\n".join(alerts)
            if send_telegram(body):
                print("telegram: anomaly alert sent")
        elif args.always:
            if send_telegram("mr-bet PF Monitor ✅ ปกติ\n\n" + report):
                print("telegram: heartbeat sent")
        return 0
    except Exception as e:
        # A cron job must never crash the run — log and exit 0.
        print(f"pf_monitor error (best-effort exit): {e}")
        return 0


if __name__ == "__main__":
    sys.exit(main())