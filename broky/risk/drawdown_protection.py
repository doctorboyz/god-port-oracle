"""Drawdown protection — multi-level loss limits for real-money accounts.

Prevents account blow-up by enforcing:
1. Daily drawdown limit — stop trading if daily loss exceeds X%
2. Weekly drawdown limit — stop trading if weekly loss exceeds Y%
3. Account drawdown limit — stop trading if equity drops below Z% of initial

Designed for small real accounts ($100-$500) where risk per trade is
disproportionately large relative to balance (10-13% on $100 at 0.01 lot).

Usage:
    protector = DrawdownProtector(
        initial_equity=101.11,
        daily_limit_pct=0.20,    # 20% daily loss = $20
        weekly_limit_pct=0.30,  # 30% weekly loss = $30
        account_limit_pct=0.30, # 30% total drawdown = $70 min equity
    )

    # Before each trade:
    can_trade, reason = protector.check(equity=99.50)

    # After each trade closes:
    protector.record_pnl(pnl=-5.20, equity=94.30)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class DrawdownState:
    """Tracks drawdown metrics across daily, weekly, and account levels."""

    # Daily tracking
    daily_pnl: float = 0.0
    daily_start_equity: float = 0.0
    daily_start: Optional[datetime] = None

    # Weekly tracking
    weekly_pnl: float = 0.0
    weekly_start_equity: float = 0.0
    weekly_start: Optional[datetime] = None

    # Account tracking
    initial_equity: float = 0.0
    peak_equity: float = 0.0

    # Status
    blocked: bool = False
    block_reason: str = ""

    # Trade count
    daily_trades: int = 0
    weekly_trades: int = 0


class DrawdownProtector:
    """Multi-level drawdown protection for real-money accounts.

    Three layers of protection:
    1. Daily — caps loss per trading day
    2. Weekly — caps loss per trading week
    3. Account — caps total drawdown from peak equity

    Each layer can independently block new trades.
    """

    def __init__(
        self,
        initial_equity: float = 100.0,
        daily_limit_pct: float = 0.20,
        weekly_limit_pct: float = 0.30,
        account_limit_pct: float = 0.30,
        cooldown_hours: int = 4,
    ):
        self._daily_limit_pct = daily_limit_pct
        self._weekly_limit_pct = weekly_limit_pct
        self._account_limit_pct = account_limit_pct
        self._cooldown_hours = cooldown_hours
        self._state = DrawdownState(
            initial_equity=initial_equity,
            peak_equity=initial_equity,
        )
        self._blocked_until: Optional[datetime] = None

    @property
    def state(self) -> DrawdownState:
        return self._state

    @property
    def is_blocked(self) -> bool:
        """Check if trading is currently blocked."""
        if self._state.blocked:
            # Check if cooldown has expired
            if self._blocked_until and datetime.now(timezone.utc) >= self._blocked_until:
                self._unblock()
                return False
            return True
        return False

    def check(self, equity: float) -> tuple[bool, str]:
        """Check if a new trade is allowed.

        Args:
            equity: Current account equity.

        Returns:
            Tuple of (can_trade, reason).
        """
        now = datetime.now(timezone.utc)

        # Initialize tracking on first call
        if self._state.daily_start is None:
            self._init_period(now, equity)

        # Check day rollover
        self._check_rollover(now)

        # 1. Account drawdown — hard stop, never auto-resets
        if self._state.initial_equity > 0:
            min_equity = self._state.initial_equity * (1 - self._account_limit_pct)
            if equity <= min_equity:
                self._block(f"Account drawdown limit: equity ${equity:.2f} <= ${min_equity:.2f} ({self._account_limit_pct*100:.0f}% from ${self._state.initial_equity:.2f})", permanent=True)
                return False, self._state.block_reason

        # 2. Check peak equity drawdown
        if self._state.peak_equity > 0:
            drawdown_from_peak = (self._state.peak_equity - equity) / self._state.peak_equity
            if drawdown_from_peak >= self._account_limit_pct:
                self._block(f"Peak drawdown limit: {drawdown_from_peak*100:.1f}% from peak ${self._state.peak_equity:.2f}", permanent=True)
                return False, self._state.block_reason

        # 3. Already blocked (cooldown not expired)
        if self.is_blocked:
            return False, self._state.block_reason

        # 4. Daily drawdown check
        if self._state.daily_start_equity > 0:
            daily_loss_pct = abs(self._state.daily_pnl) / self._state.daily_start_equity
            if self._state.daily_pnl < 0 and daily_loss_pct >= self._daily_limit_pct:
                daily_loss_amt = abs(self._state.daily_pnl)
                limit_amt = self._state.daily_start_equity * self._daily_limit_pct
                self._block(f"Daily drawdown limit: -${daily_loss_amt:.2f} >= -${limit_amt:.2f} ({self._daily_limit_pct*100:.0f}%)")
                return False, self._state.block_reason

        # 5. Weekly drawdown check
        if self._state.weekly_start_equity > 0:
            weekly_loss_pct = abs(self._state.weekly_pnl) / self._state.weekly_start_equity
            if self._state.weekly_pnl < 0 and weekly_loss_pct >= self._weekly_limit_pct:
                weekly_loss_amt = abs(self._state.weekly_pnl)
                limit_amt = self._state.weekly_start_equity * self._weekly_limit_pct
                self._block(f"Weekly drawdown limit: -${weekly_loss_amt:.2f} >= -${limit_amt:.2f} ({self._weekly_limit_pct*100:.0f}%)")
                return False, self._state.block_reason

        # Update peak equity if equity is higher
        if equity > self._state.peak_equity:
            self._state.peak_equity = equity

        return True, "OK"

    def record_pnl(self, pnl: float, equity: float) -> bool:
        """Record a trade result and update drawdown tracking.

        Args:
            pnl: Profit/loss of the closed trade.
            equity: Current account equity after the trade.

        Returns:
            True if drawdown protection was triggered.
        """
        self._state.daily_pnl += pnl
        self._state.weekly_pnl += pnl
        self._state.daily_trades += 1
        self._state.weekly_trades += 1

        # Update peak equity
        if equity > self._state.peak_equity:
            self._state.peak_equity = equity

        # Re-check after recording
        can_trade, reason = self.check(equity)
        if not can_trade:
            logger.warning(
                "[DrawdownProtection] Trade recorded PnL=%.2f → BLOCKED: %s "
                "(daily=%.2f, weekly=%.2f, equity=%.2f, peak=%.2f)",
                pnl, reason,
                self._state.daily_pnl, self._state.weekly_pnl,
                equity, self._state.peak_equity,
            )
            return True
        return False

    def _init_period(self, now: datetime, equity: float) -> None:
        """Initialize daily/weekly tracking periods."""
        # Daily: start at 00:00 UTC
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        self._state.daily_start = day_start
        self._state.daily_start_equity = equity
        self._state.daily_pnl = 0.0
        self._state.daily_trades = 0

        # Weekly: start at Monday 00:00 UTC
        days_since_monday = now.weekday()
        week_start = day_start - timedelta(days=days_since_monday)
        self._state.weekly_start = week_start
        self._state.weekly_start_equity = equity
        self._state.weekly_pnl = 0.0
        self._state.weekly_trades = 0

        if self._state.initial_equity == 0:
            self._state.initial_equity = equity
        if self._state.peak_equity == 0:
            self._state.peak_equity = equity

        logger.info(
            "[DrawdownProtection] Initialized: equity=%.2f, daily_limit=%.0f%%, "
            "weekly_limit=%.0f%%, account_limit=%.0f%%",
            equity,
            self._daily_limit_pct * 100,
            self._weekly_limit_pct * 100,
            self._account_limit_pct * 100,
        )

    def _check_rollover(self, now: datetime) -> None:
        """Check if daily/weekly periods need to reset."""
        if self._state.daily_start is None:
            return

        # Daily rollover at 00:00 UTC
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if day_start > self._state.daily_start:
            # Reset daily tracking — use current equity as new base
            self._state.daily_start = day_start
            self._state.daily_pnl = 0.0
            self._state.daily_trades = 0
            # Daily start equity = yesterday's close equity
            # We don't have yesterday's close, so use peak/current as proxy
            self._state.daily_start_equity = self._state.peak_equity
            logger.info(
                "[DrawdownProtection] Daily reset: equity_base=%.2f",
                self._state.daily_start_equity,
            )

        # Weekly rollover on Monday 00:00 UTC
        days_since_monday = now.weekday()
        week_start = day_start - timedelta(days=days_since_monday)
        if self._state.weekly_start and week_start > self._state.weekly_start:
            self._state.weekly_start = week_start
            self._state.weekly_pnl = 0.0
            self._state.weekly_trades = 0
            self._state.weekly_start_equity = self._state.peak_equity
            logger.info(
                "[DrawdownProtection] Weekly reset: equity_base=%.2f",
                self._state.weekly_start_equity,
            )

    def _block(self, reason: str, permanent: bool = False) -> None:
        """Block trading with optional cooldown."""
        self._state.blocked = True
        self._state.block_reason = reason

        if permanent:
            # Account drawdown = hard stop, no auto-unblock
            self._blocked_until = None
            logger.critical("[DrawdownProtection] PERMANENT BLOCK: %s", reason)
        else:
            # Daily/weekly = cooldown period then resume
            self._blocked_until = datetime.now(timezone.utc) + timedelta(hours=self._cooldown_hours)
            logger.warning(
                "[DrawdownProtection] BLOCKED for %dh: %s (resumes after %s)",
                self._cooldown_hours, reason, self._blocked_until,
            )

    def _unblock(self) -> None:
        """Unblock trading after cooldown expires."""
        self._state.blocked = False
        self._state.block_reason = ""
        self._blocked_until = None
        # Reset daily tracking on unblock to avoid double-counting
        now = datetime.now(timezone.utc)
        self._state.daily_pnl = 0.0
        self._state.daily_trades = 0
        logger.info("[DrawdownProtection] Unblocked — trading resumed")

    def get_status(self) -> dict:
        """Get current drawdown protection status for monitoring."""
        return {
            "blocked": self.is_blocked,
            "block_reason": self._state.block_reason,
            "initial_equity": self._state.initial_equity,
            "peak_equity": self._state.peak_equity,
            "daily_pnl": round(self._state.daily_pnl, 2),
            "daily_trades": self._state.daily_trades,
            "weekly_pnl": round(self._state.weekly_pnl, 2),
            "weekly_trades": self._state.weekly_trades,
            "daily_limit_pct": self._daily_limit_pct * 100,
            "weekly_limit_pct": self._weekly_limit_pct * 100,
            "account_limit_pct": self._account_limit_pct * 100,
            "blocked_until": self._blocked_until.isoformat() if self._blocked_until else None,
        }


# Per-account drawdown protection configs
# Account A = real money ($101) → strict protection
# Account B/C = demo → lenient (let demo run freely for data collection)
ACCOUNT_DRAWDOWN_CONFIGS = {
    "A": {
        "daily_limit_pct": 0.20,     # 20% daily loss = ~$20
        "weekly_limit_pct": 0.30,    # 30% weekly loss = ~$30
        "account_limit_pct": 0.30,   # 30% total drawdown → stop at ~$70 equity
        "cooldown_hours": 4,         # 4 hour cooldown after daily/weekly limit hit
    },
    "B": {
        "daily_limit_pct": 0.10,     # 10% daily loss (demo still needs some limit)
        "weekly_limit_pct": 0.20,    # 20% weekly loss
        "account_limit_pct": 0.50,   # 50% total drawdown
        "cooldown_hours": 2,
    },
    "C": {
        "daily_limit_pct": 0.10,
        "weekly_limit_pct": 0.20,
        "account_limit_pct": 0.50,
        "cooldown_hours": 2,
    },
}

# BUY signal confidence filter for real accounts
# BUY has lower WR than SELL, so require higher confidence
BUY_MIN_CONFIDENCE = {
    "A": 0.50,  # Real account: stricter BUY filter (0.50 vs 0.45 for SELL)
    "B": 0.45,  # Demo: same as global
    "C": 0.45,  # Demo: same as global
}