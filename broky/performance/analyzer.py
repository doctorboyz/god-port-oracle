"""Daily trade analyzer — extract learnings from closed trades.

Queries live_trades to compute regime, session, direction, and indicator
effectiveness breakdowns. Produces a LearningReport used by the learning loop
to adjust signal generation parameters.

Design principle: this is about DATA COLLECTION and LEARNING, not about
being right every time. Every trade — win or loss — teaches us something.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from metty.core.db import get_connection

logger = logging.getLogger(__name__)


@dataclass
class RegimeStats:
    regime: str
    total_trades: int
    wins: int
    win_rate: float
    profit_factor: float
    avg_pnl_pct: float
    avg_holding_bars: float


@dataclass
class SessionStats:
    session: str
    total_trades: int
    wins: int
    win_rate: float
    profit_factor: float
    avg_pnl_pct: float


@dataclass
class DirectionStats:
    direction: str
    total_trades: int
    wins: int
    win_rate: float
    profit_factor: float


@dataclass
class IndicatorStats:
    indicator_name: str
    avg_in_wins: float
    avg_in_losses: float
    correlation_with_pnl: float
    suggestion: str  # "increase_weight", "decrease_weight", "neutral"


@dataclass
class LossClusterResult:
    max_consecutive_losses: int
    cluster_patterns: list[dict] = field(default_factory=list)
    most_detrimental_regime: Optional[str] = None
    most_detrimental_session: Optional[str] = None


@dataclass
class LearningReport:
    date: str
    mode: str  # "swing" or "scalp"
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    profit_factor: float
    total_pnl: float
    avg_pnl_pct: float
    regime_stats: dict[str, RegimeStats] = field(default_factory=dict)
    session_stats: dict[str, SessionStats] = field(default_factory=dict)
    direction_stats: dict[str, DirectionStats] = field(default_factory=dict)
    indicator_effectiveness: dict[str, IndicatorStats] = field(default_factory=dict)
    loss_clusters: Optional[LossClusterResult] = None
    parameter_suggestions: dict[str, float] = field(default_factory=dict)
    timestamp: str = ""


def _parse_indicator_scores(reason: str) -> dict[str, float]:
    """Extract indicator scores from the reason string.

    Format: "Score=-0.55 | adx=+0.5, macd=-1.0, ema_cross=-1.0, ..."
    """
    scores = {}
    # Match patterns like "adx=+0.5" or "macd=-1.0" or "volume=+0.5"
    for match in re.finditer(r"(\w+)=[+-]?([\d.]+)", reason):
        name = match.group(1)
        if name in ("Score",):
            continue
        try:
            scores[name] = float(match.group(2))
        except ValueError:
            continue
    return scores


class DailyAnalyzer:
    """Analyze closed trades and extract learnings."""

    def __init__(self, db_path: Path):
        self.db_path = db_path

    def analyze(
        self,
        date: Optional[str] = None,
        mode: Optional[str] = None,
    ) -> LearningReport:
        """Run full daily analysis.

        Args:
            date: Date string YYYY-MM-DD (default: yesterday UTC).
            mode: Filter by trading_mode "swing"/"scalp" (default: all).
        """
        if date is None:
            yesterday = datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            # Use yesterday for the report
            from datetime import timedelta
            yesterday = yesterday - timedelta(days=1)
            date = yesterday.strftime("%Y-%m-%d")

        trades = self._query_closed_trades(date, mode)

        if not trades:
            return LearningReport(
                date=date,
                mode=mode or "all",
                total_trades=0,
                wins=0,
                losses=0,
                win_rate=0.0,
                profit_factor=0.0,
                total_pnl=0.0,
                avg_pnl_pct=0.0,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )

        wins = [t for t in trades if t.get("pnl", 0) > 0]
        losses = [t for t in trades if t.get("pnl", 0) <= 0]

        gross_profit = sum(t.get("pnl", 0) for t in wins)
        gross_loss = abs(sum(t.get("pnl", 0) for t in losses))
        pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        total_pnl = sum(t.get("pnl", 0) for t in trades)
        avg_pnl_pct = sum(t.get("pnl_pct", 0) for t in trades) / len(trades)

        report = LearningReport(
            date=date,
            mode=mode or "all",
            total_trades=len(trades),
            wins=len(wins),
            losses=len(losses),
            win_rate=len(wins) / len(trades) if trades else 0,
            profit_factor=round(pf, 2),
            total_pnl=round(total_pnl, 2),
            avg_pnl_pct=round(avg_pnl_pct, 4),
            regime_stats=self._analyze_by_regime(trades),
            session_stats=self._analyze_by_session(trades),
            direction_stats=self._analyze_by_direction(trades),
            indicator_effectiveness=self._analyze_indicator_effectiveness(trades),
            loss_clusters=self._detect_loss_clusters(trades),
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        return report

    def _query_closed_trades(
        self, date: str, mode: Optional[str] = None
    ) -> list[dict]:
        """Query closed trades for a specific date."""
        conn = get_connection(self.db_path)
        query = """
            SELECT id, timestamp, direction, entry_price, exit_price,
                   stop_loss, take_profit, lot_size, confidence, regime,
                   session, d1_trend, reason, pnl, pnl_pct, exit_reason,
                   trading_mode, strategy_id, lot_size
            FROM live_trades
            WHERE is_open = 0 AND pnl IS NOT NULL
              AND timestamp >= ? AND timestamp < ?
        """
        params = [f"{date}%", f"{date}%"]

        # Next day boundary
        from datetime import timedelta
        next_day = (
            datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)
        ).strftime("%Y-%m-%d")
        params = [date, next_day]

        if mode:
            query += " AND trading_mode = ?"
            params.append(mode)

        query += " ORDER BY timestamp ASC"
        rows = conn.execute(query, params).fetchall()
        columns = [desc[0] for desc in conn.execute(query, params).description]
        conn.close()

        trades = []
        for row in rows:
            trades.append(dict(zip(columns, row)))
        return trades

    def _compute_stats(self, trades: list[dict]) -> dict:
        """Compute basic stats for a group of trades."""
        if not trades:
            return {
                "total_trades": 0, "wins": 0, "win_rate": 0,
                "profit_factor": 0, "avg_pnl_pct": 0,
            }
        wins = [t for t in trades if t.get("pnl", 0) > 0]
        gross_profit = sum(t.get("pnl", 0) for t in wins)
        gross_loss = abs(sum(t.get("pnl", 0) for t in trades if t.get("pnl", 0) <= 0))
        pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")
        avg_pnl_pct = sum(t.get("pnl_pct", 0) for t in trades) / len(trades)
        return {
            "total_trades": len(trades),
            "wins": len(wins),
            "win_rate": round(len(wins) / len(trades), 4),
            "profit_factor": round(pf, 2),
            "avg_pnl_pct": round(avg_pnl_pct, 4),
        }

    def _analyze_by_regime(self, trades: list[dict]) -> dict[str, RegimeStats]:
        """WR, PF, avg PnL by regime (trending/ranging/volatile)."""
        groups = {}
        for t in trades:
            regime = t.get("regime", "unknown") or "unknown"
            groups.setdefault(regime, []).append(t)

        result = {}
        for regime, group_trades in groups.items():
            stats = self._compute_stats(group_trades)
            avg_holding = 0.0
            result[regime] = RegimeStats(
                regime=regime,
                total_trades=stats["total_trades"],
                wins=stats["wins"],
                win_rate=stats["win_rate"],
                profit_factor=stats["profit_factor"],
                avg_pnl_pct=stats["avg_pnl_pct"],
                avg_holding_bars=avg_holding,
            )
        return result

    def _analyze_by_session(self, trades: list[dict]) -> dict[str, SessionStats]:
        """WR, PF, avg PnL by session."""
        groups = {}
        for t in trades:
            session = t.get("session", "unknown") or "unknown"
            groups.setdefault(session, []).append(t)

        result = {}
        for session, group_trades in groups.items():
            stats = self._compute_stats(group_trades)
            result[session] = SessionStats(
                session=session,
                total_trades=stats["total_trades"],
                wins=stats["wins"],
                win_rate=stats["win_rate"],
                profit_factor=stats["profit_factor"],
                avg_pnl_pct=stats["avg_pnl_pct"],
            )
        return result

    def _analyze_by_direction(self, trades: list[dict]) -> dict[str, DirectionStats]:
        """WR, PF by direction (BUY/SELL)."""
        groups = {}
        for t in trades:
            direction = t.get("direction", "unknown") or "unknown"
            groups.setdefault(direction, []).append(t)

        result = {}
        for direction, group_trades in groups.items():
            stats = self._compute_stats(group_trades)
            result[direction] = DirectionStats(
                direction=direction,
                total_trades=stats["total_trades"],
                wins=stats["wins"],
                win_rate=stats["win_rate"],
                profit_factor=stats["profit_factor"],
            )
        return result

    def _analyze_indicator_effectiveness(
        self, trades: list[dict]
    ) -> dict[str, IndicatorStats]:
        """Which indicator scores correlated with winning trades.

        Parses the reason string to extract individual indicator scores,
        then computes average scores in wins vs losses and correlation with PnL.
        """
        # Extract scores from all trades
        trade_scores = []
        for t in trades:
            reason = t.get("reason", "")
            if not reason:
                continue
            scores = _parse_indicator_scores(reason)
            if scores:
                trade_scores.append({"pnl": t.get("pnl", 0), "pnl_pct": t.get("pnl_pct", 0), "scores": scores})

        if len(trade_scores) < 5:
            return {}

        # Collect all indicator names
        all_indicators = set()
        for ts in trade_scores:
            all_indicators.update(ts["scores"].keys())

        wins_data = [ts for ts in trade_scores if ts["pnl"] > 0]
        losses_data = [ts for ts in trade_scores if ts["pnl"] <= 0]

        result = {}
        for indicator in sorted(all_indicators):
            win_scores = [ts["scores"].get(indicator, 0) for ts in wins_data if indicator in ts["scores"]]
            loss_scores = [ts["scores"].get(indicator, 0) for ts in losses_data if indicator in ts["scores"]]

            avg_wins = sum(win_scores) / len(win_scores) if win_scores else 0
            avg_losses = sum(loss_scores) / len(loss_scores) if loss_scores else 0

            # Pearson correlation between indicator score and pnl_pct
            all_pairs = [(ts["scores"].get(indicator, 0), ts["pnl_pct"]) for ts in trade_scores if indicator in ts["scores"]]
            correlation = self._pearson_r(all_pairs) if len(all_pairs) >= 5 else 0

            if abs(correlation) > 0.2:
                suggestion = "increase_weight" if correlation > 0 else "decrease_weight"
            else:
                suggestion = "neutral"

            result[indicator] = IndicatorStats(
                indicator_name=indicator,
                avg_in_wins=round(avg_wins, 3),
                avg_in_losses=round(avg_losses, 3),
                correlation_with_pnl=round(correlation, 3),
                suggestion=suggestion,
            )
        return result

    @staticmethod
    def _pearson_r(pairs: list[tuple[float, float]]) -> float:
        """Compute Pearson correlation coefficient."""
        n = len(pairs)
        if n < 2:
            return 0
        xs = [p[0] for p in pairs]
        ys = [p[1] for p in pairs]
        mean_x = sum(xs) / n
        mean_y = sum(ys) / n
        cov = sum((x - mean_x) * (y - mean_y) for x, y in pairs)
        var_x = sum((x - mean_x) ** 2 for x in xs)
        var_y = sum((y - mean_y) ** 2 for y in ys)
        denom = (var_x * var_y) ** 0.5
        return cov / denom if denom > 0 else 0

    def _detect_loss_clusters(self, trades: list[dict]) -> LossClusterResult:
        """Detect consecutive loss streaks and common patterns."""
        if not trades:
            return LossClusterResult(max_consecutive_losses=0)

        max_streak = 0
        current_streak = 0
        streak_trades: list[dict] = []
        all_loss_patterns: list[dict] = []

        for t in trades:
            if t.get("pnl", 0) <= 0:
                current_streak += 1
                streak_trades.append(t)
                max_streak = max(max_streak, current_streak)
            else:
                if current_streak >= 3 and streak_trades:
                    # Extract common features from the loss streak
                    regimes = [s.get("regime", "") for s in streak_trades]
                    sessions = [s.get("session", "") for s in streak_trades]
                    directions = [s.get("direction", "") for s in streak_trades]
                    all_loss_patterns.append({
                        "length": current_streak,
                        "regimes": list(set(regimes)),
                        "sessions": list(set(sessions)),
                        "directions": list(set(directions)),
                    })
                current_streak = 0
                streak_trades = []

        # Check final streak
        if current_streak >= 3 and streak_trades:
            regimes = [s.get("regime", "") for s in streak_trades]
            sessions = [s.get("session", "") for s in streak_trades]
            directions = [s.get("direction", "") for s in streak_trades]
            all_loss_patterns.append({
                "length": current_streak,
                "regimes": list(set(regimes)),
                "sessions": list(set(sessions)),
                "directions": list(set(directions)),
            })

        # Find most detrimental regime/session across all losses
        loss_trades = [t for t in trades if t.get("pnl", 0) <= 0]
        worst_regime = None
        worst_session = None
        if loss_trades:
            regime_counts: dict[str, int] = {}
            session_counts: dict[str, int] = {}
            for t in loss_trades:
                r = t.get("regime", "unknown") or "unknown"
                s = t.get("session", "unknown") or "unknown"
                regime_counts[r] = regime_counts.get(r, 0) + 1
                session_counts[s] = session_counts.get(s, 0) + 1
            if regime_counts:
                worst_regime = max(regime_counts, key=regime_counts.get)
            if session_counts:
                worst_session = max(session_counts, key=session_counts.get)

        return LossClusterResult(
            max_consecutive_losses=max_streak,
            cluster_patterns=all_loss_patterns,
            most_detrimental_regime=worst_regime,
            most_detrimental_session=worst_session,
        )