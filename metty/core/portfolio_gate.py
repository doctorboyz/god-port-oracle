"""Portfolio gates for P-accounts: entry-hour window + freeze/cooldown.

Two gates guard every new entry on portfolio (P) accounts:

1. Entry-hour window (ENTRY_HOURS_<NAME> / BLOCKED_HOURS_<NAME>, UTC hours).
   Sweet-spot analysis (oracle_vps.db, 2,154 trades) says golden hours are
   01-03, 07-08, 13, 15 BKK and 10-12, 16, 21 BKK are negative-EV. Outside
   the window → HOLD. Legacy accounts (A-D) set no env var → gate is a
   no-op, existing behaviour unchanged.

2. Portfolio freeze/cooldown (accounts.portfolio_status / cooldown_until).
   The portfolio manager writes these; the engine reads them every cycle.
   Frozen/cooled accounts keep monitoring existing positions (SL/TP still
   work) but never open new ones. State lives in the DB, so the gate
   survives container restarts — unlike DrawdownProtector (in-memory).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


def parse_hour_list(raw: Optional[str]) -> set[int]:
    """Parse '18,19,20,6,8' into {18,19,20,6,8}. Empty/None → empty set."""
    if not raw:
        return set()
    hours = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        hour = int(part)
        if not 0 <= hour <= 23:
            raise ValueError(f"invalid hour {hour!r} in hour list {raw!r}")
        hours.add(hour)
    return hours


def entry_hour_gate(
    now_utc: datetime,
    account: str,
    environ: Optional[dict] = None,
) -> tuple[bool, str]:
    """Check entry-hour window for an account. Returns (allowed, reason).

    Rules (evaluated in order):
    - No env config at all → allowed (legacy accounts unaffected)
    - BLOCKED_HOURS_<NAME> set and current UTC hour in it → blocked
    - ENTRY_HOURS_<NAME> set and current UTC hour NOT in it → blocked
    - Both unset → allowed
    """
    env = environ if environ is not None else os.environ
    blocked = parse_hour_list(env.get(f"BLOCKED_HOURS_{account}", ""))
    allowed_hours = parse_hour_list(env.get(f"ENTRY_HOURS_{account}", ""))

    if not blocked and not allowed_hours:
        return True, ""  # no window configured — legacy behaviour

    hour = now_utc.hour
    if hour in blocked:
        return False, f"blocked hour {hour:02d}:00 UTC (negative-EV window)"
    if allowed_hours and hour not in allowed_hours:
        return False, (
            f"outside entry window: hour {hour:02d}:00 UTC not in "
            f"[{','.join(f'{h:02d}' for h in sorted(allowed_hours))}]"
        )
    return True, ""


def _parse_iso_utc(raw: Optional[str]) -> Optional[datetime]:
    """Parse a DB timestamp as UTC. Returns None if missing/unparseable."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        logger.warning("unparseable cooldown_until %r — ignoring", raw)
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def portfolio_gate(
    account: str,
    db_path=None,
    now_utc: Optional[datetime] = None,
) -> tuple[bool, str]:
    """Check freeze/cooldown state for an account. Returns (allowed, reason).

    - Account row missing or columns unset → allowed (fail-open, legacy
      accounts in oracle.db have no portfolio state yet)
    - portfolio_status in ('frozen', 'closed') → blocked
    - cooldown_until in the future → blocked (24h CB pause from manager)
    - cooldown_until expired → allowed (expiry is lazy, not written back)
    """
    from metty.core.db import get_account_portfolio_state

    state = get_account_portfolio_state(account, db_path)
    if state is None:
        return True, ""

    status = state.get("portfolio_status") or "running"
    if status in ("frozen", "closed"):
        reason = state.get("frozen_reason") or "no reason recorded"
        return False, f"portfolio_status={status} ({reason})"

    until = _parse_iso_utc(state.get("cooldown_until"))
    if until is not None:
        now = now_utc or datetime.now(timezone.utc)
        if now < until:
            return False, f"circuit-breaker cooldown until {until.isoformat()}"
        # expired cooldown is passable but worth one log line for the audit trail
        logger.info("[PortfolioGate] %s cooldown expired at %s — resuming", account, until.isoformat())

    return True, ""