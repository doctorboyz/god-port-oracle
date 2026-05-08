"""Moving Average signal group — fires on MA crossovers and price-MA interactions."""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from broky.indicators.sma import calculate_sma, calculate_sma_10, calculate_sma_20, calculate_sma_50
from broky.indicators.ema import calculate_ema
from broky.indicators.dema import calculate_dema
from broky.indicators.tema import calculate_tema
from broky.indicators.ichimoku import calculate_ichimoku, price_vs_cloud

from shared.models import SignalGroup
from broky.signals.groups.base import GroupSignal


class MovingAverageGroup:
    """Moving Average signal group.

    Triggers on:
    - EMA 9/21 crossover (golden/death cross)
    - Price crosses SMA 50
    - Ichimoku Tenkan/Kijun cross
    - Price crosses above/below Ichimoku cloud
    - DEMA/TEMA directional signals
    """

    group = SignalGroup.MA

    def compute_indicators(
        self, candles: dict[str, pd.DataFrame]
    ) -> dict[str, float]:
        """Calculate all MA indicators."""
        df = candles.get("M5", candles.get("H1", candles.get("D1")))
        if df is None or df.empty:
            return {}

        close = df["close"]
        high = df["high"]
        low = df["low"]

        # SMAs
        sma_10 = calculate_sma_10(close)
        sma_20 = calculate_sma_20(close)
        sma_50 = calculate_sma_50(close)

        # EMAs
        ema_9 = calculate_ema(close, 9)
        ema_21 = calculate_ema(close, 21)
        ema_50 = calculate_ema(close, 50)
        ema_200 = calculate_ema(close, 200)

        # DEMA/TEMA
        dema_21 = calculate_dema(close, 21)
        tema_21 = calculate_tema(close, 21)

        # Ichimoku
        ichimoku = calculate_ichimoku(high, low, close)
        cloud_position = price_vs_cloud(close, ichimoku.senkou_a, ichimoku.senkou_b)

        def last(series: pd.Series) -> float:
            if isinstance(series, pd.Series) and len(series) > 0:
                val = series.iloc[-1]
                return float(val) if not pd.isna(val) else float("nan")
            return float("nan")

        def last_str(series: pd.Series) -> str:
            if isinstance(series, pd.Series) and len(series) > 0:
                val = series.iloc[-1]
                return str(val) if not pd.isna(val) else ""
            return ""

        return {
            "sma_10": last(sma_10),
            "sma_20": last(sma_20),
            "sma_50": last(sma_50),
            "ema_9": last(ema_9),
            "ema_21": last(ema_21),
            "ema_50": last(ema_50),
            "ema_200": last(ema_200),
            "dema_21": last(dema_21),
            "tema_21": last(tema_21),
            "ichimoku_tenkan": last(ichimoku.tenkan),
            "ichimoku_kijun": last(ichimoku.kijun),
            "ichimoku_senkou_a": last(ichimoku.senkou_a),
            "ichimoku_senkou_b": last(ichimoku.senkou_b),
            "ichimoku_chikou": last(ichimoku.chikou),
            "price_vs_cloud": last_str(cloud_position),
        }

    def check_trigger(
        self, indicator_values: dict[str, float]
    ) -> Optional[GroupSignal]:
        """Check MA trigger conditions."""
        triggers: list[str] = []
        direction_scores: list[float] = []

        ema_9 = indicator_values.get("ema_9", float("nan"))
        ema_21 = indicator_values.get("ema_21", float("nan"))
        price = indicator_values.get("price", float("nan"))
        sma_50 = indicator_values.get("sma_50", float("nan"))
        tenkan = indicator_values.get("ichimoku_tenkan", float("nan"))
        kijun = indicator_values.get("ichimoku_kijun", float("nan"))
        cloud_pos = indicator_values.get("price_vs_cloud", "")

        # EMA 9/21 crossover
        if not pd.isna(ema_9) and not pd.isna(ema_21):
            if ema_9 > ema_21:
                triggers.append("ema_bullish_cross")
                direction_scores.append(0.8)
            elif ema_9 < ema_21:
                triggers.append("ema_bearish_cross")
                direction_scores.append(-0.8)

        # Price vs SMA 50
        if not pd.isna(price) and not pd.isna(sma_50):
            if price > sma_50:
                triggers.append("price_above_sma50")
                direction_scores.append(0.6)
            elif price < sma_50:
                triggers.append("price_below_sma50")
                direction_scores.append(-0.6)

        # Ichimoku Tenkan/Kijun cross
        if not pd.isna(tenkan) and not pd.isna(kijun):
            if tenkan > kijun:
                triggers.append("ichimoku_bullish_cross")
                direction_scores.append(0.7)
            elif tenkan < kijun:
                triggers.append("ichimoku_bearish_cross")
                direction_scores.append(-0.7)

        # Price vs Ichimoku cloud
        if cloud_pos:
            if cloud_pos == "above":
                triggers.append("price_above_cloud")
                direction_scores.append(0.5)
            elif cloud_pos == "below":
                triggers.append("price_below_cloud")
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
            reason=f"MA triggers: {', '.join(triggers)}",
        )