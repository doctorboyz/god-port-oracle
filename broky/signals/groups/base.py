"""Signal group protocol and base classes for independent group triggers."""

from __future__ import annotations

from typing import Optional, Protocol

import pandas as pd

from shared.models import SignalGroup


class GroupSignal:
    """Signal output from a single indicator group."""

    __slots__ = ("group", "direction", "confidence", "triggering_indicators", "reason")

    def __init__(
        self,
        group: SignalGroup,
        direction: str,
        confidence: float,
        triggering_indicators: list[str] | None = None,
        reason: str = "",
    ):
        self.group = group
        self.direction = direction  # "BUY" or "SELL"
        self.confidence = confidence
        self.triggering_indicators = triggering_indicators or []
        self.reason = reason

    def __repr__(self) -> str:
        return (
            f"GroupSignal(group={self.group.value}, dir={self.direction}, "
            f"conf={self.confidence:.2f}, triggers={self.triggering_indicators})"
        )


class SignalGroupProtocol(Protocol):
    """Protocol for signal groups. Each group independently evaluates its indicators
    and decides whether to fire a signal.

    The goal is DATA COLLECTION, not profitable trading. Triggers should fire
    often enough to capture diverse market conditions for ML training.
    """

    group: SignalGroup

    def compute_indicators(
        self, candles: dict[str, pd.DataFrame]
    ) -> dict[str, float]:
        """Calculate all indicators for this group.

        Args:
            candles: Dict of timeframe -> DataFrame with OHLCV columns.

        Returns:
            Dict of indicator name -> current value.
        """
        ...

    def check_trigger(
        self, indicator_values: dict[str, float]
    ) -> Optional[GroupSignal]:
        """Check if any indicator in this group exceeds the trigger threshold.

        Returns None if no signal, or a GroupSignal with direction and triggering indicators.
        """
        ...


def compute_all_indicators(
    candles: dict[str, pd.DataFrame],
) -> dict[str, float]:
    """Compute indicators from ALL groups at once for a full feature snapshot.

    This is called when ANY group triggers, to capture the complete market state
    across all indicator groups for ML training.
    """
    from broky.signals.groups.volume import VolumeGroup
    from broky.signals.groups.ob_os import OverboughtOversoldGroup
    from broky.signals.groups.ma_group import MovingAverageGroup
    from broky.signals.groups.sentiment import SentimentGroup

    volume_group = VolumeGroup()
    obos_group = OverboughtOversoldGroup()
    ma_group = MovingAverageGroup()
    sentiment_group = SentimentGroup()

    all_indicators: dict[str, float] = {}
    all_indicators.update(volume_group.compute_indicators(candles))
    all_indicators.update(obos_group.compute_indicators(candles))
    all_indicators.update(ma_group.compute_indicators(candles))
    all_indicators.update(sentiment_group.compute_indicators(candles))

    return all_indicators