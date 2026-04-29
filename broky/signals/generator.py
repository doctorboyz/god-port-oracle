"""Signal generation engine — combines indicators + scaling rules to produce signals."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from shared.models import Signal, SignalType, SessionType, ScalingDecision, ScalingAction
from broky.indicators.ema import calculate_ema, calculate_ema_cross
from broky.indicators.macd import calculate_macd
from broky.indicators.bollinger import calculate_bollinger
from broky.indicators.atr import calculate_atr
from broky.indicators.adx import calculate_adx
from broky.indicators.volume import calculate_volume_ratio
from broky.signals.scaling import calculate_scaling_action, calculate_entry_and_change


# Indicator weights — best practice: 1 indicator per category, no redundancy
# Trend: EMA Cross + ADX = 35% | Momentum: MACD = 35%
# Volatility: Bollinger = 10% | Confirmation: Volume = 15%
# RSI + Stochastic removed — both momentum, redundant with MACD
INDICATOR_WEIGHTS = {
    "ema_cross": 0.15,   # Trend direction (short-term EMA 9/21)
    "ema_trend": 0.05,   # Long-term trend (EMA 50/200) — direction filter
    "adx": 0.15,         # Trend strength
    "macd": 0.35,        # Momentum (sole momentum indicator)
    "bollinger": 0.10,   # Volatility (ADX-filtered, reduced to prevent overtrading)
    "volume": 0.15,      # Confirmation
}

# Session multipliers — adjust confidence based on trading session liquidity
# NOTE: Disabled for now (all 1.0) — session filtering needs separate threshold tuning
# Will be enabled after forward testing validates the adjustment
SESSION_CONFIDENCE_MULTIPLIER = {
    SessionType.OVERLAP: 1.0,    # Best liquidity: no change (future: boost to 1.1)
    SessionType.LONDON: 1.0,     # Good liquidity: no change
    SessionType.NY: 1.0,         # Good liquidity: no change
    SessionType.ASIAN: 1.0,     # Low liquidity: no change (future: reduce to 0.85)
}

# Confidence thresholds (tuned from H1 backtest scan)
# v3: Raised to 0.60 to reduce overtrading (was 0.55 → 604 trades, MaxDD 51%)
MIN_CONFIDENCE = 0.60
STRONG_SIGNAL = 0.75


def classify_session(timestamp: datetime) -> SessionType:
    """Classify market session based on UTC hour.

    Args:
        timestamp: UTC datetime.

    Returns:
        SessionType enum.

    Example:
        >>> classify_session(datetime(2026, 1, 1, 14, 0))  # 14:00 UTC = overlap
        SessionType.OVERLAP
    """
    hour = timestamp.hour
    if 13 <= hour < 16:
        return SessionType.OVERLAP
    if 8 <= hour < 16:
        return SessionType.LONDON
    if 13 <= hour < 22:
        return SessionType.NY
    return SessionType.ASIAN


def calculate_indicator_scores(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    volume: pd.Series,
) -> tuple[dict[str, float], float]:
    """Calculate individual indicator scores for the latest candle.

    Each indicator produces a score from -1 (bearish) to +1 (bullish).

    Args:
        close: Close price series.
        high: High price series.
        low: Low price series.
        volume: Volume series.

    Returns:
        Tuple of (scores dict, latest_adx value).
    """
    scores = {}

    # ADX — trend strength and direction
    # ADX > 20 = trend present, +DI/-DI = direction
    adx, plus_di, minus_di = calculate_adx(high, low, close, period=14)
    latest_adx = adx.iloc[-1]
    latest_pdi = plus_di.iloc[-1]
    latest_mdi = minus_di.iloc[-1]
    adx_trending = False
    if pd.notna(latest_adx) and pd.notna(latest_pdi) and pd.notna(latest_mdi):
        if latest_adx >= 25:
            adx_trending = True
            # Strong trend: score based on direction
            if latest_adx >= 50:
                scores["adx"] = 1.0 if latest_pdi > latest_mdi else -1.0
            else:
                scores["adx"] = 0.5 if latest_pdi > latest_mdi else -0.5
        elif latest_adx >= 20:
            # Trend forming: mild signal
            scores["adx"] = 0.3 if latest_pdi > latest_mdi else -0.3
            adx_trending = False  # Not strong enough to filter Bollinger
        else:
            # No trend: ADX = 0 (ranging market)
            scores["adx"] = 0.0

    # MACD — sole momentum indicator (RSI removed: redundant with MACD)
    macd_result = calculate_macd(close)
    hist = macd_result.histogram.iloc[-1]
    if pd.notna(hist):
        scores["macd"] = 1.0 if hist > 0 else -1.0

    # EMA Cross — crossover detection with trend fallback
    _, _, crossover = calculate_ema_cross(close, fast_period=9, slow_period=21)
    latest_cross = crossover.iloc[-1]
    if pd.notna(latest_cross) and latest_cross != 0:
        # Fresh crossover: strong signal
        scores["ema_cross"] = float(latest_cross)
    else:
        # No crossover: use trend direction as soft signal
        fast_val = calculate_ema(close, 9).iloc[-1]
        slow_val = calculate_ema(close, 21).iloc[-1]
        if pd.notna(fast_val) and pd.notna(slow_val):
            scores["ema_cross"] = 0.5 if fast_val > slow_val else -0.5

    # EMA Trend — long-term direction filter (EMA 50 vs 200)
    # Best practice for XAUUSD: trade with the big trend
    ema50 = calculate_ema(close, 50)
    ema200 = calculate_ema(close, 200)
    latest_ema50 = ema50.iloc[-1]
    latest_ema200 = ema200.iloc[-1]
    if pd.notna(latest_ema50) and pd.notna(latest_ema200):
        if latest_ema50 > latest_ema200:
            scores["ema_trend"] = 0.5  # Long-term uptrend
        else:
            scores["ema_trend"] = -0.5  # Long-term downtrend

    # Bollinger Bands — with ADX filter
    # When ADX > 25 (strong trend), Bollinger mean-reversion is unreliable
    # Reduce Bollinger weight (not zero) — let it confirm trend direction instead
    boll = calculate_bollinger(close, period=20, std_dev=2.0)
    latest_close = close.iloc[-1]
    latest_lower = boll.lower.iloc[-1]
    latest_upper = boll.upper.iloc[-1]
    if pd.notna(latest_close) and pd.notna(latest_lower) and pd.notna(latest_upper):
        if adx_trending:
            # Strong trend: Bollinger mean-reversion is dangerous → reduce to mild trend-follow
            if latest_close > latest_upper:
                scores["bollinger"] = -0.5  # Above upper = trend continuation (not reversal)
            elif latest_close < latest_lower:
                scores["bollinger"] = 0.5   # Below lower = trend continuation
            else:
                scores["bollinger"] = 0.0
        elif latest_close < latest_lower:
            scores["bollinger"] = 1.0  # Below lower = potential buy (ranging)
        elif latest_close > latest_upper:
            scores["bollinger"] = -1.0  # Above upper = potential sell (ranging)
        else:
            scores["bollinger"] = 0.0

    # Volume — bidirectional: high volume confirms direction, low volume weakens
    # Volume > 1x average → positive (confirms), Volume < 0.7x average → negative (weak signal)
    vol_ratio = calculate_volume_ratio(volume, period=20)
    latest_vol_ratio = vol_ratio.iloc[-1]
    if pd.notna(latest_vol_ratio) and latest_vol_ratio > 0:
        if latest_vol_ratio >= 2.0:
            scores["volume"] = 1.0    # Very high volume: strong confirmation
        elif latest_vol_ratio >= 1.0:
            scores["volume"] = 0.5   # Normal-high volume: moderate confirmation
        elif latest_vol_ratio >= 0.7:
            scores["volume"] = 0.0   # Average volume: neutral
        elif latest_vol_ratio >= 0.5:
            scores["volume"] = -0.3  # Low volume: weak signal
        else:
            scores["volume"] = -0.5  # Very low volume: unreliable signal

    return scores, latest_adx if pd.notna(latest_adx) else 0.0


def calculate_weighted_score(scores: dict[str, float]) -> float:
    """Calculate weighted average of indicator scores for signal direction.

    Args:
        scores: Dictionary of indicator name → score (-1 to +1).

    Returns:
        Weighted score from -1 to +1 (direction + magnitude).
    """
    total_weight = 0.0
    weighted_sum = 0.0
    for name, score in scores.items():
        weight = INDICATOR_WEIGHTS.get(name, 0.0)
        if weight > 0:
            weighted_sum += score * weight
            total_weight += weight

    if total_weight == 0:
        return 0.0

    return weighted_sum / total_weight


def calculate_consensus_confidence(scores: dict[str, float], direction: float) -> float:
    """Calculate confidence based on indicator agreement with the dominant direction.

    Unlike raw weighted average (which gets diluted by opposing signals),
    this measures how strongly indicators agree with the dominant direction.

    Args:
        scores: Dictionary of indicator name → score (-1 to +1).
        direction: Signal direction (>0 = bullish, <0 = bearish, 0 = neutral).

    Returns:
        Confidence from 0.0 to 1.0 based on same-direction agreement.
    """
    if direction == 0:
        return 0.0

    agreeing_weight = 0.0
    total_weight = 0.0
    for name, score in scores.items():
        weight = INDICATOR_WEIGHTS.get(name, 0.0)
        if weight > 0:
            total_weight += weight
            if (direction > 0 and score > 0) or (direction < 0 and score < 0):
                agreeing_weight += abs(score) * weight

    if total_weight == 0:
        return 0.0

    return min(agreeing_weight / total_weight, 1.0)


def calculate_signal_confidence(scores: dict[str, float], weighted_score: float) -> float:
    """Calculate signal confidence — raw score as base, with mild strong-agreement boost.

    Confidence is primarily driven by absolute weighted score (directional strength).
    A mild boost is applied when 2+ strong indicators (|score| >= 0.5) agree, but
    it cannot exceed raw_confidence + 0.15 to avoid creating false signals.

    Args:
        scores: Dictionary of indicator name → score (-1 to +1).
        weighted_score: Weighted average score from calculate_weighted_score.

    Returns:
        Confidence from 0.0 to 1.0.
    """
    raw_confidence = abs(weighted_score)

    # Strong agreement boost: if 2+ strong indicators (|score| >= 0.5) agree
    # in the same direction, give a modest confidence bump
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

    # Cap boost: can't exceed raw + 0.15 to avoid creating signals from noise
    confidence = min(raw_confidence + boost, raw_confidence + 0.15, 1.0)

    return confidence


def score_to_signal_type(score: float) -> SignalType:
    """Convert a weighted score to a signal type.

    Args:
        score: Weighted score from -1 to +1.

    Returns:
        BUY if score > threshold, SELL if score < -threshold, else HOLD.
    """
    threshold = 0.3
    if score > threshold:
        return SignalType.BUY
    if score < -threshold:
        return SignalType.SELL
    return SignalType.HOLD


def score_to_confidence(score: float) -> float:
    """Convert absolute score to confidence (0.0 to 1.0).

    Args:
        score: Weighted score from -1 to +1.

    Returns:
        Confidence value. Minimum 0.3 (below this = no actionable signal).
    """
    return min(abs(score), 1.0)


def generate_signal(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    volume: pd.Series,
    current_price: Optional[float] = None,
    entry_price: Optional[float] = None,
    timeframe: str = "M5",
    timestamp: Optional[datetime] = None,
    d1_trend: Optional[str] = None,
) -> Signal:
    """Generate a trading signal by combining indicator scores with JPMorgan scaling rules.

    This is the main entry point for signal generation. It:
    1. Calculates all indicator scores
    2. Applies weighted average to get a composite score
    3. Applies multi-timeframe trend filter (if provided)
    4. Determines signal type (BUY/SELL/HOLD)
    5. If an existing position exists, applies JPMorgan scaling rules
    6. Returns a Signal with confidence, reason, and scaling info

    Args:
        close: Close price series.
        high: High price series.
        low: Low price series.
        volume: Volume series.
        current_price: Current market price (defaults to latest close).
        entry_price: Entry price of existing position (for scaling decisions).
        timeframe: Timeframe string (default M5).
        timestamp: Candle timestamp for session classification (defaults to now).
        d1_trend: D1 trend direction ('bullish', 'bearish', or None for no filter).
            When provided, BUY signals are only allowed in bullish D1,
            SELL signals only in bearish D1.

    Returns:
        Signal with type, confidence, and reason.
    """
    if current_price is None:
        current_price = float(close.iloc[-1])

    if timestamp is None:
        timestamp = datetime.now(timezone.utc)

    scores, latest_adx = calculate_indicator_scores(close, high, low, volume)

    # Minimum ADX filter: do not trade in ranging markets (ADX < 20)
    if latest_adx < 20:
        return Signal(
            symbol="XAUUSD",
            signal_type=SignalType.HOLD,
            confidence=0.0,
            price=current_price,
            timestamp=timestamp,
            timeframe=timeframe,
            indicators=scores,
            reason=f"ADX={latest_adx:.1f} < 20 — ranging market, no trade",
        )

    weighted_score = calculate_weighted_score(scores)
    signal_type = score_to_signal_type(weighted_score)
    confidence = calculate_signal_confidence(scores, weighted_score)

    # Multi-timeframe trend filter: trade WITH the big trend, not against it
    # Soft filter: reduce confidence of counter-trend signals instead of blocking
    if d1_trend is not None:
        if d1_trend == "bullish" and signal_type == SignalType.SELL:
            confidence *= 0.5  # Counter-trend SELL: half confidence
            scores["_d1_soft"] = -0.5
        elif d1_trend == "bearish" and signal_type == SignalType.BUY:
            confidence *= 0.5  # Counter-trend BUY: half confidence
            scores["_d1_soft"] = -0.5

    # Apply session-based confidence multiplier
    session = classify_session(timestamp)
    session_mult = SESSION_CONFIDENCE_MULTIPLIER.get(session, 1.0)
    if session_mult != 1.0:
        confidence *= session_mult
        confidence = min(confidence, 1.0)  # Cap at 1.0

    # Build reason string from active indicators
    active_indicators = [f"{k}={v:+.1f}" for k, v in scores.items() if v != 0]
    reason = f"Score={weighted_score:+.2f} | " + ", ".join(active_indicators) if active_indicators else f"Score={weighted_score:+.2f}"

    # If we have an existing position, check JPMorgan scaling rules
    if entry_price is not None and entry_price > 0:
        price_change_pct = calculate_entry_and_change(entry_price, current_price)
        scaling = calculate_scaling_action(price_change_pct)
        reason += f" | Scaling: {scaling.reason}"

        # Override signal with scaling action if actionable
        if scaling.action != ScalingAction.HOLD:
            if scaling.action.value == "BUY" and signal_type == SignalType.HOLD:
                signal_type = SignalType.BUY
                reason += " (scaling override)"
            elif scaling.action.value == "SELL" and signal_type != SignalType.SELL:
                signal_type = SignalType.SELL
                reason += " (scaling override)"

    # Only produce actionable signals above minimum confidence
    if confidence < MIN_CONFIDENCE and signal_type != SignalType.HOLD:
        signal_type = SignalType.HOLD
        reason += f" (confidence {confidence:.2f} below {MIN_CONFIDENCE})"

    return Signal(
        symbol="XAUUSD",
        signal_type=signal_type,
        confidence=confidence,
        price=current_price,
        timestamp=timestamp,
        timeframe=timeframe,
        indicators=scores,
        reason=reason,
    )