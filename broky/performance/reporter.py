"""Daily reporter — formats learning reports for Telegram and psi vault.

Produces two outputs:
1. Telegram message — concise summary for the human
2. Psi vault file — full report stored in ψ/outbox/ for agent memory
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from broky.performance.analyzer import LearningReport
from broky.performance.adjuster import AdjustmentResult

logger = logging.getLogger(__name__)


def format_telegram_summary(
    report: LearningReport,
    adjustment: Optional[AdjustmentResult] = None,
) -> str:
    """Format a concise daily summary for Telegram.

    Keeps it under 4096 chars (Telegram limit) and scannable on mobile.
    """
    lines = [
        f"📊 <b>Daily Learning Report</b> — {report.date}",
        f"Mode: {report.mode} | Trades: {report.total_trades}",
        f"WR: {report.win_rate:.1%} | PF: {report.profit_factor:.2f} | "
        f"PnL: {report.total_pnl:+.2f} ({report.avg_pnl_pct:+.4f}%)",
    ]

    # Regime breakdown
    if report.regime_stats:
        lines.append("")
        lines.append("<b>By Regime:</b>")
        for regime, stats in sorted(report.regime_stats.items()):
            emoji = "🟢" if stats.win_rate >= 0.55 else ("🟡" if stats.win_rate >= 0.45 else "🔴")
            lines.append(
                f"  {emoji} {regime}: {stats.wins}/{stats.total_trades} "
                f"({stats.win_rate:.0%}) PF={stats.profit_factor:.2f}"
            )

    # Session breakdown
    if report.session_stats:
        lines.append("")
        lines.append("<b>By Session:</b>")
        for session, stats in sorted(report.session_stats.items()):
            emoji = "🟢" if stats.win_rate >= 0.55 else ("🟡" if stats.win_rate >= 0.45 else "🔴")
            lines.append(
                f"  {emoji} {session}: {stats.wins}/{stats.total_trades} "
                f"({stats.win_rate:.0%}) PF={stats.profit_factor:.2f}"
            )

    # Direction breakdown
    if report.direction_stats:
        lines.append("")
        lines.append("<b>By Direction:</b>")
        for direction, stats in sorted(report.direction_stats.items()):
            lines.append(
                f"  {direction}: {stats.wins}/{stats.total_trades} "
                f"({stats.win_rate:.0%}) PF={stats.profit_factor:.2f}"
            )

    # Loss cluster
    if report.loss_clusters and report.loss_clusters.max_consecutive_losses > 0:
        lines.append("")
        lc = report.loss_clusters
        lines.append(
            f"⚠️ Max consecutive losses: {lc.max_consecutive_losses}"
        )
        if lc.most_detrimental_regime:
            lines.append(f"  Worst regime: {lc.most_detrimental_regime}")
        if lc.most_detrimental_session:
            lines.append(f"  Worst session: {lc.most_detrimental_session}")

    # Weight adjustments
    if adjustment and not adjustment.skipped:
        lines.append("")
        lines.append("<b>Weight Adjustments:</b>")
        for adj in adjustment.adjustments:
            if adj.delta != 0:
                arrow = "⬆️" if adj.delta > 0 else "⬇️"
                lines.append(
                    f"  {arrow} {adj.indicator}: {adj.old_weight:.2f} → {adj.new_weight:.2f} "
                    f"({adj.delta:+.3f})"
                )
    elif adjustment and adjustment.skipped:
        lines.append("")
        lines.append(f"⏸️ Weight adjust skipped: {adjustment.skip_reason}")

    return "\n".join(lines)


def format_vault_report(
    report: LearningReport,
    adjustment: Optional[AdjustmentResult] = None,
    loss_suggestions: Optional[dict[str, str]] = None,
) -> str:
    """Format a full report for psi vault storage.

    This is the detailed version kept for agent memory — not limited by
    Telegram message size.
    """
    now = datetime.now(timezone.utc)
    lines = [
        f"# Daily Learning Report — {report.date}",
        f"",
        f"**Mode**: {report.mode}",
        f"**Timestamp**: {now.isoformat()}",
        f"",
        f"## Summary",
        f"- Total trades: {report.total_trades}",
        f"- Wins: {report.wins} | Losses: {report.losses}",
        f"- Win rate: {report.win_rate:.1%}",
        f"- Profit factor: {report.profit_factor:.2f}",
        f"- Total PnL: {report.total_pnl:+.2f}",
        f"- Avg PnL%: {report.avg_pnl_pct:+.4f}%",
    ]

    # Regime breakdown
    if report.regime_stats:
        lines.append("")
        lines.append("## Regime Performance")
        for regime, stats in sorted(report.regime_stats.items()):
            lines.append(
                f"- **{regime}**: {stats.wins}/{stats.total_trades} "
                f"WR={stats.win_rate:.1%} PF={stats.profit_factor:.2f} "
                f"avgPnL={stats.avg_pnl_pct:+.4f}%"
            )

    # Session breakdown
    if report.session_stats:
        lines.append("")
        lines.append("## Session Performance")
        for session, stats in sorted(report.session_stats.items()):
            lines.append(
                f"- **{session}**: {stats.wins}/{stats.total_trades} "
                f"WR={stats.win_rate:.1%} PF={stats.profit_factor:.2f} "
                f"avgPnL={stats.avg_pnl_pct:+.4f}%"
            )

    # Direction breakdown
    if report.direction_stats:
        lines.append("")
        lines.append("## Direction Performance")
        for direction, stats in sorted(report.direction_stats.items()):
            lines.append(
                f"- **{direction}**: {stats.wins}/{stats.total_trades} "
                f"WR={stats.win_rate:.1%} PF={stats.profit_factor:.2f}"
            )

    # Indicator effectiveness
    if report.indicator_effectiveness:
        lines.append("")
        lines.append("## Indicator Effectiveness")
        for name, stats in sorted(report.indicator_effectiveness.items()):
            lines.append(
                f"- **{name}**: avg_win={stats.avg_in_wins:+.3f} "
                f"avg_loss={stats.avg_in_losses:+.3f} "
                f"r={stats.correlation_with_pnl:.3f} "
                f"→ {stats.suggestion}"
            )

    # Loss clusters
    if report.loss_clusters and report.loss_clusters.max_consecutive_losses > 0:
        lc = report.loss_clusters
        lines.append("")
        lines.append("## Loss Clusters")
        lines.append(f"- Max consecutive losses: {lc.max_consecutive_losses}")
        if lc.most_detrimental_regime:
            lines.append(f"- Worst regime: {lc.most_detrimental_regime}")
        if lc.most_detrimental_session:
            lines.append(f"- Worst session: {lc.most_detrimental_session}")
        for pattern in lc.cluster_patterns:
            lines.append(
                f"- Cluster({pattern['length']}): "
                f"regimes={pattern.get('regimes', [])} "
                f"sessions={pattern.get('sessions', [])}"
            )

    # Weight adjustments
    if adjustment:
        lines.append("")
        if adjustment.skipped:
            lines.append(f"## Weight Adjustment — SKIPPED")
            lines.append(f"Reason: {adjustment.skip_reason}")
        else:
            lines.append(f"## Weight Adjustments")
            lines.append(f"Confidence floor: {adjustment.confidence_floor}")
            for adj in adjustment.adjustments:
                lines.append(
                    f"- {adj.indicator}: {adj.old_weight:.4f} → {adj.new_weight:.4f} "
                    f"({adj.delta:+.4f}) [{adj.reason}]"
                )
            lines.append("")
            lines.append("### New Weights")
            for name, weight in sorted(adjustment.new_weights.items()):
                lines.append(f"- {name}: {weight:.4f}")

    # Loss cluster suggestions
    if loss_suggestions:
        lines.append("")
        lines.append("## Qualitative Suggestions")
        for key, desc in loss_suggestions.items():
            lines.append(f"- **{key}**: {desc}")

    # Parameter suggestions from analyzer
    if report.parameter_suggestions:
        lines.append("")
        lines.append("## Parameter Suggestions")
        for key, value in report.parameter_suggestions.items():
            lines.append(f"- {key}: {value}")

    lines.append("")
    lines.append("---")
    lines.append(f"*Generated by DailyReporter at {now.strftime('%Y-%m-%d %H:%M UTC')}*")

    return "\n".join(lines)


def save_vault_report(content: str, date: str, psi_root: Optional[Path] = None) -> Path:
    """Save the vault report to ψ/outbox/.

    Args:
        content: Full report text.
        date: Date string (YYYY-MM-DD).
        psi_root: Path to ψ directory.

    Returns:
        Path to the saved file.
    """
    if psi_root is None:
        psi_root = Path(__file__).parent.parent.parent / "ψ"

    outbox = psi_root / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)

    filepath = outbox / f"learning_report_{date}.md"
    filepath.write_text(content, encoding="utf-8")
    logger.info("Saved learning report to %s", filepath)
    return filepath