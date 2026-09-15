"""Portfolio manager — the kill/freeze/compound brain for P-accounts (item C).

Runs on the VPS via crontab (*/30 min). For each P-account:

  1. Fetch live equity from the account's MT5 bridge
  2. Update the high-water mark (peak_equity) — compounding is automatic
     because position sizing uses current equity
  3. Freeze when drawdown from peak >= 20% (kill switch, DB-backed)
  4. Start a 24h cooldown after 3 consecutive losses (circuit-breaker pause)
  5. Audit every decision to portfolio_events (append-only, Kappa #1)

Status transitions:
  - running → frozen : automatic (this manager)
  - frozen → running  : MANUAL ONLY (คุณหมอ/Hermes decision, never auto)
  - → closed          : MANUAL ONLY — this manager never closes an account

Weekly summary (Sunday 20:00 BKK = 13:00 UTC) → ψ/inbox + Telegram.

Usage (VPS crontab, every 30 min):
    python3 scripts/portfolio_manager.py
    python3 scripts/portfolio_manager.py --weekly   # force summary
    python3 scripts/portfolio_manager.py --dry-run  # report, write nothing
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from metty.core.db import (
    get_account_portfolio_state,
    get_consecutive_losses,
    log_portfolio_event,
    set_cooldown,
    set_portfolio_status,
    update_peak_equity,
)

logger = logging.getLogger("portfolio_manager")

# Portfolio rules (from Hermes' analysis — sweet-spot requirements)
FREEZE_DD_PCT = 20.0        # DD from peak_equity >= 20% → freeze
CB_CONSECUTIVE_LOSSES = 3   # 3 losses in a row → pause
CB_PAUSE_HOURS = 24         # pause duration
WEEKLY_UTC_HOUR = 13        # Sunday 20:00 BKK = 13:00 UTC

BKK_UTC_OFFSET = 7


def check_account(
    account_name: str,
    db_path,
    equity: Optional[float],
    now_utc: Optional[datetime] = None,
    dry_run: bool = False,
    freeze_dd_pct: float = FREEZE_DD_PCT,
    cb_losses: int = CB_CONSECUTIVE_LOSSES,
    cb_pause_hours: int = CB_PAUSE_HOURS,
) -> list[dict]:
    """Evaluate one P-account: peak update, freeze check, CB cooldown.

    Returns a list of action dicts:
      {"type": "peak_updated", "equity": ...}
      {"type": "frozen", "reason": ..., "dd_pct": ...}
      {"type": "cooldown_set", "until": ..., "losses": ...}

    dry_run=True computes decisions but writes nothing (for --dry-run report).
    """
    now = now_utc or datetime.now(timezone.utc)
    state = get_account_portfolio_state(account_name, db_path)
    if state is None:
        logger.warning("[PM] %s not enrolled in accounts table — skipping", account_name)
        return []

    status = state.get("portfolio_status") or "running"
    actions: list[dict] = []

    # Manual-only statuses are untouchable by automation
    if status == "closed":
        logger.info("[PM] %s is closed (manual status) — no automatic action", account_name)
        return actions

    # 1. High-water mark (compounding base). Equity unavailable → skip sizing
    #    checks but still run the loss-streak check (DB-only, no bridge needed).
    peak = state.get("peak_equity") or 0.0
    if equity is not None and equity > 0:
        if not dry_run:
            update_peak_equity(account_name, equity, db_path)
        peak = max(peak, equity)
        if equity > (state.get("peak_equity") or 0.0):
            actions.append({"type": "peak_updated", "equity": equity})

        # 2. Freeze check: DD from high-water peak
        if status == "running" and peak > 0:
            dd_pct = (peak - equity) / peak * 100.0
            if dd_pct >= freeze_dd_pct:
                reason = (
                    f"drawdown {dd_pct:.1f}% from peak {peak:.2f} "
                    f"(equity {equity:.2f}) >= {freeze_dd_pct:.0f}% kill line"
                )
                if not dry_run:
                    set_portfolio_status(account_name, "frozen", reason, db_path)
                    log_portfolio_event(
                        account_name, "frozen", reason,
                        metrics={"equity": equity, "peak": peak, "dd_pct": round(dd_pct, 2)},
                        db_path=db_path,
                    )
                actions.append({"type": "frozen", "reason": reason, "dd_pct": dd_pct})
                status = "frozen"  # skip cooldown write below on the same cycle

    # 3. Circuit breaker: trailing loss streak → cooldown (only when running)
    if status == "running":
        losses = get_consecutive_losses(state["account_id"], db_path)
        if losses >= cb_losses:
            until = now + timedelta(hours=cb_pause_hours)
            until_iso = until.isoformat()
            reason = f"{losses} consecutive losses → pause {cb_pause_hours}h"
            if not dry_run:
                set_cooldown(account_name, until_iso, reason, db_path)
                log_portfolio_event(
                    account_name, "cooldown_set", reason,
                    metrics={"losses": losses, "until": until_iso},
                    db_path=db_path,
                )
            actions.append({"type": "cooldown_set", "until": until_iso, "losses": losses})

    return actions


def fetch_account_equity(account_name: str) -> Optional[float]:
    """Live equity from the account's MT5 bridge (None if unreachable)."""
    try:
        from metty.bridge.client import MT5Bridge
        from metty.core.account_registry import get_account_config
        from metty.core.models import AccountConfig

        cfg = get_account_config(account_name)
        config = AccountConfig(
            name=cfg.name,
            broker_login=cfg.broker_login,
            broker_server=cfg.broker_server,
            balance=cfg.initial_balance,
            leverage=cfg.leverage,
            bridge_host=cfg.bridge_host,
            bridge_port=cfg.bridge_internal_port,
            signal_group=cfg.signal_group,
        )
        info = MT5Bridge(config).fetch_account_info_sync()
        return info.equity if info else None
    except Exception as e:
        logger.warning("[PM] %s equity fetch failed: %s", account_name, e)
        return None


def account_db_path(account_name: str, db_dir) -> Path:
    """Per-account DB path: {db_dir}/{name.lower()}/oracle_{name.lower()}.db

    Matches the VPS bind-mount layout: engine container writes
    /app/data/oracle_p1.db inside ${PORTFOLIO_DATA_DIR}/p1, so the host-side
    manager (crontab) reads ${PORTFOLIO_DATA_DIR}/p1/oracle_p1.db.
    """
    low = account_name.lower()
    return Path(db_dir) / low / f"oracle_{low}.db"


def build_weekly_summary(
    accounts: list[str],
    db_dir,
    db_paths: Optional[dict] = None,
) -> str:
    """Human-readable weekly portfolio summary (Sunday 20:00 BKK)."""
    import sqlite3

    if db_paths is None:
        db_paths = {name: account_db_path(name, db_dir) for name in accounts}

    lines = ["📊 Portfolio weekly summary — " + datetime.now(timezone.utc).strftime("%Y-%m-%d")]
    for name in accounts:
        db_path = db_paths[name]
        try:
            state = get_account_portfolio_state(name, db_path)
        except sqlite3.OperationalError:
            state = None  # DB file/table not created yet — engine never ran
        if state is None:
            lines.append(f"• {name}: not enrolled")
            continue
        status = state.get("portfolio_status") or "running"
        peak = state.get("peak_equity")
        baseline = state.get("baseline_balance")
        # last-7d closed trades
        wins = losses = 0
        pnl_sum = 0.0
        try:
            conn = sqlite3.connect(db_path)
            try:
                rows = conn.execute(
                    "SELECT pnl FROM live_trades WHERE account_id = ? AND is_open = 0 "
                    "AND pnl IS NOT NULL AND exit_time >= datetime('now', '-7 days')",
                    (state["account_id"],),
                ).fetchall()
            finally:
                conn.close()
            for (pnl,) in rows:
                pnl_sum += pnl
                if pnl < 0:
                    losses += 1
                else:
                    wins += 1
        except sqlite3.OperationalError:
            pass  # fresh account, no trades table rows yet
        lines.append(
            f"• {name} [{status}] 7d: {wins}W/{losses}L pnl {pnl_sum:+.2f} "
            f"peak {peak if peak is not None else '—'} baseline {baseline if baseline is not None else '—'}"
        )
    return "\n".join(lines)


def is_weekly_window(now_utc: Optional[datetime] = None) -> bool:
    """True during Sunday 13:00 UTC hour (20:00 BKK) — cron may land anywhere
    inside the hour, so the whole hour counts."""
    now = now_utc or datetime.now(timezone.utc)
    return now.weekday() == 6 and now.hour == WEEKLY_UTC_HOUR


def _send_alerts(text: str) -> None:
    """Telegram alert, best-effort (no token → disabled, no crash)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        logger.info("[PM] Telegram not configured — alert skipped:\n%s", text)
        return
    try:
        from metty.notify.telegram_bot import TelegramNotifier
        TelegramNotifier(token, chat_id).send(text)
    except Exception as e:
        logger.warning("[PM] Telegram send failed: %s", e)


def _write_inbox_summary(text: str, psi_dir: str = "ψ") -> Path:
    """Write the weekly summary to ψ/inbox (PM/human reads it on wake)."""
    inbox = Path(psi_dir) / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = inbox / f"portfolio_weekly_summary_{stamp}.md"
    path.write_text(text, encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Portfolio manager for P-accounts")
    parser.add_argument("--accounts", default=os.environ.get("PORTFOLIO_ACCOUNTS", "P1,P2,P3"))
    parser.add_argument("--db-dir", default=os.environ.get("PORTFOLIO_DB_DIR", "data"))
    parser.add_argument("--psi-dir", default="ψ")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--weekly", action="store_true", help="force weekly summary")
    parser.add_argument("--no-equity", action="store_true",
                        help="skip bridge fetch (loss-streak check only)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    accounts = [a.strip().upper() for a in args.accounts.split(",") if a.strip()]
    db_paths = {name: account_db_path(name, args.db_dir) for name in accounts}

    alert_lines: list[str] = []
    for name in accounts:
        equity = None if args.no_equity else fetch_account_equity(name)
        if not args.no_equity and equity is None:
            # fall back to last known balance so sizing checks still run
            state = get_account_portfolio_state(name, db_paths[name])
            equity = (state or {}).get("baseline_balance")
            if equity is not None:
                logger.warning("[PM] %s bridge unreachable — using baseline %.2f", name, equity)
        actions = check_account(name, db_paths[name], equity, dry_run=args.dry_run)
        for act in actions:
            if act["type"] == "frozen":
                alert_lines.append(f"🧊 {name} FROZEN: {act['reason']}")
            elif act["type"] == "cooldown_set":
                alert_lines.append(f"⏸ {name} cooldown until {act['until']} ({act['losses']} losses)")

    if alert_lines:
        _send_alerts("PORTFOLIO ALERT\n" + "\n".join(alert_lines))

    if args.weekly or is_weekly_window():
        summary = build_weekly_summary(accounts, args.db_dir, db_paths)
        path = _write_inbox_summary(summary, args.psi_dir)
        _send_alerts(summary)
        logger.info("[PM] weekly summary → %s", path)

    logger.info("[PM] done: %s accounts, %d actions%s",
                len(accounts), len(alert_lines), " (dry-run)" if args.dry_run else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())