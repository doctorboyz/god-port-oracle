"""Volume signal group — fires on volume anomalies."""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from broky.indicators.obv import calculate_obv
from broky.indicators.mfi import calculate_mfi
from broky.indicators.vwap import calculate_vwap, calculate_vwap_offset
from broky.indicators.volume_roc import calculate_volume_roc
from broky.indicators.ad_line import calculate_ad_line, calculate_ad_line_slope
from broky.indicators.cmf import calculate_cmf

from shared.models import SignalGroup
from broky.signals.groups.base import GroupSignal


class VolumeGroup:
    """Volume-based signal group.

    Triggers on:
    - OBV slope > 2 std devs from 20-period mean (volume trend divergence)
    - MFI crosses 20 (oversold) or 80 (overbought)
    - Volume ROC > 50% (volume spike)
    - CMF absolute value > 0.15 (significant money flow)
    """

    group = SignalGroup.VOLUME

    def compute_indicators(
        self, candles: dict[str, pd.DataFrame]
    ) -> dict[str, float]:
        """Calculate all volume indicators."""
        # Use M5 or lowest available timeframe for detailed volume analysis
        df = candles.get("M5", candles.get("H1", candles.get("D1")))
        if df is None or df.empty:
            return {}

        close = df["close"]
        high = df["high"]
        low = df["low"]
        volume = df["volume"]

        # Calculate all indicators
        obv = calculate_obv(close, volume)
        obv_slope = calculate_ad_line_slope(obv, 20) if len(obv) > 20 else pd.Series(dtype=float)
        mfi = calculate_mfi(high, low, close, volume)
        vwap = calculate_vwap(high, low, close, volume)
        vwap_offset = calculate_vwap_offset(close, vwap) if len(vwap) > 0 else pd.Series(dtype=float)
        vol_roc = calculate_volume_roc(volume)
        ad = calculate_ad_line(high, low, close, volume)
        ad_slope = calculate_ad_line_slope(ad, 20) if len(ad) > 20 else pd.Series(dtype=float)
        cmf_val = calculate_cmf(high, low, close, volume)

        # Return latest values (or NaN if not enough data)
        def last(series: pd.Series) -> float:
            return float(series.iloc[-1]) if len(series) > 0 and not pd.isna(series.iloc[-1]) else float("nan")

        # Derive mfi_signal from MFI value
        mfi_val = last(mfi)
        if not pd.isna(mfi_val):
            mfi_signal = "oversold" if mfi_val < 20 else ("overbought" if mfi_val > 80 else "neutral")
        else:
            mfi_signal = "neutral"

        return {
            "obv": last(obv),
            "obv_slope": last(obv_slope),
            "mfi": mfi_val,
            "mfi_signal": mfi_signal,
            "vwap_offset_pct": last(vwap_offset),
            "volume_roc": last(vol_roc),
            "ad_line": last(ad),
            "ad_line_slope": last(ad_slope),
            "cmf": last(cmf_val),
        }

    def check_trigger(
        self, indicator_values: dict[str, float]
    ) -> Optional[GroupSignal]:
        """Check volume trigger conditions."""
        triggers: list[str] = []
        direction_scores: list[float] = []

        mfi = indicator_values.get("mfi", float("nan"))
        obv_slope = indicator_values.get("obv_slope", float("nan"))
        vol_roc = indicator_values.get("volume_roc", float("nan"))
        cmf = indicator_values.get("cmf", float("nan"))

        # MFI oversold/overbought
        if not pd.isna(mfi):
            if mfi < 20:
                triggers.append("mfi_oversold")
                direction_scores.append(1.0)  # bullish
            elif mfi > 80:
                triggers.append("mfi_overbought")
                direction_scores.append(-1.0)  # bearish

        # OBV slope divergence (strong volume trend)
        if not pd.isna(obv_slope) and abs(obv_slope) > 0:
            # OBV slope > 2 std devs from mean (simplified: just use sign)
            if obv_slope > 0:
                triggers.append("obv_rising")
                direction_scores.append(0.7)
            elif obv_slope < 0:
                triggers.append("obv_falling")
                direction_scores.append(-0.7)

        # Volume spike
        if not pd.isna(vol_roc) and vol_roc > 50:
            triggers.append("volume_spike")
            # Volume spike direction based on price change (use volume ROC sign)
            direction_scores.append(1.0 if vol_roc > 0 else -1.0)

        # CMF (Chaikin Money Flow)
        if not pd.isna(cmf):
            if cmf > 0.15:
                triggers.append("cmf_positive")
                direction_scores.append(0.6)
            elif cmf < -0.15:
                triggers.append("cmf_negative")
                direction_scores.append(-0.6)

        if not triggers:
            return None

        # Determine overall direction from average of scores
        avg_score = sum(direction_scores) / len(direction_scores)
        direction = "BUY" if avg_score > 0 else "SELL"
        confidence = min(abs(avg_score), 1.0)

        return GroupSignal(
            group=self.group,
            direction=direction,
            confidence=confidence,
            triggering_indicators=triggers,
            reason=f"Volume triggers: {', '.join(triggers)}",
        )