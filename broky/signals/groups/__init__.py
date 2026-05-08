"""Signal groups for ML data collection — independent indicator groups that fire separately."""

from broky.signals.groups.base import GroupSignal, SignalGroupProtocol, compute_all_indicators
from broky.signals.groups.volume import VolumeGroup
from broky.signals.groups.ob_os import OverboughtOversoldGroup
from broky.signals.groups.ma_group import MovingAverageGroup
from broky.signals.groups.sentiment import SentimentGroup

# GroupCoordinator lives in broky.signals.group_engine (not here) to avoid circular imports

__all__ = [
    "GroupSignal",
    "SignalGroupProtocol",
    "compute_all_indicators",
    "VolumeGroup",
    "OverboughtOversoldGroup",
    "MovingAverageGroup",
    "SentimentGroup",
]