"""Parameter adjuster — safely evolves indicator weights from daily learning reports.

Safety principles (from Kappa Principle 7: Protect the Portfolio):
- Max weight change per day: 0.03 (3%) — gradual evolution, never revolution
- Confidence floor: 0.55 — never lower than this, signals must remain meaningful
- Minimum sample size: 10 trades before adjusting anything
- Minimum correlation threshold: |r| > 0.15 before suggesting a change
- Weight bounds: every weight stays in [0.02, 0.50] — no indicator dominates or vanishes
- Weights must sum to 1.0 after adjustment

The adjuster produces a new weight dict and logs the change history to DB
so we can audit every evolution step.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from broky.performance.analyzer import LearningReport

logger = logging.getLogger(__name__)

# Safety bounds
MAX_WEIGHT_CHANGE_PER_DAY = 0.03  # Max absolute change per weight per day
MIN_WEIGHT = 0.02  # No weight can drop below this
MAX_WEIGHT = 0.50  # No weight can exceed this
MIN_SAMPLE_SIZE = 10  # Minimum trades before adjusting
MIN_CORRELATION_THRESHOLD = 0.15  # Minimum |r| to suggest a change
MIN_CONFIDENCE_FLOOR = 0.55  # Never lower confidence below this

# Default weights (broky/signals/generator.py INDICATOR_WEIGHTS)
DEFAULT_WEIGHTS: dict[str, float] = {
    "ema_cross": 0.15,
    "ema_trend": 0.05,
    "adx": 0.15,
    "macd": 0.35,
    "bollinger": 0.10,
    "volume": 0.15,
}


@dataclass
class WeightAdjustment:
    """A single weight adjustment with before/after and reason."""
    indicator: str
    old_weight: float
    new_weight: float
    delta: float
    reason: str  # "increase_weight", "decrease_weight", "neutral", "safety_cap", "min_floor"


@dataclass
class AdjustmentResult:
    """Result of a parameter adjustment cycle."""
    date: str
    mode: str
    adjustments: list[WeightAdjustment] = field(default_factory=list)
    new_weights: dict[str, float] = field(default_factory=dict)
    confidence_floor: float = MIN_CONFIDENCE_FLOOR
    skipped: bool = False
    skip_reason: str = ""
    timestamp: str = ""


class ParameterAdjuster:
    """Adjusts indicator weights based on daily learning reports.

    Design principle: conservative evolution. Small changes, always bounded,
    never destructive. The portfolio must survive every adjustment.
    """

    def __init__(
        self,
        current_weights: Optional[dict[str, float]] = None,
        max_change: float = MAX_WEIGHT_CHANGE_PER_DAY,
        min_weight: float = MIN_WEIGHT,
        max_weight: float = MAX_WEIGHT,
        min_sample: int = MIN_SAMPLE_SIZE,
        min_correlation: float = MIN_CORRELATION_THRESHOLD,
        confidence_floor: float = MIN_CONFIDENCE_FLOOR,
        db_path: Optional[Path] = None,
    ):
        self.weights = dict(current_weights or DEFAULT_WEIGHTS)
        self.max_change = max_change
        self.min_weight = min_weight
        self.max_weight = max_weight
        self.min_sample = min_sample
        self.min_correlation = min_correlation
        self.confidence_floor = confidence_floor
        self.db_path = db_path

    def adjust(self, report: LearningReport) -> AdjustmentResult:
        """Compute weight adjustments from a learning report.

        Args:
            report: Daily learning report from DailyAnalyzer.

        Returns:
            AdjustmentResult with new weights and adjustment details.
        """
        now = datetime.now(timezone.utc).isoformat()

        # Skip if insufficient data
        if report.total_trades < self.min_sample:
            return AdjustmentResult(
                date=report.date,
                mode=report.mode,
                new_weights=dict(self.weights),
                skipped=True,
                skip_reason=f"Insufficient trades: {report.total_trades} < {self.min_sample}",
                timestamp=now,
            )

        # Skip if win rate is catastrophically low — don't learn from chaos
        if report.win_rate < 0.25:
            return AdjustmentResult(
                date=report.date,
                mode=report.mode,
                new_weights=dict(self.weights),
                skipped=True,
                skip_reason=f"Win rate too low ({report.win_rate:.1%}) — not learning from chaos",
                timestamp=now,
            )

        adjustments: list[WeightAdjustment] = []
        new_weights = dict(self.weights)

        # Apply indicator effectiveness suggestions
        for indicator, stats in report.indicator_effectiveness.items():
            if indicator not in new_weights:
                continue  # Unknown indicator, skip

            old_weight = new_weights[indicator]
            suggestion = stats.suggestion
            correlation = abs(stats.correlation_with_pnl)

            # Only adjust if correlation is meaningful
            if correlation < self.min_correlation:
                adjustments.append(WeightAdjustment(
                    indicator=indicator,
                    old_weight=old_weight,
                    new_weight=old_weight,
                    delta=0.0,
                    reason=f"neutral (correlation {correlation:.3f} < {self.min_correlation})",
                ))
                continue

            # Compute desired change
            # Correlation magnitude determines change size (scaled to max_change)
            change_scale = min(correlation / 0.5, 1.0)  # Normalize: 0.5 correlation = full change
            change_amount = self.max_change * change_scale

            if suggestion == "increase_weight":
                desired = old_weight + change_amount
                reason = f"increase_weight (r={stats.correlation_with_pnl:.3f})"
            elif suggestion == "decrease_weight":
                desired = old_weight - change_amount
                reason = f"decrease_weight (r={stats.correlation_with_pnl:.3f})"
            else:
                adjustments.append(WeightAdjustment(
                    indicator=indicator,
                    old_weight=old_weight,
                    new_weight=old_weight,
                    delta=0.0,
                    reason=f"neutral (suggestion={suggestion})",
                ))
                continue

            # Clamp to bounds
            clamped = max(self.min_weight, min(self.max_weight, desired))
            actual_delta = clamped - old_weight

            # Safety: enforce max change even after clamping
            if abs(actual_delta) > self.max_change:
                actual_delta = self.max_change if actual_delta > 0 else -self.max_change
                clamped = old_weight + actual_delta

            adjustment_reason = reason
            if clamped != desired:
                if clamped == self.min_weight:
                    adjustment_reason += " [min_floor]"
                elif clamped == self.max_weight:
                    adjustment_reason += " [safety_cap]"

            new_weights[indicator] = round(clamped, 4)
            adjustments.append(WeightAdjustment(
                indicator=indicator,
                old_weight=old_weight,
                new_weight=round(clamped, 4),
                delta=round(actual_delta, 4),
                reason=adjustment_reason,
            ))

        # Renormalize weights to sum to 1.0
        total = sum(new_weights.values())
        if total > 0:
            new_weights = {k: round(v / total, 4) for k, v in new_weights.items()}
            # Fix rounding: add/subtract remainder from the largest weight
            remainder = round(1.0 - sum(new_weights.values()), 4)
            if abs(remainder) > 0 and new_weights:
                largest = max(new_weights, key=new_weights.get)
                new_weights[largest] = round(new_weights[largest] + remainder, 4)

        result = AdjustmentResult(
            date=report.date,
            mode=report.mode,
            adjustments=adjustments,
            new_weights=new_weights,
            confidence_floor=self.confidence_floor,
            timestamp=now,
        )

        # Log to DB if available
        if self.db_path:
            self._save_to_db(result)

        # Update internal weights for next cycle
        self.weights = dict(new_weights)

        return result

    def adjust_from_loss_clusters(self, report: LearningReport) -> dict[str, str]:
        """Generate session/regime avoidance suggestions from loss clusters.

        This doesn't change weights — it produces qualitative suggestions
        that can be applied as session multipliers or regime filters.

        Returns:
            Dict of suggestion_name → description.
        """
        suggestions: dict[str, str] = {}

        if report.loss_clusters is None:
            return suggestions

        # Consecutive loss streak warning
        if report.loss_clusters.max_consecutive_losses >= 5:
            suggestions["circuit_breaker_review"] = (
                f"Max consecutive losses: {report.loss_clusters.max_consecutive_losses}. "
                "Consider tightening circuit breaker threshold."
            )

        # Detrimental regime
        if report.loss_clusters.most_detrimental_regime:
            regime = report.loss_clusters.most_detrimental_regime
            suggestions[f"avoid_regime_{regime}"] = (
                f"Most losses occurred in {regime} regime. "
                "Consider adding regime filter or reducing position size."
            )

        # Detrimental session
        if report.loss_clusters.most_detrimental_session:
            session = report.loss_clusters.most_detrimental_session
            suggestions[f"reduce_session_{session}"] = (
                f"Most losses occurred in {session} session. "
                "Consider reducing session multiplier or skipping this session."
            )

        # Cluster patterns
        for pattern in report.loss_clusters.cluster_patterns:
            if pattern.get("length", 0) >= 4:
                suggestions[f"cluster_{pattern['length']}losses"] = (
                    f"Cluster of {pattern['length']} consecutive losses in "
                    f"{pattern.get('regimes', '?')} regime, "
                    f"{pattern.get('sessions', '?')} session. "
                    "Review entry conditions for these conditions."
                )

        return suggestions

    def _save_to_db(self, result: AdjustmentResult) -> None:
        """Save adjustment result to learning_params_history table."""
        from metty.core.db import get_connection

        conn = get_connection(self.db_path)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS learning_params_history (
                    id INTEGER PRIMARY KEY,
                    date TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    old_weights TEXT NOT NULL,
                    new_weights TEXT NOT NULL,
                    adjustments TEXT NOT NULL,
                    skipped INTEGER NOT NULL DEFAULT 0,
                    skip_reason TEXT,
                    timestamp TEXT NOT NULL
                )
            """)
            conn.execute(
                """INSERT INTO learning_params_history
                   (date, mode, old_weights, new_weights, adjustments, skipped, skip_reason, timestamp)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    result.date,
                    result.mode,
                    json.dumps(self.weights),
                    json.dumps(result.new_weights),
                    json.dumps([{
                        "indicator": a.indicator,
                        "old": a.old_weight,
                        "new": a.new_weight,
                        "delta": a.delta,
                        "reason": a.reason,
                    } for a in result.adjustments]),
                    1 if result.skipped else 0,
                    result.skip_reason,
                    result.timestamp,
                ),
            )
            conn.commit()
        except Exception as e:
            logger.error("Failed to save learning params to DB: %s", e)
        finally:
            conn.close()

    @classmethod
    def load_weights_from_db(cls, db_path: Path, mode: str = "swing") -> dict[str, float]:
        """Load the most recent weights from DB for a given mode.

        Returns DEFAULT_WEIGHTS if no history exists.
        """
        from metty.core.db import get_connection

        conn = get_connection(db_path)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS learning_params_history (
                    id INTEGER PRIMARY KEY,
                    date TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    old_weights TEXT NOT NULL,
                    new_weights TEXT NOT NULL,
                    adjustments TEXT NOT NULL,
                    skipped INTEGER NOT NULL DEFAULT 0,
                    skip_reason TEXT,
                    timestamp TEXT NOT NULL
                )
            """)
            row = conn.execute(
                """SELECT new_weights FROM learning_params_history
                   WHERE mode = ? AND skipped = 0
                   ORDER BY id DESC LIMIT 1""",
                (mode,),
            ).fetchone()
            if row:
                return json.loads(row[0])
            return dict(DEFAULT_WEIGHTS)
        except Exception:
            return dict(DEFAULT_WEIGHTS)
        finally:
            conn.close()