"""Circuit breaker — stops trading when losses exceed thresholds.

Implements the risk management rules from broky/config/risk.yaml:
- Level 1: Stop new signals during high volatility
- Level 2: Cancel pending orders
- Level 3: Close all positions on flash crash (10% drop in 5 min)

Plus daily loss limit and consecutive loss cooldown.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Optional

from shared.models import CircuitBreakerState


class CircuitBreaker:
    """Manages circuit breaker state for risk management.

    Tracks consecutive losses, daily PnL, and flash crash detection.
    When triggered, blocks new trades until cooldown expires.

    Example:
        >>> cb = CircuitBreaker()
        >>> cb.record_loss()
        >>> cb.is_active
        False
        >>> cb.record_loss()
        >>> cb.record_loss()
        >>> cb.is_active  # 5 consecutive losses triggers cooldown
        True
    """

    def __init__(
        self,
        daily_loss_limit_pct: float = 0.05,
        consecutive_loss_limit: int = 5,
        cooldown_minutes: int = 15,
        flash_crash_drop_pct: float = 0.10,
    ):
        self._state = CircuitBreakerState()
        self._daily_loss_limit_pct = daily_loss_limit_pct
        self._consecutive_loss_limit = consecutive_loss_limit
        self._cooldown_minutes = cooldown_minutes
        self._flash_crash_drop_pct = flash_crash_drop_pct
        self._daily_pnl: float = 0.0
        self._daily_start_equity: Optional[float] = None
        self._current_time: Optional[datetime] = None

    @property
    def state(self) -> CircuitBreakerState:
        return self._state

    @property
    def is_active(self) -> bool:
        """Check if circuit breaker is currently active (blocking trades)."""
        if self._state.is_active:
            # Check if cooldown has expired
            now = self._current_time or datetime.now(timezone.utc)
            if self._state.cooldown_until and now >= self._state.cooldown_until:
                self._reset()
                return False
            return True
        return False

    def record_loss(self, pnl: float = 0.0, equity: Optional[float] = None) -> bool:
        """Record a trade loss. Returns True if circuit breaker is triggered.

        Args:
            pnl: The PnL of the losing trade (negative value).
            equity: Current account equity (for daily loss calculation).

        Returns:
            True if circuit breaker was triggered, False otherwise.
        """
        self._state.consecutive_losses += 1
        self._daily_pnl += pnl

        if equity and equity > 0:
            if self._daily_start_equity is None:
                self._daily_start_equity = equity
            daily_loss_pct = abs(self._daily_pnl) / self._daily_start_equity
            self._state.daily_loss_pct = daily_loss_pct

            # Check daily loss limit
            if daily_loss_pct >= self._daily_loss_limit_pct:
                self._activate("Daily loss limit exceeded")
                return True

        # Check consecutive loss limit
        if self._state.consecutive_losses >= self._consecutive_loss_limit:
            self._activate(f"{self._consecutive_loss_limit} consecutive losses")
            return True

        return False

    def record_win(self, pnl: float = 0.0) -> None:
        """Record a winning trade — resets consecutive loss counter."""
        self._state.consecutive_losses = 0
        self._daily_pnl += pnl

    def check_flash_crash(self, price_drop_pct: float) -> bool:
        """Check if a flash crash has occurred.

        Args:
            price_drop_pct: Percentage price drop in the last 5 minutes.

        Returns:
            True if flash crash detected (Level 3 circuit breaker).
        """
        if abs(price_drop_pct) >= self._flash_crash_drop_pct * 100:
            self._state.flash_crash_detected = True
            self._activate(f"Flash crash: {price_drop_pct:.1f}% drop")
            return True
        return False

    def can_open_trade(self, equity: Optional[float] = None) -> tuple[bool, str]:
        """Check if a new trade can be opened.

        Args:
            equity: Current account equity for daily loss check.

        Returns:
            Tuple of (can_trade, reason).
        """
        if self.is_active:
            return False, "Circuit breaker active"

        if equity and self._daily_start_equity:
            daily_loss_pct = abs(self._daily_pnl) / self._daily_start_equity
            if daily_loss_pct >= self._daily_loss_limit_pct:
                self._activate("Daily loss limit reached on check")
                return False, "Daily loss limit exceeded"

        return True, "OK"

    def _activate(self, reason: str) -> None:
        self._state.is_active = True
        now = self._current_time or datetime.now(timezone.utc)
        self._state.cooldown_until = now + timedelta(minutes=self._cooldown_minutes)

    def _reset(self) -> None:
        self._state.is_active = False
        self._state.consecutive_losses = 0
        self._state.cooldown_until = None
        self._state.flash_crash_detected = False

    def reset_daily(self) -> None:
        """Reset daily PnL tracking — call at start of each trading day."""
        self._daily_pnl = 0.0
        self._daily_start_equity = None
        self._state.daily_loss_pct = 0.0

    def set_time(self, timestamp: datetime) -> None:
        """Set the simulated time for backtest cooldown checks.

        Without this, the circuit breaker uses real wall-clock time,
        which never advances in a backtest, so cooldowns never expire.
        """
        self._current_time = timestamp