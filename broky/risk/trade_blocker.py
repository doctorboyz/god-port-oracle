"""Trade blocker — hard pre-trade safety gate.

Single source of truth for "should we block this trade?" verdicts.
Each block returns (blocked: bool, reason: str). The aggregate function
combines all blocks and returns the first blocking reason.

These are PRE-TRADE blocks (called before order_send). They are
DIFFERENT from DrawdownProtector (which tracks PnL history) and
CircuitBreaker (which tracks loss streaks). TradeBlocker enforces
per-trade limits that the others miss:

  - daily_trade_count_limit: max N trades per UTC day (anti-churn)
  - hard_max_lots: refuse order if computed lots > hard cap
  - risk_pct_sanity: refuse if risk_per_trade_pct > sane max (e.g., 5%)
  - sl_distance_sanity: refuse if SL so tight that lots explode

This module is PURE — no I/O, no MT5, no DB. The caller passes in the
state; TradeBlocker decides. Easy to unit-test in isolation.

Usage:
    from broky.risk.trade_blocker import TradeBlocker, BlockInput

    blocker = TradeBlocker(
        daily_trade_count_limit=20,
        hard_max_lots=0.50,
        max_risk_pct=0.05,
        min_sl_distance_pct=0.05,
    )
    inp = BlockInput(
        open_positions=3, max_positions=5,
        daily_trades_today=18,
        lots=0.20, risk_pct=0.02,
        sl_distance_pct=0.30,
    )
    blocked, reason = blocker.check(inp)
    if blocked:
        # log + skip, do not order_send
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class BlockInput:
    """All state needed to decide whether to block a trade."""
    open_positions: int = 0
    max_positions: int = 5
    daily_trades_today: int = 0
    weekly_trades_this_week: int = 0
    lots: float = 0.0
    risk_pct: float = 0.0           # risk_per_trade as decimal (e.g., 0.02)
    sl_distance_pct: float = 0.0    # |entry - sl| / entry * 100
    equity: float = 0.0
    margin_required: float = 0.0
    free_margin: float = 0.0
    learning_mode: bool = False     # bypass non-critical blocks for data collection


@dataclass(frozen=True)
class BlockVerdict:
    blocked: bool
    reason: str
    block_name: str  # which check fired ("" if not blocked)


class TradeBlocker:
    """Hard pre-trade safety gate.

    Each check is independent. First blocking check wins (returns immediately).
    Order of checks is from most critical (account survival) to least.
    """

    def __init__(
        self,
        daily_trade_count_limit: int = 20,
        weekly_trade_count_limit: int = 80,
        hard_max_lots: float = 0.50,
        max_risk_pct: float = 0.05,        # refuse if risk_per_trade_pct > 5%
        min_sl_distance_pct: float = 0.05, # refuse if SL tighter than 0.05%
        max_sl_distance_pct: float = 5.0,  # refuse if SL wider than 5% (likely bug)
        margin_safety_factor: float = 0.8,  # block if margin_required > 80% of free_margin
    ):
        self.daily_trade_count_limit = daily_trade_count_limit
        self.weekly_trade_count_limit = weekly_trade_count_limit
        self.hard_max_lots = hard_max_lots
        self.max_risk_pct = max_risk_pct
        self.min_sl_distance_pct = min_sl_distance_pct
        self.max_sl_distance_pct = max_sl_distance_pct
        self.margin_safety_factor = margin_safety_factor

    def check(self, inp: BlockInput) -> BlockVerdict:
        """Run all block checks. Return first blocking verdict or pass."""
        # 1. Position limit — ALWAYS enforced, even in learning mode
        if inp.open_positions >= inp.max_positions:
            return BlockVerdict(
                blocked=True,
                reason=f"position limit ({inp.open_positions}/{inp.max_positions})",
                block_name="position_limit",
            )

        # 2. % Risk sanity — refuse if risk_per_trade misconfigured
        if inp.risk_pct > self.max_risk_pct:
            return BlockVerdict(
                blocked=True,
                reason=f"risk_per_trade {inp.risk_pct*100:.1f}% > max {self.max_risk_pct*100:.1f}%",
                block_name="risk_pct_sanity",
            )

        # 3. SL distance sanity — refuse if SL too tight (lots would explode)
        #    or too wide (likely config bug)
        if 0 < inp.sl_distance_pct < self.min_sl_distance_pct:
            return BlockVerdict(
                blocked=True,
                reason=f"SL distance {inp.sl_distance_pct:.3f}% < min {self.min_sl_distance_pct:.3f}% (lots would explode)",
                block_name="sl_too_tight",
            )
        if inp.sl_distance_pct > self.max_sl_distance_pct:
            return BlockVerdict(
                blocked=True,
                reason=f"SL distance {inp.sl_distance_pct:.2f}% > max {self.max_sl_distance_pct:.2f}% (likely config bug)",
                block_name="sl_too_wide",
            )

        # 4. Hard cap on lots — refuse if computed lots exceed hard cap
        if inp.lots > self.hard_max_lots:
            return BlockVerdict(
                blocked=True,
                reason=f"lots {inp.lots:.2f} > hard cap {self.hard_max_lots:.2f}",
                block_name="hard_max_lots",
            )

        # 5. Margin safety — block if margin required eats too much free margin
        if inp.margin_required > 0 and inp.free_margin > 0:
            if inp.margin_required > inp.free_margin * self.margin_safety_factor:
                return BlockVerdict(
                    blocked=True,
                    reason=f"margin ${inp.margin_required:.2f} > {self.margin_safety_factor*100:.0f}% of free margin ${inp.free_margin:.2f}",
                    block_name="margin_safety",
                )

        # 6. Daily trade count limit — anti-churn (ISSUE-02)
        #    Skipped in learning mode (data collection needs to cycle)
        if not inp.learning_mode and inp.daily_trades_today >= self.daily_trade_count_limit:
            return BlockVerdict(
                blocked=True,
                reason=f"daily trade count {inp.daily_trades_today}/{self.daily_trade_count_limit}",
                block_name="daily_trade_count",
            )

        # 7. Weekly trade count limit
        if not inp.learning_mode and inp.weekly_trades_this_week >= self.weekly_trade_count_limit:
            return BlockVerdict(
                blocked=True,
                reason=f"weekly trade count {inp.weekly_trades_this_week}/{self.weekly_trade_count_limit}",
                block_name="weekly_trade_count",
            )

        return BlockVerdict(blocked=False, reason="OK", block_name="")