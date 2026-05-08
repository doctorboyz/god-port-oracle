"""Overbought/Oversold signal group — fires on OB/OS threshold crossings."""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from broky.indicators.rsi import calculate_rsi
from broky.indicators.stochastic import calculate_stochastic
from broky.indicators.williams_r import calculate_williams_r
from broky.indicators.cci import calculate_cci
from broky.indicators.demarker import calculate_demarker
from broky.indicators.roc import calculate_roc

from shared.models import SignalGroup
from broky.signals.groups.base import GroupSignal


class OverboughtOversoldGroup:
    """Overbought/Oversold signal group.

    Triggers on:
    - RSI crosses 30 (oversold) or 70 (overbought)
    - Stochastic %K crosses 20 or 80
    - Williams %R crosses -20 or -80
    - CCI absolute value > 100
    - DeMarker > 0.7 or < 0.3
    """

    group = SignalGroup.OB_OS

    def compute_indicators(
        self, candles: dict[str, pd.DataFrame]
    ) -> dict[str, float]:
        """Calculate all OB/OS indicators."""
        df = candles.get("M5", candles.get("H1", candles.get("D1")))
        if df is None or df.empty:
            return {}

        close = df["close"]
        high = df["high"]
        low = df["low"]

        rsi = calculate_rsi(close)
        stoch = calculate_stochastic(high, low, close)
        williams = calculate_williams_r(high, low, close)
        cci = calculate_cci(high, low, close)
        demarker = calculate_demarker(high, low)
        roc = calculate_roc(close)

        def last(series: pd.Series) -> float:
            if isinstance(series, pd.Series) and len(series) > 0:
                val = series.iloc[-1]
                return float(val) if not pd.isna(val) else float("nan")
            return float("nan")

        stoch_k = last(stoch.k_line) if hasattr(stoch, "k_line") else float("nan")
        stoch_d = last(stoch.d_line) if hasattr(stoch, "d_line") else float("nan")

        return {
            "rsi": last(rsi),
            "stoch_k": stoch_k,
            "stoch_d": stoch_d,
            "williams_r": last(williams),
            "cci": last(cci),
            "demarker": last(demarker),
            "roc": last(roc),
        }

    def check_trigger(
        self, indicator_values: dict[str, float]
    ) -> Optional[GroupSignal]:
        """Check OB/OS trigger conditions."""
        triggers: list[str] = []
        direction_scores: list[float] = []

        rsi = indicator_values.get("rsi", float("nan"))
        stoch_k = indicator_values.get("stoch_k", float("nan"))
        williams = indicator_values.get("williams_r", float("nan"))
        cci = indicator_values.get("cci", float("nan"))
        dem = indicator_values.get("demarker", float("nan"))

        # RSI oversold/overbought
        if not pd.isna(rsi):
            if rsi < 30:
                triggers.append("rsi_oversold")
                direction_scores.append(1.0)
            elif rsi > 70:
                triggers.append("rsi_overbought")
                direction_scores.append(-1.0)

        # Stochastic
        if not pd.isna(stoch_k):
            if stoch_k < 20:
                triggers.append("stoch_oversold")
                direction_scores.append(0.8)
            elif stoch_k > 80:
                triggers.append("stoch_overbought")
                direction_scores.append(-0.8)

        # Williams %R (-100 to 0)
        if not pd.isna(williams):
            if williams < -80:
                triggers.append("williams_oversold")
                direction_scores.append(0.7)
            elif williams > -20:
                triggers.append("williams_overbought")
                direction_scores.append(-0.7)

        # CCI
        if not pd.isna(cci):
            if cci < -100:
                triggers.append("cci_oversold")
                direction_scores.append(0.6)
            elif cci > 100:
                triggers.append("cci_overbought")
                direction_scores.append(-0.6)

        # DeMarker
        if not pd.isna(dem):
            if dem < 0.3:
                triggers.append("demarker_oversold")
                direction_scores.append(0.5)
            elif dem > 0.7:
                triggers.append("demarker_overbought")
                direction_scores.append(-0.5)

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
            reason=f"OB/OS triggers: {', '.join(triggers)}",
        )