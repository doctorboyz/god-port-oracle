"""M1 Scalp signal generator — optimized for 1-minute bars.

Key differences from M5 swing generator:
- No D1 counter-trend filter (scalps are too short for D1 alignment)
- Session gate: London + Overlap + NY (all liquid sessions, no Asian)
- Spread filter: skip if spread > threshold
- Lower ADX threshold (10 vs 20) — M1 trends form faster, ADX(7) is noisy
- Lower min confidence (0.45 vs 0.60) — noisier timeframe, need more trades
- Lower direction threshold (0.15 vs 0.30) — scalp needs higher frequency
- Faster indicator periods (EMA 5/13, MACD 6/13/5, etc.)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from broky.indicators.ema import calculate_ema, calculate_ema_cross
from broky.indicators.macd import calculate_macd
from broky.indicators.bollinger import calculate_bollinger
from broky.indicators.atr import calculate_atr
from broky.indicators.adx import calculate_adx
from broky.indicators.volume import calculate_volume_ratio
from broky.risk.spread_filter import check_spread
from shared.models import Signal, SignalType, TradingMode
from broky.signals.registry import strategy

# M1 Scalp indicator weights
# Momentum-heavy: MACD is king on M1, EMA cross for quick direction
SCALP_WEIGHTS = {
    "ema_cross": 0.20,
    "ema_trend": 0.05,
    "adx": 0.15,
    "macd": 0.30,
    "bollinger": 0.10,
    "volume": 0.20,
}

# M1 indicator periods (faster than M5)
SCALP_PERIODS = {
    "ema_fast": 5,
    "ema_slow": 13,
    "ema_trend_fast": 21,
    "ema_trend_slow": 50,
    "adx": 7,
    "macd_fast": 6,
    "macd_slow": 13,
    "macd_signal": 5,
    "bollinger": 10,
    "bollinger_std": 1.5,
    "atr": 7,
    "volume_ma": 10,
}

# Scalp thresholds
SCALP_MIN_CONFIDENCE = 0.45
SCALP_ADX_THRESHOLD = 10
SCALP_SPREAD_MAX = 35  # points — slightly wider for M1
SCALP_DIRECTION_THRESHOLD = 0.15

# Session gate: trade during all liquid sessions (London, Overlap, NY)
# Asian session (22:00-08:00 UTC) excluded — low volume, wider spreads
SCALP_SESSIONS = {"london", "overlap", "ny"}

# Session classification (UTC hours)
def _classify_session_utc(hour: int) -> str:
    if 13 <= hour < 16:
        return "overlap"
    if 8 <= hour < 16:
        return "london"
    if 13 <= hour < 22:
        return "ny"
    return "asian"


@strategy(
    name="m1_scalp",
    timeframe="M1",
    trading_mode=TradingMode.SCALP,
    description="M1 scalping with session gate and spread filter",
    risk_defaults={"risk_per_trade": 0.015, "atr_multiplier": 1.0, "risk_reward_ratio": 1.5, "min_confidence": 0.45},
    requires_spread=True,
    min_bars=50,
)
def generate_scalp_signal(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    volume: pd.Series,
    current_price: Optional[float] = None,
    timestamp: Optional[datetime] = None,
    spread: Optional[float] = None,
    min_confidence: float = SCALP_MIN_CONFIDENCE,
    max_spread: float = SCALP_SPREAD_MAX,
) -> Signal:
    """Generate an M1 scalp signal.

    Args:
        close: M1 close price series.
        high: M1 high price series.
        low: M1 low price series.
        volume: M1 volume series.
        current_price: Current price (defaults to latest close).
        timestamp: Candle timestamp (defaults to now UTC).
        spread: Current spread in points (skip if > max_spread).
        min_confidence: Minimum confidence threshold.
        max_spread: Maximum spread in points.

    Returns:
        Signal with type, confidence, and reason.
    """
    if current_price is None:
        current_price = float(close.iloc[-1])

    if timestamp is None:
        timestamp = datetime.now(timezone.utc)

    # Session gate — trade during liquid sessions only (no Asian)
    session = _classify_session_utc(timestamp.hour)
    if session not in SCALP_SESSIONS:
        return Signal(
            symbol="XAUUSD",
            signal_type=SignalType.HOLD,
            confidence=0.0,
            price=current_price,
            timestamp=timestamp,
            timeframe="M1",
            reason=f"Scalp blocked: {session} session (liquid sessions only)",
            regime="unknown",
            trading_mode=TradingMode.SCALP,
        )

    # Spread filter — skip if spread too wide
    if spread is not None and not check_spread(spread, max_spread):
        return Signal(
            symbol="XAUUSD",
            signal_type=SignalType.HOLD,
            confidence=0.0,
            price=current_price,
            timestamp=timestamp,
            timeframe="M1",
            reason=f"Scalp blocked: spread={spread:.0f} > max={max_spread:.0f}",
            regime="unknown",
            trading_mode=TradingMode.SCALP,
        )

    # Calculate indicators with M1 periods
    p = SCALP_PERIODS
    scores = {}

    # ADX — faster period, lower threshold
    adx, plus_di, minus_di = calculate_adx(high, low, close, period=p["adx"])
    latest_adx = adx.iloc[-1]
    latest_pdi = plus_di.iloc[-1]
    latest_mdi = minus_di.iloc[-1]

    if pd.notna(latest_adx) and pd.notna(latest_pdi) and pd.notna(latest_mdi):
        if latest_adx >= SCALP_ADX_THRESHOLD:
            if latest_adx >= 40:
                scores["adx"] = 1.0 if latest_pdi > latest_mdi else -1.0
            elif latest_adx >= 25:
                scores["adx"] = 0.5 if latest_pdi > latest_mdi else -0.5
            else:
                scores["adx"] = 0.3 if latest_pdi > latest_mdi else -0.3
        else:
            scores["adx"] = 0.0

    # MACD — shorter periods for M1
    macd_result = calculate_macd(
        close,
        fast_period=p["macd_fast"],
        slow_period=p["macd_slow"],
        signal_period=p["macd_signal"],
    )
    hist = macd_result.histogram.iloc[-1]
    if pd.notna(hist):
        scores["macd"] = 1.0 if hist > 0 else -1.0

    # EMA Cross — fast periods (5/13)
    _, _, crossover = calculate_ema_cross(close, fast_period=p["ema_fast"], slow_period=p["ema_slow"])
    latest_cross = crossover.iloc[-1]
    if pd.notna(latest_cross) and latest_cross != 0:
        scores["ema_cross"] = float(latest_cross)
    else:
        fast_val = calculate_ema(close, p["ema_fast"]).iloc[-1]
        slow_val = calculate_ema(close, p["ema_slow"]).iloc[-1]
        if pd.notna(fast_val) and pd.notna(slow_val):
            scores["ema_cross"] = 0.5 if fast_val > slow_val else -0.5

    # EMA Trend — shorter lookback (21/50)
    ema_trend_fast = calculate_ema(close, p["ema_trend_fast"]).iloc[-1]
    ema_trend_slow = calculate_ema(close, p["ema_trend_slow"]).iloc[-1]
    if pd.notna(ema_trend_fast) and pd.notna(ema_trend_slow):
        scores["ema_trend"] = 0.5 if ema_trend_fast > ema_trend_slow else -0.5

    # Bollinger Bands — tighter (10/1.5)
    boll = calculate_bollinger(close, period=p["bollinger"], std_dev=p["bollinger_std"])
    latest_close = close.iloc[-1]
    latest_lower = boll.lower.iloc[-1]
    latest_upper = boll.upper.iloc[-1]
    latest_middle = boll.middle.iloc[-1]
    if pd.notna(latest_close) and pd.notna(latest_lower) and pd.notna(latest_upper):
        if latest_close < latest_lower:
            scores["bollinger"] = 1.0
        elif latest_close > latest_upper:
            scores["bollinger"] = -1.0
        else:
            scores["bollinger"] = 0.0

    # Volume — shorter MA
    vol_ratio = calculate_volume_ratio(volume, period=p["volume_ma"])
    latest_vol_ratio = vol_ratio.iloc[-1]
    if pd.notna(latest_vol_ratio) and latest_vol_ratio > 0:
        if latest_vol_ratio >= 2.0:
            scores["volume"] = 1.0
        elif latest_vol_ratio >= 1.0:
            scores["volume"] = 0.5
        elif latest_vol_ratio >= 0.7:
            scores["volume"] = 0.0
        else:
            scores["volume"] = -0.3

    # Regime classification
    boll_bw = None
    if pd.notna(latest_upper) and pd.notna(latest_middle) and latest_middle != 0:
        boll_bw = (latest_upper - latest_lower) / latest_middle

    adx_val = latest_adx if pd.notna(latest_adx) else 0.0
    if adx_val >= 25:
        regime = "volatile" if boll_bw and boll_bw > 0.04 else "trending"
    elif adx_val >= SCALP_ADX_THRESHOLD:
        regime = "ranging"
    else:
        regime = "ranging"

    # ADX threshold filter
    if adx_val < SCALP_ADX_THRESHOLD:
        return Signal(
            symbol="XAUUSD",
            signal_type=SignalType.HOLD,
            confidence=0.0,
            price=current_price,
            timestamp=timestamp,
            timeframe="M1",
            indicators=scores,
            reason=f"Scalp ADX={adx_val:.1f} < {SCALP_ADX_THRESHOLD} — ranging",
            regime=regime,
            trading_mode=TradingMode.SCALP,
        )

    # Weighted score
    total_weight = 0.0
    weighted_sum = 0.0
    for name, score in scores.items():
        weight = SCALP_WEIGHTS.get(name, 0.0)
        if weight > 0:
            weighted_sum += score * weight
            total_weight += weight

    weighted_score = weighted_sum / total_weight if total_weight > 0 else 0.0

    # Direction and signal type
    threshold = SCALP_DIRECTION_THRESHOLD
    if weighted_score > threshold:
        signal_type = SignalType.BUY
    elif weighted_score < -threshold:
        signal_type = SignalType.SELL
    else:
        signal_type = SignalType.HOLD

    # Confidence
    raw_confidence = abs(weighted_score)
    direction = 1 if weighted_score > 0 else (-1 if weighted_score < 0 else 0)
    strong_agree_count = sum(
        1 for s in scores.values()
        if abs(s) >= 0.5 and ((direction > 0 and s > 0) or (direction < 0 and s < 0))
    )
    if strong_agree_count >= 3:
        boost = 0.15
    elif strong_agree_count >= 2:
        boost = 0.10
    else:
        boost = 0.0
    confidence = min(raw_confidence + boost, raw_confidence + 0.15, 1.0)

    # Build reason
    active_indicators = [f"{k}={v:+.1f}" for k, v in scores.items() if v != 0]
    reason = f"Scalp Score={weighted_score:+.2f} | " + ", ".join(active_indicators)
    reason += f" [{regime}]"

    # Minimum confidence filter
    if confidence < min_confidence and signal_type != SignalType.HOLD:
        signal_type = SignalType.HOLD
        reason += f" (confidence {confidence:.2f} below {min_confidence})"

    return Signal(
        symbol="XAUUSD",
        signal_type=signal_type,
        confidence=confidence,
        price=current_price,
        timestamp=timestamp,
        timeframe="M1",
        indicators=scores,
        reason=reason,
        regime=regime,
        trading_mode=TradingMode.SCALP,
    )