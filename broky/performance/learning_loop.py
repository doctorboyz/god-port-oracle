"""Daily learning loop — orchestrates analysis, adjustment, and reporting.

Runs once per day (at 00:05 UTC) after the daily summary:
1. Analyze yesterday's trades (DailyAnalyzer)
2. Adjust indicator weights (ParameterAdjuster)
3. Generate loss cluster suggestions
4. Format and send Telegram notification
5. Save full report to psi vault
6. Write updated weights to JSON config file

The loop is designed to be safe: if there aren't enough trades, or if
the win rate is catastrophically low, it skips adjustment and reports why.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from broky.performance.analyzer import DailyAnalyzer
from broky.performance.adjuster import ParameterAdjuster, DEFAULT_WEIGHTS
from broky.performance.reporter import (
    format_telegram_summary,
    format_vault_report,
    save_vault_report,
)

logger = logging.getLogger(__name__)

# Default paths
DEFAULT_DB_PATH = Path(__file__).parent.parent.parent / "data" / "oracle.db"
DEFAULT_WEIGHTS_FILE = Path(__file__).parent.parent / "config" / "indicator_weights.json"
DEFAULT_PSI_ROOT = Path(__file__).parent.parent.parent / "ψ"


def load_current_weights(weights_file: Path = DEFAULT_WEIGHTS_FILE) -> dict[str, float]:
    """Load indicator weights from JSON config file.

    Falls back to DEFAULT_WEIGHTS if file doesn't exist.
    """
    if weights_file.exists():
        try:
            data = json.loads(weights_file.read_text(encoding="utf-8"))
            if "weights" in data:
                return data["weights"]
            return data
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("Failed to load weights from %s: %s, using defaults", weights_file, e)
    return dict(DEFAULT_WEIGHTS)


def save_weights_to_file(weights: dict[str, float], weights_file: Path = DEFAULT_WEIGHTS_FILE) -> None:
    """Save indicator weights to JSON config file.

    The file format includes metadata for traceability.
    """
    weights_file.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "weights": weights,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source": "daily_learning_loop",
    }
    weights_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    logger.info("Saved weights to %s", weights_file)


class DailyLearningLoop:
    """Orchestrates the daily learning cycle.

    Usage:
        loop = DailyLearningLoop(db_path=Path("data/oracle.db"))
        result = loop.run(date="2026-05-05")
        # Or for yesterday (default):
        result = loop.run()
    """

    def __init__(
        self,
        db_path: Path = DEFAULT_DB_PATH,
        weights_file: Path = DEFAULT_WEIGHTS_FILE,
        psi_root: Path = DEFAULT_PSI_ROOT,
        notifier=None,
    ):
        self.db_path = db_path
        self.weights_file = weights_file
        self.psi_root = psi_root
        self.notifier = notifier  # TelegramNotifier instance (optional)

        # Load current weights
        current_weights = load_current_weights(weights_file)

        # Initialize components
        self.analyzer = DailyAnalyzer(db_path=db_path)
        self.adjuster = ParameterAdjuster(
            current_weights=current_weights,
            db_path=db_path,
        )

    def run(
        self,
        date: Optional[str] = None,
        mode: Optional[str] = None,
    ) -> dict:
        """Run the full daily learning cycle.

        Args:
            date: Date string YYYY-MM-DD (default: yesterday UTC).
            mode: Filter by trading_mode "swing"/"scalp" (default: all).

        Returns:
            Dict with report, adjustment, and suggestions.
        """
        if date is None:
            from datetime import timedelta
            yesterday = datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            ) - timedelta(days=1)
            date = yesterday.strftime("%Y-%m-%d")

        logger.info("Starting daily learning loop for %s (mode=%s)", date, mode or "all")

        # Step 1: Analyze trades
        report = self.analyzer.analyze(date=date, mode=mode)
        logger.info(
            "Analysis complete: %d trades, WR=%.1f%%, PF=%.2f, PnL=%+.2f",
            report.total_trades,
            report.win_rate * 100,
            report.profit_factor,
            report.total_pnl,
        )

        # Step 2: Adjust weights
        adjustment = self.adjuster.adjust(report)
        if adjustment.skipped:
            logger.info("Weight adjustment skipped: %s", adjustment.skip_reason)
        else:
            logger.info("Applied %d weight adjustments", len(adjustment.adjustments))
            for adj in adjustment.adjustments:
                if adj.delta != 0:
                    logger.info(
                        "  %s: %.4f → %.4f (%+.4f) [%s]",
                        adj.indicator, adj.old_weight, adj.new_weight, adj.delta, adj.reason,
                    )

        # Step 3: Generate loss cluster suggestions
        loss_suggestions = self.adjuster.adjust_from_loss_clusters(report)
        if loss_suggestions:
            logger.info("Loss cluster suggestions: %s", list(loss_suggestions.keys()))

        # Step 4: Save weights if adjusted
        if not adjustment.skipped and adjustment.new_weights:
            save_weights_to_file(adjustment.new_weights, self.weights_file)

        # Step 5: Send Telegram notification
        if self.notifier:
            try:
                telegram_msg = format_telegram_summary(report, adjustment)
                self.notifier.send(telegram_msg)
                logger.info("Sent learning report via Telegram")
            except Exception as e:
                logger.error("Failed to send Telegram notification: %s", e)

        # Step 6: Save vault report
        try:
            vault_content = format_vault_report(report, adjustment, loss_suggestions)
            vault_path = save_vault_report(vault_content, date, self.psi_root)
            logger.info("Saved vault report to %s", vault_path)
        except Exception as e:
            logger.error("Failed to save vault report: %s", e)

        return {
            "report": report,
            "adjustment": adjustment,
            "loss_suggestions": loss_suggestions,
            "weights_updated": not adjustment.skipped,
            "date": date,
            "mode": mode or "all",
        }


def run_daily_learning(
    db_path: Optional[Path] = None,
    weights_file: Optional[Path] = None,
    psi_root: Optional[Path] = None,
    notifier=None,
    date: Optional[str] = None,
    mode: Optional[str] = None,
) -> dict:
    """Convenience function to run the daily learning loop.

    Can be called from oracle_runner or directly.
    """
    _db = db_path or DEFAULT_DB_PATH
    _wf = weights_file or DEFAULT_WEIGHTS_FILE
    _psi = psi_root or DEFAULT_PSI_ROOT

    loop = DailyLearningLoop(
        db_path=_db,
        weights_file=_wf,
        psi_root=_psi,
        notifier=notifier,
    )
    return loop.run(date=date, mode=mode)