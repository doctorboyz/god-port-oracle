"""Signal generation engine — combines indicators + scaling rules to produce signals."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

from shared.models import Signal, SignalType, SessionType, MarketRegime, ScalingDecision, ScalingAction, TradingMode
from broky.signals.registry import strategy
from broky.indicators.ema import calculate_ema, calculate_ema_cross
from broky.indicators.macd import calculate_macd
from broky.indicators.bollinger import calculate_bollinger
from broky.indicators.atr import calculate_atr
from broky.indicators.adx import calculate_adx
from broky.indicators.volume import calculate_volume_ratio
from broky.signals.scaling import calculate_scaling_action, calculate_entry_and_change

logger = logging.getLogger(__name__)

# Indicator weights — loaded from JSON config if available, else defaults
# Trend: EMA Cross + ADX = 35% | Momentum: MACD = 35%
# Volatility: Bollinger = 10% | Confirmation: Volume = 15%
_DEFAULT_WEIGHTS: dict[str, float] = {
    "ema_cross": 0.15,
    "ema_trend": 0.05,
    "adx": 0.15,
    "macd": 0.35,
    "bollinger": 0.10,
    "volume": 0.15,
}

_WEIGHTS_FILE = Path(__file__).parent.parent / "config" / "indicator_weights.json"


def _load_weights() -> dict[str, float]:
    """Load indicator weights from JSON config, falling back to defaults."""
    if _WEIGHTS_FILE.exists():
        try:
            data = json.loads(_WEIGHTS_FILE.read_text(encoding="utf-8"))
            weights = data.get("weights", data)
            # Validate: all expected keys present
            if all(k in weights for k in _DEFAULT_WEIGHTS):
                return {k: float(weights[k]) for k in _DEFAULT_WEIGHTS}
            logger.warning("Weights file missing keys, using defaults")
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("Failed to load weights: %s, using defaults", e)
    return dict(_DEFAULT_WEIGHTS)


INDICATOR_WEIGHTS = _load_weights()

# Session multipliers — adjust confidence based on trading session liquidity
# NOTE: Disabled for now (all 1.0) — session filtering needs separate threshold tuning
# Will be enabled after forward testing validates the adjustment
SESSION_CONFIDENCE_MULTIPLIER = {
    SessionType.OVERLAP: 1.1,    # Best liquidity: boost confidence
    SessionType.LONDON: 1.0,     # Good liquidity: no change
    SessionType.NY: 1.0,         # Good liquidity: no change
    SessionType.ASIAN: 0.70,     # Low liquidity: strong reduction
}

# Confidence thresholds (tuned from H1 backtest scan)
# v3: Raised to 0.60 to reduce overtrading (was 0.55 → 604 trades, MaxDD 51%)
MIN_CONFIDENCE = 0.60
STRONG_SIGNAL = 0.75


def classify_regime(latest_adx: float, boll_bandwidth: Optional[float] = None) -> str:
    """Classify market regime based on ADX and Bollinger Band width.

    Args:
        latest_adx: Current ADX value.
        boll_bandwidth: Bollinger Band bandwidth (upper - lower) / middle. Optional.

    Returns:
        Regime string: 'trending', 'ranging', or 'volatile'.
    """
    if latest_adx >= 25:
        # Strong trend — check for volatility
        if boll_bandwidth is not None and boll_bandwidth > 0.04:
            return MarketRegime.VOLATILE.value
        return MarketRegime.TRENDING.value
    elif latest_adx >= 20:
        # Trend forming — classify as ranging (choppy)
        return MarketRegime.RANGING.value
    else:
        # No trend — ranging
        return MarketRegime.RANGING.value


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
    # When ADX 20-25 (trend forming), reduce Bollinger to mild signal (false signal zone)
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
        elif 20 <= latest_adx < 25:
            # Trend forming: reduce Bollinger from ±1.0 to ±0.3 (choppy zone = false signals)
            if latest_close < latest_lower:
                scores["bollinger"] = 0.3   # Mild buy (not full confidence)
            elif latest_close > latest_upper:
                scores["bollinger"] = -0.3  # Mild sell
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


def score_to_signal_type(score: float, learning_mode: bool = False) -> SignalType:
    """Convert a weighted score to a signal type.

    Args:
        score: Weighted score from -1 to +1.
        learning_mode: If True, use lower threshold for more signals.

    Returns:
        BUY if score > threshold, SELL if score < -threshold, else HOLD.
    """
    threshold = 0.05 if learning_mode else 0.3
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


# Ranging market signal constants
_RANGING_BOLL_THRESHOLD = 0.70   # Price must be >= 70% toward a band to trigger (0.0=mid, 1.0=at band)
_RANGING_CONFIDENCE_CAP = 0.65   # Ranging signals capped lower than trending (mean reversion inherently weaker)
_RANGING_DIRECTION_THRESHOLD = 0.15  # Lower threshold than trending (0.30) — ranging signals are milder


def _generate_ranging_signal(
    close: pd.Series,
    current_price: float,
    timestamp: datetime,
    timeframe: str,
    scores: dict[str, float],
    regime: str,
    min_confidence: float,
    learning_mode: bool = False,
) -> Signal:
    """Generate a signal for ranging markets using Bollinger mean-reversion.

    In ranging (ADX < 20), trend-following indicators are unreliable. Instead:
    - Bollinger Bands: price near lower band → BUY (oversold), near upper band → SELL (overbought)
    - Volume: high volume confirms, low volume weakens
    - MACD: mild momentum confirmation (not primary)
    - Confidence is capped at 0.65 (ranging signals are inherently weaker)

    Returns HOLD if no clear mean-reversion opportunity exists.
    """
    boll = calculate_bollinger(close, period=20, std_dev=2.0)
    latest_upper = boll.upper.iloc[-1]
    latest_lower = boll.lower.iloc[-1]
    latest_middle = boll.middle.iloc[-1]

    if pd.isna(latest_upper) or pd.isna(latest_lower) or pd.isna(latest_middle):
        return Signal(
            symbol="XAUUSD", signal_type=SignalType.HOLD, confidence=0.0,
            price=current_price, timestamp=timestamp, timeframe=timeframe,
            indicators=scores,
            reason="ranging: Bollinger data unavailable", regime=regime,
        )

    band_range = latest_upper - latest_lower
    if band_range <= 0:
        return Signal(
            symbol="XAUUSD", signal_type=SignalType.HOLD, confidence=0.0,
            price=current_price, timestamp=timestamp, timeframe=timeframe,
            indicators=scores,
            reason="ranging: Bollinger bands invalid", regime=regime,
        )

    # Position within bands: 0.0 = at lower band, 1.0 = at upper band, 0.5 = middle
    band_position = (current_price - latest_lower) / band_range

    # Direction: near lower band → BUY (expect bounce up), near upper band → SELL (expect revert down)
    if band_position <= (1.0 - _RANGING_BOLL_THRESHOLD):
        direction = "BUY"
        boll_score = 1.0 - band_position  # Closer to lower band = stronger buy signal
    elif band_position >= _RANGING_BOLL_THRESHOLD:
        direction = "SELL"
        boll_score = band_position  # Closer to upper band = stronger sell signal
    else:
        return Signal(
            symbol="XAUUSD", signal_type=SignalType.HOLD, confidence=0.0,
            price=current_price, timestamp=timestamp, timeframe=timeframe,
            indicators=scores,
            reason=f"ranging: price mid-band (pos={band_position:.2f})",
            regime=regime,
        )

    # Bollinger score is the primary signal — scale to [-1, 1]
    boll_signal = boll_score if direction == "SELL" else -boll_score
    boll_signal = (boll_signal - 0.5) * 2  # Normalize: 0.7→0.4, 1.0→1.0

    # MACD: mild confirmation (momentum direction)
    macd_weight = 0.3
    macd_score = scores.get("macd", 0)
    if direction == "SELL":
        macd_score = macd_score if macd_score < 0 else -0.3  # Want MACD bearish for sell
    else:
        macd_score = macd_score if macd_score > 0 else 0.3   # Want MACD bullish for buy

    # Volume: confirmation only — high volume increases confidence, low volume reduces it
    volume_score = scores.get("volume", 0)
    if volume_score > 0:
        vol_bonus = 0.15 if volume_score >= 1.0 else 0.10
    elif volume_score < 0:
        vol_bonus = -0.10
    else:
        vol_bonus = 0.0

    # Composite confidence: Bollinger (0.50) + MACD (0.30) + Volume (0.20)
    raw_confidence = abs(boll_signal) * 0.50 + abs(macd_score) * 0.30 + 0.20
    raw_confidence += vol_bonus
    raw_confidence = max(0.0, min(raw_confidence, _RANGING_CONFIDENCE_CAP))

    # Direction threshold check
    if raw_confidence < _RANGING_DIRECTION_THRESHOLD:
        return Signal(
            symbol="XAUUSD", signal_type=SignalType.HOLD, confidence=0.0,
            price=current_price, timestamp=timestamp, timeframe=timeframe,
            indicators=scores,
            reason=f"ranging: conf={raw_confidence:.2f} < {_RANGING_DIRECTION_THRESHOLD}",
            regime=regime,
        )

    signal_type = SignalType.BUY if direction == "BUY" else SignalType.SELL

    # Learning mode: emit regardless of min_confidence
    if raw_confidence < min_confidence and not learning_mode:
        return Signal(
            symbol="XAUUSD", signal_type=SignalType.HOLD, confidence=0.0,
            price=current_price, timestamp=timestamp, timeframe=timeframe,
            indicators=scores,
            reason=f"ranging: conf={raw_confidence:.2f} < min={min_confidence}",
            regime=regime,
        )
    if raw_confidence < min_confidence and learning_mode:
        reason_extra = f" (learning: ranging conf={raw_confidence:.2f} below {min_confidence})"
    else:
        reason_extra = ""

    return Signal(
        symbol="XAUUSD",
        signal_type=signal_type,
        confidence=raw_confidence,
        price=current_price,
        timestamp=timestamp,
        timeframe=timeframe,
        indicators=scores,
        reason=f"ranging {direction}: boll_pos={band_position:.2f} conf={raw_confidence:.2f}"
               f" | macd={macd_score:+.1f} vol={volume_score:+.1f}{reason_extra} [{regime}]",
        regime=regime,
    )


def compute_trend_alignment(
    effective_trend: str,
    signal_type: "SignalType",
    d1_trend_strength: Optional[float] = None,
    price_momentum_24h: Optional[float] = None,
) -> float:
    """Compute trend alignment multiplier for counter-trend confidence scaling.

    Instead of hard blocking counter-trend signals, scale confidence based on:
    1. Trend strength — strong trend = stronger block, weak trend = weaker block
    2. Price momentum — if price is already moving against the trend, the
       counter-trend signal may be an early trend change detection.

    Returns multiplier 0.0-1.0:
      0.0 = full block (strong trend + price confirming it)
      0.3 = heavy reduction (strong trend + price starting to reverse)
      0.5 = moderate reduction (weak trend + price confirming)
      0.7 = light reduction (weak trend + price reversing)
      1.0 = no reduction (trend not applicable or unknown)

    The multiplier can be used as: confidence *= multiplier
    """
    # Default: no reduction if we don't have strength/momentum data
    if d1_trend_strength is None and price_momentum_24h is None:
        return 1.0

    strength = d1_trend_strength if d1_trend_strength is not None else 0.5
    momentum = price_momentum_24h if price_momentum_24h is not None else 0.0

    # Clamp
    strength = max(0.0, min(1.0, strength))
    momentum = max(-0.05, min(0.05, momentum))

    # Is price moving against the trend?
    if effective_trend == "bullish":
        price_against_trend = momentum < -0.005  # price falling >0.5% in 24h
        price_with_trend = momentum > 0.003     # price rising
    elif effective_trend == "bearish":
        price_against_trend = momentum > 0.005   # price rising >0.5% in 24h
        price_with_trend = momentum < -0.003     # price falling
    else:
        return 1.0

    if price_against_trend:
        if strength > 0.6:
            return 0.3   # Strong trend but price reversing — allow with heavy reduction
        else:
            return 0.7   # Weak trend + price reversing — mostly allow (early detection)

    if price_with_trend:
        if strength > 0.6:
            return 0.0   # Strong trend + price confirming — hard block
        else:
            return 0.5   # Weak trend + price confirming — moderate reduction

    # Price neutral / consolidating
    return 0.3 if strength > 0.6 else 0.6


@strategy(
    name="swing",
    timeframe="H1",
    trading_mode=TradingMode.SWING,
    description="EMA+MACD swing strategy with ADX filter and session confidence",
    risk_defaults={"risk_per_trade": 0.02, "atr_multiplier": 1.5, "risk_reward_ratio": 2.0, "min_confidence": 0.60},
    requires_d1_trend=True,
    min_bars=50,
)
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
    h4_trend: Optional[str] = None,
    d1_trend_strength: Optional[float] = None,
    price_momentum_24h: Optional[float] = None,
    min_confidence: float = MIN_CONFIDENCE,
    strategy_id: str = "",
    learning_mode: bool = False,
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
        h4_trend: H4 trend direction ('bullish', 'bearish', or None). Uses EMA 10/50.
        d1_trend_strength: Normalized D1 trend strength (0=flat, 1=strong).
            EMA50-EMA200 spread as fraction of price.
        price_momentum_24h: 24h price change ratio (e.g. -0.015 = -1.5%).
            Detects trend reversals before EMA crossovers confirm them.

    Returns:
        Signal with type, confidence, and reason.
    """
    if current_price is None:
        current_price = float(close.iloc[-1])

    if timestamp is None:
        timestamp = datetime.now(timezone.utc)

    scores, latest_adx = calculate_indicator_scores(close, high, low, volume)

    # Classify market regime
    boll = calculate_bollinger(close, period=20, std_dev=2.0)
    boll_bw = None
    if pd.notna(boll.upper.iloc[-1]) and pd.notna(boll.middle.iloc[-1]) and boll.middle.iloc[-1] != 0:
        boll_bw = (boll.upper.iloc[-1] - boll.lower.iloc[-1]) / boll.middle.iloc[-1]
    regime = classify_regime(latest_adx, boll_bw)

    # Compute weighted_score for all paths (needed for Signal constructor even in ranging mode)
    weighted_score = calculate_weighted_score(scores)

    # Ranging market (ADX < 20): use Bollinger mean-reversion instead of trend-following
    # Data shows ranging WR 66.7% vs trending 46.2% — mean reversion works in ranges
    if latest_adx < 20:
        signal = _generate_ranging_signal(
            close=close,
            current_price=current_price,
            timestamp=timestamp,
            timeframe=timeframe,
            scores=scores,
            regime=regime,
            min_confidence=min_confidence,
            learning_mode=learning_mode,
        )
        if signal.signal_type == SignalType.HOLD:
            return signal
        signal_type = signal.signal_type
        confidence = signal.confidence
        reason = signal.reason
    else:
        signal_type = score_to_signal_type(weighted_score, learning_mode=learning_mode)
        confidence = calculate_signal_confidence(scores, weighted_score)

        # Build reason string from active indicators
        active_indicators = [f"{k}={v:+.1f}" for k, v in scores.items() if v != 0]
        reason = f"Score={weighted_score:+.2f} | " + ", ".join(active_indicators) if active_indicators else f"Score={weighted_score:+.2f}"

    # Initialize trend/session intermediate values (may be overwritten by trend logic)
    trend_mult: float = 1.0
    h4_override: bool = False
    session_mult: float = 1.0

    # Multi-timeframe trend filter — confidence scaling based on trend strength + momentum
    # Instead of hard blocking counter-trend trades, scale confidence by:
    # 1. How strong the D1 trend is (weak trend → less reduction)
    # 2. Whether price momentum is already reversing (price falling in bullish D1 → early signal)
    #
    # Hard block only when: strong trend + price confirming the trend direction
    # Allow with reduced confidence when: trend weakening OR price starting to reverse
    #
    # H4 override: H4 EMA 10/50 responds ~4x faster than D1 EMA 50/200.
    # When H4 disagrees with D1, trend is considered "weakening" regardless of strength.
    if d1_trend is not None and signal_type != SignalType.HOLD:
        effective_trend = d1_trend
        if h4_trend is not None and h4_trend != "unknown":
            if d1_trend == "bullish" and h4_trend == "bearish":
                effective_trend = "bearish"
            elif d1_trend == "bearish" and h4_trend == "bullish":
                effective_trend = "bullish"

        h4_override = effective_trend != d1_trend

        is_counter_trend = (
            (effective_trend == "bullish" and signal_type == SignalType.SELL)
            or (effective_trend == "bearish" and signal_type == SignalType.BUY)
        )

        if is_counter_trend:
            if learning_mode:
                reason += f" (learning: counter-trend {signal_type.value} in {d1_trend} D1)"
            else:
                # If H4 overrides, trend is already conflicted — reduce block strength
                if h4_override:
                    trend_mult = 0.5  # H4 conflict = trend uncertain
                    reason += f" (H4 override: {d1_trend} D1 → {effective_trend} H4)"
                else:
                    trend_mult = compute_trend_alignment(
                        effective_trend, signal_type,
                        d1_trend_strength, price_momentum_24h,
                    )

                if trend_mult == 0.0:
                    signal_type = SignalType.HOLD
                    reason += f" (counter-trend {signal_type.value} blocked: strong {effective_trend} trend)"
                else:
                    confidence *= trend_mult
                    reason += f" (counter-trend: trend_mult={trend_mult:.1f}, conf={confidence:.2f})"
        elif h4_override:
            reason += f" (H4 override: {d1_trend} D1 → {effective_trend} H4)"

    # Regime label included in signal output (no confidence reduction — volatile regime filter hurts PF)
    reason += f" [{regime}]"

    # Apply session-based confidence multiplier
    session = classify_session(timestamp)
    session_mult = SESSION_CONFIDENCE_MULTIPLIER.get(session, 1.0)
    if session_mult != 1.0:
        confidence *= session_mult
        confidence = min(confidence, 1.0)  # Cap at 1.0

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

    # Confidence filter — in learning mode, emit signal regardless so we can
    # analyze which confidence levels actually produce wins
    if confidence < min_confidence and signal_type != SignalType.HOLD:
        if learning_mode:
            reason += f" (learning: confidence {confidence:.2f} below {min_confidence})"
        else:
            signal_type = SignalType.HOLD
            reason += f" (confidence {confidence:.2f} below {min_confidence})"

    return Signal(
        symbol="XAUUSD",
        signal_type=signal_type,
        confidence=confidence,
        price=current_price,
        timestamp=timestamp,
        timeframe=timeframe,
        indicators=scores,
        reason=reason,
        regime=regime,
        strategy_id=strategy_id,
        weighted_score=weighted_score,
        trend_mult=trend_mult if trend_mult != 1.0 else None,
        h4_override=str(h4_override) if h4_override else None,
        session_mult=session_mult if session_mult != 1.0 else None,
    )