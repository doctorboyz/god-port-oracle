"""M5 Scalp signal generator — 6-EMA Ribbon Cloud with HTF alignment.

Professional XAUUSD scalping strategy based on research from multiple sources:
- 6-EMA Ribbon (8, 13, 21, 34, 55, 89) for trend identification and pullback entries
- 4-level ATR-based take profit for position scaling
- H4 + D1 trend alignment as soft confidence filter (not hard block)
- Session gate (London + Overlap + NY only)
- Signal scoring: ribbon expansion, ATR level, session position, pullback depth

Key differences from M1 scalp:
- M5 timeframe (less noise, professional consensus sweet spot)
- Ribbon order requirement (all 6 EMAs must be aligned)
- H4+D1 alignment filter (soft: reduces confidence, doesn't block)
- ADX threshold 15 (more stable on M5)
- Direction threshold 0.20 (between M1's 0.15 and swing's 0.30)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from broky.indicators.ema import calculate_ema
from broky.indicators.macd import calculate_macd
from broky.indicators.bollinger import calculate_bollinger
from broky.indicators.atr import calculate_atr
from broky.indicators.adx import calculate_adx
from broky.indicators.volume import calculate_volume_ratio
from broky.risk.spread_filter import check_spread
from shared.models import Signal, SignalType, TradingMode

# M5 Scalp indicator weights — ribbon-heavy with momentum confirmation
M5_SCALP_WEIGHTS = {
    "ema_ribbon": 0.35,  # Ribbon order + pullback (primary signal)
    "macd": 0.25,        # Momentum confirmation
    "adx": 0.10,         # Trend strength
    "bollinger": 0.10,   # Volatility / mean-reversion in ranging
    "volume": 0.15,      # Confirmation
    "ema_trend": 0.05,   # Long-term direction filter
}

# M5 indicator periods (slower than M1, faster than swing)
M5_SCALP_PERIODS = {
    "ema_fast": 8,       # Ribbon layer 1
    "ema_2": 13,         # Ribbon layer 2
    "ema_3": 21,         # Ribbon layer 3
    "ema_4": 34,         # Ribbon layer 4
    "ema_5": 55,         # Ribbon layer 5
    "ema_6": 89,         # Ribbon layer 6
    "ema_trend_fast": 21, # Mid-term trend
    "ema_trend_slow": 55, # Mid-term trend
    "adx": 14,           # Standard period (more stable than M1's 7)
    "macd_fast": 8,
    "macd_slow": 21,
    "macd_signal": 5,
    "bollinger": 14,
    "bollinger_std": 1.8,
    "atr": 10,
    "volume_ma": 14,
}

# M5 Scalp thresholds
M5_SCALP_MIN_CONFIDENCE = 0.50
M5_SCALP_ADX_THRESHOLD = 15
M5_SCALP_SPREAD_MAX = 30
M5_SCALP_DIRECTION_THRESHOLD = 0.20

# Sessions where M5 scalp is allowed (all liquid sessions)
M5_SCALP_SESSIONS = {"london", "overlap", "ny"}

# HTF alignment confidence reduction (soft filter)
HTF_DISAGREE_MULTIPLIER = 0.6  # Reduce confidence by 40% when HTF disagrees


def classify_session_m5(hour: int) -> str:
    """Classify trading session by UTC hour."""
    if 13 <= hour < 16:
        return "overlap"
    if 8 <= hour < 16:
        return "london"
    if 13 <= hour < 22:
        return "ny"
    return "asian"


def classify_ribbon_state(
    ema_8: float, ema_13: float, ema_21: float,
    ema_34: float, ema_55: float, ema_89: float,
) -> str:
    """Classify the 6-EMA ribbon state.

    Returns:
        "bullish": All EMAs in ascending order (8 > 13 > 21 > 34 > 55 > 89)
        "bearish": All EMAs in descending order (8 < 13 < 21 < 34 < 55 < 89)
        "squeeze": EMAs converging (spreads narrowing)
        "chop": EMAs crossing randomly
    """
    emas = [ema_8, ema_13, ema_21, ema_34, ema_55, ema_89]

    # Check for squeeze first: all EMAs within a tight band (even if technically ordered)
    ema_range = max(emas) - min(emas)
    avg_ema = sum(emas) / len(emas)
    if avg_ema > 0 and ema_range / avg_ema < 0.003:  # Within 0.3% band
        return "squeeze"

    # Check for perfect bullish order
    if all(emas[i] > emas[i + 1] for i in range(len(emas) - 1)):
        return "bullish"

    # Check for perfect bearish order
    if all(emas[i] < emas[i + 1] for i in range(len(emas) - 1)):
        return "bearish"

    return "chop"


def calculate_ribbon_expansion(
    ema_8: float, ema_13: float, ema_55: float, ema_89: float,
    prev_ema_8: float, prev_ema_13: float, prev_ema_55: float, prev_ema_89: float,
) -> float:
    """Calculate how much the ribbon is expanding (positive) or compressing (negative).

    Returns a float: positive = expanding (trend accelerating), negative = compressing.
    """
    current_spread = (ema_8 - ema_89) + (ema_13 - ema_55)
    prev_spread = (prev_ema_8 - prev_ema_89) + (prev_ema_13 - prev_ema_55)
    if abs(prev_spread) < 1e-6:
        return 0.0
    expansion = (current_spread - prev_spread) / abs(prev_spread)
    return max(-1.0, min(1.0, expansion))  # Clamp to [-1, 1]


PULLBACK_TOLERANCE = 0.005  # 0.5% zone around fast cloud for pullback check


def is_pullback_to_fast_cloud(
    latest_close: float,
    latest_low: float,
    latest_high: float,
    ema_8: float, ema_21: float,
    direction: int,
) -> bool:
    """Check if price pulled back to the fast cloud (EMA 8-21 zone).

    Uses candle wick (high/low) instead of just close — a pullback means
    the candle touched the EMA 8-21 zone, even if it closed above/below.

    For BUY: candle low touched EMA 8-21 zone (pullback in uptrend)
    For SELL: candle high touched EMA 8-21 zone (pullback in downtrend)
    """
    if direction > 0:  # Bullish — low of candle should touch near EMA 8 or below
        return latest_low <= ema_8 * (1 + PULLBACK_TOLERANCE)
    elif direction < 0:  # Bearish — high of candle should touch near EMA 8 or above
        return latest_high >= ema_8 * (1 - PULLBACK_TOLERANCE)
    return False


def calculate_signal_score(
    ribbon_state: str,
    ribbon_expansion: float,
    atr_ratio: float,
    session: str,
    pullback_depth: str,
) -> float:
    """Score a signal based on quality factors.

    Args:
        ribbon_state: "bullish" or "bearish"
        ribbon_expansion: How much the ribbon is expanding (positive = stronger)
        atr_ratio: Current ATR / average ATR (>1 = more volatile = better)
        session: Current session name
        pullback_depth: "shallow" (to EMA 8), "medium" (to EMA 21), "deep" (to EMA 55+)

    Returns:
        Score from 0.0 to 1.0. Only take signals above ~0.5.
    """
    score = 0.0

    # Ribbon expansion (0.25 weight)
    if ribbon_expansion > 0.05:
        score += 0.25
    elif ribbon_expansion > 0:
        score += 0.15
    elif ribbon_expansion > -0.02:
        score += 0.05
    # Negative expansion = compressing, low score

    # ATR level (0.25 weight)
    if atr_ratio > 1.5:
        score += 0.25
    elif atr_ratio > 1.0:
        score += 0.20
    elif atr_ratio > 0.7:
        score += 0.10

    # Session position (0.25 weight)
    if session == "overlap":
        score += 0.25
    elif session == "london":
        score += 0.20
    elif session == "ny":
        score += 0.15

    # Pullback depth (0.25 weight) — shallow is stronger
    if pullback_depth == "shallow":
        score += 0.25
    elif pullback_depth == "medium":
        score += 0.15
    elif pullback_depth == "deep":
        score += 0.05

    return score


def generate_m5_scalp_signal(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    volume: pd.Series,
    current_price: Optional[float] = None,
    timestamp: Optional[datetime] = None,
    spread: Optional[float] = None,
    d1_trend: Optional[str] = None,
    h4_trend: Optional[str] = None,
    min_confidence: float = M5_SCALP_MIN_CONFIDENCE,
    max_spread: float = M5_SCALP_SPREAD_MAX,
) -> Signal:
    """Generate an M5 scalp signal using 6-EMA Ribbon Cloud strategy.

    Args:
        close: M5 close price series (minimum 200 bars for H4 alignment).
        high: M5 high price series.
        low: M5 low price series.
        volume: M5 volume series.
        current_price: Current price (defaults to latest close).
        timestamp: Candle timestamp (defaults to now UTC).
        spread: Current spread in points.
        d1_trend: D1 trend direction ("bullish"/"bearish"/None).
        h4_trend: H4 trend direction ("bullish"/"bearish"/"neutral"/None).
        min_confidence: Minimum confidence threshold.
        max_spread: Maximum spread in points.

    Returns:
        Signal with type, confidence, and reason.
    """
    # Input validation
    min_bars = 200
    if len(close) < min_bars:
        price = current_price or 0.0
        return Signal(
            symbol="XAUUSD",
            signal_type=SignalType.HOLD,
            confidence=0.0,
            price=price,
            timestamp=timestamp or datetime.now(timezone.utc),
            timeframe="M5",
            reason=f"M5 insufficient data: {len(close)} bars (need {min_bars})",
            regime="unknown",
            trading_mode=TradingMode.M5_SCALP,
        )

    if current_price is None:
        current_price = float(close.iloc[-1])

    # Guard against NaN price
    if pd.isna(current_price) or current_price <= 0:
        last_valid = close.dropna().iloc[-1] if len(close.dropna()) > 0 else 0.0
        safe_price = float(last_valid) if last_valid > 0 else 1.0
        return Signal(
            symbol="XAUUSD",
            signal_type=SignalType.HOLD,
            confidence=0.0,
            price=safe_price,
            timestamp=timestamp or datetime.now(timezone.utc),
            timeframe="M5",
            reason="M5 invalid price (NaN or <= 0)",
            regime="unknown",
            trading_mode=TradingMode.M5_SCALP,
        )

    if timestamp is None:
        timestamp = datetime.now(timezone.utc)

    # Session gate
    session = classify_session_m5(timestamp.hour)
    if session not in M5_SCALP_SESSIONS:
        return Signal(
            symbol="XAUUSD",
            signal_type=SignalType.HOLD,
            confidence=0.0,
            price=current_price,
            timestamp=timestamp,
            timeframe="M5",
            reason=f"M5 scalp blocked: {session} session (liquid sessions only)",
            regime="unknown",
            trading_mode=TradingMode.M5_SCALP,
        )

    # Spread filter — require spread data for live trading
    if spread is None:
        return Signal(
            symbol="XAUUSD",
            signal_type=SignalType.HOLD,
            confidence=0.0,
            price=current_price,
            timestamp=timestamp,
            timeframe="M5",
            reason="M5 scalp blocked: spread data unavailable",
            regime="unknown",
            trading_mode=TradingMode.M5_SCALP,
        )
    if not check_spread(spread, max_spread):
        return Signal(
            symbol="XAUUSD",
            signal_type=SignalType.HOLD,
            confidence=0.0,
            price=current_price,
            timestamp=timestamp,
            timeframe="M5",
            reason=f"M5 scalp blocked: spread={spread:.0f} > max={max_spread:.0f}",
            regime="unknown",
            trading_mode=TradingMode.M5_SCALP,
        )

    # Calculate 6-EMA Ribbon
    p = M5_SCALP_PERIODS
    ema_8 = calculate_ema(close, p["ema_fast"])
    ema_13 = calculate_ema(close, p["ema_2"])
    ema_21 = calculate_ema(close, p["ema_3"])
    ema_34 = calculate_ema(close, p["ema_4"])
    ema_55 = calculate_ema(close, p["ema_5"])
    ema_89 = calculate_ema(close, p["ema_6"])

    latest_ema_8 = ema_8.iloc[-1]
    latest_ema_13 = ema_13.iloc[-1]
    latest_ema_21 = ema_21.iloc[-1]
    latest_ema_34 = ema_34.iloc[-1]
    latest_ema_55 = ema_55.iloc[-1]
    latest_ema_89 = ema_89.iloc[-1]

    # Previous EMAs for expansion check
    prev_ema_8 = ema_8.iloc[-2] if len(ema_8) > 1 else latest_ema_8
    prev_ema_13 = ema_13.iloc[-2] if len(ema_13) > 1 else latest_ema_13
    prev_ema_55 = ema_55.iloc[-2] if len(ema_55) > 1 else latest_ema_55
    prev_ema_89 = ema_89.iloc[-2] if len(ema_89) > 1 else latest_ema_89

    # Ribbon state classification
    ribbon_state = classify_ribbon_state(
        latest_ema_8, latest_ema_13, latest_ema_21,
        latest_ema_34, latest_ema_55, latest_ema_89,
    )

    # Calculate expansion
    ribbon_expansion = calculate_ribbon_expansion(
        latest_ema_8, latest_ema_13, latest_ema_55, latest_ema_89,
        prev_ema_8, prev_ema_13, prev_ema_55, prev_ema_89,
    )

    # If ribbon is not in order, skip (squeeze or chop)
    if ribbon_state not in ("bullish", "bearish"):
        return Signal(
            symbol="XAUUSD",
            signal_type=SignalType.HOLD,
            confidence=0.0,
            price=current_price,
            timestamp=timestamp,
            timeframe="M5",
            reason=f"M5 ribbon {ribbon_state} — no directional order",
            indicators={"ribbon_state": ribbon_state},
            regime="unknown",
            trading_mode=TradingMode.M5_SCALP,
        )

    # Determine direction from ribbon
    direction = 1 if ribbon_state == "bullish" else -1

    # Pullback check — candle wick touched the fast cloud (EMA 8-21 zone)
    latest_low = float(low.iloc[-1]) if pd.notna(low.iloc[-1]) else current_price
    latest_high = float(high.iloc[-1]) if pd.notna(high.iloc[-1]) else current_price
    has_pullback = is_pullback_to_fast_cloud(
        current_price, latest_low, latest_high,
        latest_ema_8, latest_ema_21, direction,
    )

    # Confirmation candle: close above EMA 8 (buy) or below EMA 8 (sell)
    confirmed = False
    if direction > 0 and current_price > latest_ema_8:
        confirmed = True
    elif direction < 0 and current_price < latest_ema_8:
        confirmed = True

    # Ribbon must be expanding (trend accelerating)
    expanding = ribbon_expansion > -0.01  # Allow slight compression but not collapse

    # Calculate additional indicators
    scores = {}

    # ADX
    adx, plus_di, minus_di = calculate_adx(high, low, close, period=p["adx"])
    latest_adx = adx.iloc[-1]
    latest_pdi = plus_di.iloc[-1]
    latest_mdi = minus_di.iloc[-1]
    if pd.notna(latest_adx) and pd.notna(latest_pdi) and pd.notna(latest_mdi):
        if latest_adx >= 25:
            scores["adx"] = 1.0 if latest_pdi > latest_mdi else -1.0
        elif latest_adx >= 15:
            scores["adx"] = 0.5 if latest_pdi > latest_mdi else -0.5
        else:
            scores["adx"] = 0.0

    # MACD
    macd_result = calculate_macd(
        close,
        fast_period=p["macd_fast"],
        slow_period=p["macd_slow"],
        signal_period=p["macd_signal"],
    )
    hist = macd_result.histogram.iloc[-1]
    if pd.notna(hist):
        scores["macd"] = 1.0 if hist > 0 else -1.0

    # Long-term EMA trend (21 vs 55)
    ema_trend_fast = calculate_ema(close, p["ema_trend_fast"]).iloc[-1]
    ema_trend_slow = calculate_ema(close, p["ema_trend_slow"]).iloc[-1]
    if pd.notna(ema_trend_fast) and pd.notna(ema_trend_slow):
        scores["ema_trend"] = 0.5 if ema_trend_fast > ema_trend_slow else -0.5

    # Bollinger Bands
    boll = calculate_bollinger(close, period=p["bollinger"], std_dev=p["bollinger_std"])
    latest_lower = boll.lower.iloc[-1]
    latest_upper = boll.upper.iloc[-1]
    latest_middle = boll.middle.iloc[-1]
    boll_bw = None
    if pd.notna(latest_upper) and pd.notna(latest_middle) and latest_middle != 0:
        boll_bw = (latest_upper - latest_lower) / latest_middle
    if pd.notna(current_price) and pd.notna(latest_lower) and pd.notna(latest_upper):
        adx_trending = pd.notna(latest_adx) and latest_adx >= 25
        if adx_trending:
            # Trend-following: price above upper BB = bullish continuation
            scores["bollinger"] = 0.5 if current_price > latest_upper else (-0.5 if current_price < latest_lower else 0.0)
        elif current_price < latest_lower:
            # Mean-reversion: oversold = bullish bounce
            scores["bollinger"] = 1.0
        elif current_price > latest_upper:
            # Mean-reversion: overbought = bearish reversal
            scores["bollinger"] = -1.0
        else:
            scores["bollinger"] = 0.0

    # Volume
    vol_ratio = calculate_volume_ratio(volume, period=p["volume_ma"])
    latest_vol_ratio = vol_ratio.iloc[-1]
    if pd.notna(latest_vol_ratio) and latest_vol_ratio > 0:
        if latest_vol_ratio >= 2.0:
            scores["volume"] = 1.0
        elif latest_vol_ratio >= 1.0:
            scores["volume"] = 0.5
        elif latest_vol_ratio >= 0.7:
            scores["volume"] = 0.0
        elif latest_vol_ratio >= 0.5:
            scores["volume"] = -0.3
        else:
            scores["volume"] = -0.5

    # Ribbon score — based on state
    scores["ema_ribbon"] = 1.0 if ribbon_state == "bullish" else -1.0

    # Regime classification
    adx_val = latest_adx if pd.notna(latest_adx) else 0.0
    if adx_val >= 25:
        regime = "volatile" if boll_bw and boll_bw > 0.04 else "trending"
    else:
        regime = "ranging"

    # ADX threshold filter
    if adx_val < M5_SCALP_ADX_THRESHOLD:
        return Signal(
            symbol="XAUUSD",
            signal_type=SignalType.HOLD,
            confidence=0.0,
            price=current_price,
            timestamp=timestamp,
            timeframe="M5",
            indicators=scores,
            reason=f"M5 ADX={adx_val:.1f} < {M5_SCALP_ADX_THRESHOLD} — weak trend",
            regime=regime,
            trading_mode=TradingMode.M5_SCALP,
        )

    # Must have at least pullback or confirmation, plus expansion
    if not ((has_pullback or confirmed) and expanding):
        missing = []
        if not has_pullback and not confirmed:
            missing.append("no pullback or confirmation")
        elif not has_pullback:
            missing.append("no pullback")
        elif not confirmed:
            missing.append("no confirmation")
        if not expanding:
            missing.append("not expanding")
        return Signal(
            symbol="XAUUSD",
            signal_type=SignalType.HOLD,
            confidence=0.0,
            price=current_price,
            timestamp=timestamp,
            timeframe="M5",
            indicators=scores,
            reason=f"M5 ribbon {ribbon_state}: {', '.join(missing)}",
            regime=regime,
            trading_mode=TradingMode.M5_SCALP,
        )

    # Weighted score from indicators
    total_weight = 0.0
    weighted_sum = 0.0
    for name, score in scores.items():
        weight = M5_SCALP_WEIGHTS.get(name, 0.0)
        if weight > 0:
            weighted_sum += score * weight
            total_weight += weight
    weighted_score = weighted_sum / total_weight if total_weight > 0 else 0.0

    # Direction and signal type
    signal_type = SignalType.BUY if weighted_score > M5_SCALP_DIRECTION_THRESHOLD else (
        SignalType.SELL if weighted_score < -M5_SCALP_DIRECTION_THRESHOLD else SignalType.HOLD
    )

    # Confidence calculation
    raw_confidence = abs(weighted_score)
    # Exclude ema_ribbon from agree count — it defines the direction, not a confirmation
    strong_agree_count = sum(
        1 for name, s in scores.items()
        if name != "ema_ribbon" and abs(s) >= 0.5 and ((direction > 0 and s > 0) or (direction < 0 and s < 0))
    )
    if strong_agree_count >= 3:
        boost = 0.15
    elif strong_agree_count >= 2:
        boost = 0.10
    else:
        boost = 0.0
    confidence = min(raw_confidence + boost, 1.0)

    # Signal quality scoring
    pullback_depth = "shallow"  # default for confirmed signal near EMA 8
    if current_price and pd.notna(latest_ema_21) and pd.notna(latest_ema_55):
        if abs(current_price - latest_ema_8) < abs(current_price - latest_ema_21):
            pullback_depth = "shallow"
        elif abs(current_price - latest_ema_21) < abs(current_price - latest_ema_55):
            pullback_depth = "medium"
        else:
            pullback_depth = "deep"

    atr_series = calculate_atr(high, low, close, period=p["atr"])
    latest_atr = atr_series.iloc[-1]
    if pd.isna(latest_atr):
        atr_ratio = 1.0
    else:
        avg_atr = atr_series.rolling(20).mean().iloc[-1] if len(atr_series) >= 20 else latest_atr
        atr_ratio = latest_atr / avg_atr if pd.notna(avg_atr) and avg_atr > 0 else 1.0

    quality_score = calculate_signal_score(
        ribbon_state=ribbon_state,
        ribbon_expansion=ribbon_expansion,
        atr_ratio=atr_ratio,
        session=session,
        pullback_depth=pullback_depth,
    )

    # Boost confidence by quality score (max +0.10)
    confidence += quality_score * 0.10
    confidence = min(confidence, 1.0)

    # HTF alignment (SOFT filter: reduce confidence, don't block)
    if d1_trend is not None and signal_type != SignalType.HOLD:
        if d1_trend == "bullish" and signal_type == SignalType.SELL:
            confidence *= HTF_DISAGREE_MULTIPLIER
        elif d1_trend == "bearish" and signal_type == SignalType.BUY:
            confidence *= HTF_DISAGREE_MULTIPLIER

    if h4_trend is not None and signal_type != SignalType.HOLD:
        if h4_trend == "bullish" and signal_type == SignalType.SELL:
            confidence *= HTF_DISAGREE_MULTIPLIER
        elif h4_trend == "bearish" and signal_type == SignalType.BUY:
            confidence *= HTF_DISAGREE_MULTIPLIER

    # Build reason string
    active_indicators = [f"{k}={v:+.1f}" for k, v in scores.items() if v != 0]
    reason = f"M5 Score={weighted_score:+.2f} | " + ", ".join(active_indicators) if active_indicators else f"M5 Score={weighted_score:+.2f}"
    reason += f" [{regime}] ribbon={ribbon_state} quality={quality_score:.2f}"

    if d1_trend:
        reason += f" d1={d1_trend}"
    if h4_trend:
        reason += f" h4={h4_trend}"

    # Min confidence filter
    if confidence < min_confidence and signal_type != SignalType.HOLD:
        signal_type = SignalType.HOLD
        reason += f" (confidence {confidence:.2f} below {min_confidence})"

    # ATR for TP calculation (stored in indicators for trader use)
    indicators = dict(scores)
    indicators["atr"] = float(latest_atr) if pd.notna(latest_atr) else 0.0
    indicators["ribbon_state"] = ribbon_state
    indicators["ribbon_expansion"] = float(ribbon_expansion)
    indicators["quality_score"] = quality_score
    indicators["pullback_depth"] = pullback_depth
    if pd.notna(latest_ema_8):
        indicators["ema_8"] = float(latest_ema_8)
    if pd.notna(latest_ema_21):
        indicators["ema_21"] = float(latest_ema_21)
    if pd.notna(latest_ema_55):
        indicators["ema_55"] = float(latest_ema_55)
    if pd.notna(latest_ema_89):
        indicators["ema_89"] = float(latest_ema_89)

    return Signal(
        symbol="XAUUSD",
        signal_type=signal_type,
        confidence=confidence,
        price=current_price,
        timestamp=timestamp,
        timeframe="M5",
        indicators=indicators,
        reason=reason,
        regime=regime,
        trading_mode=TradingMode.M5_SCALP,
    )