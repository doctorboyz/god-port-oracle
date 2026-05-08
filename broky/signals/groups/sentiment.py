"""Sentiment signal group — fires on market sentiment anomalies."""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from shared.models import SignalGroup
from broky.signals.groups.base import GroupSignal


class SentimentGroup:
    """Sentiment-based signal group.

    Uses market microstructure indicators as sentiment proxies:
    - Tick volume ratio (> 3x average = spike)
    - Spread ratio (> 2x normal = stress)
    - Session pattern (overlap = higher activity)

    Note: Long/Short ratio from broker data requires MT5 terminal access
    and may not be available on all brokers. Falls back to tick volume analysis.
    """

    group = SignalGroup.SENTIMENT

    def compute_indicators(
        self, candles: dict[str, pd.DataFrame]
    ) -> dict[str, float]:
        """Calculate sentiment indicators from tick volume and spread."""
        # Use M5 for short-term sentiment, H1 for session context
        m5 = candles.get("M5")
        if m5 is None or m5.empty or len(m5) < 20:
            return {}

        volume = m5["volume"]
        close = m5["close"]

        # Tick volume ratio: current volume / 20-period average
        vol_ma_20 = volume.rolling(20).mean()
        tick_volume_ratio = volume.iloc[-1] / vol_ma_20.iloc[-1] if vol_ma_20.iloc[-1] > 0 else float("nan")

        # Spread approximation: (high - low) as proxy for spread width
        # In live mode, we'd get actual spread from MT5
        spread_approx = (m5["high"] - m5["low"]).rolling(20).mean()
        current_spread = m5["high"].iloc[-1] - m5["low"].iloc[-1]
        spread_ratio = current_spread / spread_approx.iloc[-1] if spread_approx.iloc[-1] > 0 else float("nan")

        # Session strength based on time of day
        timestamp = m5.index[-1] if isinstance(m5.index[-1], pd.Timestamp) else pd.Timestamp.now()
        hour = timestamp.hour if hasattr(timestamp, "hour") else 0
        session_strength = self._session_strength(hour)

        # Long/Short ratio placeholder (requires broker data, not available in backtest)
        long_short_ratio = float("nan")

        def safe_float(val) -> float:
            return float(val) if not pd.isna(val) else float("nan")

        return {
            "tick_volume_ratio": safe_float(tick_volume_ratio),
            "spread_ratio": safe_float(spread_ratio),
            "long_short_ratio": long_short_ratio,
            "session_strength": session_strength,
        }

    def _session_strength(self, hour: int) -> float:
        """Calculate session strength score based on UTC hour.

        London/NY overlap (13-16 UTC) = 1.0
        London (8-16 UTC) = 0.7
        NY (13-22 UTC) = 0.7
        Asian (0-8 UTC) = 0.4
        Off-hours = 0.2
        """
        if 13 <= hour <= 16:
            return 1.0  # London/NY overlap
        elif 8 <= hour <= 16:
            return 0.7  # London
        elif 13 <= hour <= 22:
            return 0.7  # NY
        elif 0 <= hour <= 8:
            return 0.4  # Asian
        else:
            return 0.2  # Off-hours

    def check_trigger(
        self, indicator_values: dict[str, float]
    ) -> Optional[GroupSignal]:
        """Check sentiment trigger conditions."""
        triggers: list[str] = []
        direction_scores: list[float] = []

        tick_vol = indicator_values.get("tick_volume_ratio", float("nan"))
        spread = indicator_values.get("spread_ratio", float("nan"))
        session = indicator_values.get("session_strength", 0.0)

        # Tick volume spike (> 3x average)
        if not pd.isna(tick_vol) and tick_vol > 3.0:
            triggers.append("volume_spike")
            # Direction based on price change (simplified: use volume ratio sign)
            direction_scores.append(1.0 if tick_vol > 0 else -1.0)

        # Spread widening (> 2x normal = market stress)
        if not pd.isna(spread) and spread > 2.0:
            triggers.append("spread_widening")
            direction_scores.append(-0.5)  # Wide spread often = uncertainty

        # Session overlap with unusual activity
        if session >= 1.0 and not pd.isna(tick_vol) and tick_vol > 2.0:
            triggers.append("overlap_activity")
            direction_scores.append(0.6)

        if not triggers:
            return None

        avg_score = sum(direction_scores) / len(direction_scores)
        direction = "BUY" if avg_score > 0 else "SELL"
        confidence = min(abs(avg_score), 1.0)

        return GroupSignal(
            group=self.group,
            direction=direction,
            confidence=confidence,
            triggering_indicators=triggers,
            reason=f"Sentiment triggers: {', '.join(triggers)}",
        )