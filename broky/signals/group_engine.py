"""Signal group engine — coordinates 4 independent indicator groups.

When ANY group triggers, captures a full feature snapshot across ALL groups
for ML training data collection.
"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from shared.models import SignalGroup
from broky.signals.groups.base import GroupSignal, compute_all_indicators
from broky.signals.groups.volume import VolumeGroup
from broky.signals.groups.ob_os import OverboughtOversoldGroup
from broky.signals.groups.ma_group import MovingAverageGroup
from broky.signals.groups.sentiment import SentimentGroup

logger = logging.getLogger(__name__)


class GroupCoordinator:
    """Coordinates 4 independent signal groups.

    On each evaluation cycle:
    1. Compute indicators for each group independently
    2. Check each group's trigger rules
    3. When ANY group triggers, compute a full feature snapshot
       across ALL groups for ML training data
    4. Return list of triggered signals with their feature snapshots
    """

    def __init__(self):
        self.volume = VolumeGroup()
        self.ob_os = OverboughtOversoldGroup()
        self.ma = MovingAverageGroup()
        self.sentiment = SentimentGroup()

        self._groups = {
            SignalGroup.VOLUME: self.volume,
            SignalGroup.OB_OS: self.ob_os,
            SignalGroup.MA: self.ma,
            SignalGroup.SENTIMENT: self.sentiment,
        }

    def evaluate(
        self, candles: dict[str, pd.DataFrame]
    ) -> list[tuple[GroupSignal, dict[str, float]]]:
        """Evaluate all groups and return triggered signals with full snapshots.

        Args:
            candles: Dict of timeframe -> DataFrame with OHLCV columns.
                     Must include at least M5 and optionally H1, H4, D1.

        Returns:
            List of (GroupSignal, full_feature_snapshot) tuples.
            Only includes groups that triggered.
        """
        triggered_signals: list[tuple[GroupSignal, dict[str, float]]] = []

        # Step 1: Compute each group's indicators independently
        group_indicators: dict[SignalGroup, dict[str, float]] = {}
        for group_name, group in self._groups.items():
            try:
                indicators = group.compute_indicators(candles)
                group_indicators[group_name] = indicators
            except Exception as e:
                logger.warning(f"Error computing indicators for {group_name.value}: {e}")
                group_indicators[group_name] = {}

        # Step 2: Check each group's trigger rules
        any_triggered = False
        for group_name, group in self._groups.items():
            try:
                signal = group.check_trigger(group_indicators[group_name])
                if signal is not None:
                    any_triggered = True
                    triggered_signals.append((signal, {}))  # Snapshot filled below
                    logger.info(
                        f"Signal triggered: {signal.group.value} {signal.direction} "
                        f"conf={signal.confidence:.2f} triggers={signal.triggering_indicators}"
                    )
            except Exception as e:
                logger.warning(f"Error checking trigger for {group_name.value}: {e}")

        # Step 3: If any group triggered, compute full feature snapshot
        if any_triggered:
            try:
                full_snapshot = compute_all_indicators(candles)
                # Add price from M5 data
                m5 = candles.get("M5", candles.get("H1", candles.get("D1")))
                if m5 is not None and not m5.empty:
                    full_snapshot["price"] = float(m5["close"].iloc[-1])

                # Attach full snapshot to each triggered signal
                updated_signals = []
                for signal, _ in triggered_signals:
                    updated_signals.append((signal, full_snapshot))
                return updated_signals

            except Exception as e:
                logger.error(f"Error computing full feature snapshot: {e}")
                # Return signals without snapshots rather than losing them
                return triggered_signals

        return triggered_signals

    def evaluate_single_group(
        self, candles: dict[str, pd.DataFrame], group: SignalGroup
    ) -> Optional[GroupSignal]:
        """Evaluate a single group (useful for testing or selective evaluation)."""
        group_obj = self._groups.get(group)
        if group_obj is None:
            return None

        try:
            indicators = group_obj.compute_indicators(candles)
            return group_obj.check_trigger(indicators)
        except Exception as e:
            logger.error(f"Error evaluating group {group.value}: {e}")
            return None