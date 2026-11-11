"""Event library for the theory-free bracket test.

Every detector marks the M5 bar where an "interesting point" happened
(reversal / extreme touch / cross). NO direction is ever attached to a
fire: the bracket opens BUY and SELL ledgers at every event regardless
of what the indicator seems to suggest — theory-free by construction.

All detectors consume only bars up to and including the fire bar
(shifted windows, NaN-gated) — no lookahead.
"""
from __future__ import annotations

import pandas as pd

from broky.indicators.bollinger import calculate_bollinger
from broky.indicators.macd import calculate_macd
from broky.indicators.rsi import calculate_rsi
from broky.indicators.stochastic import calculate_stochastic

EVENT_BOLL_EXTREME = "boll_extreme"
EVENT_RSI_REVERSAL = "rsi_reversal"
EVENT_MACD_CROSS = "macd_cross"
EVENT_EMA_CROSS = "ema_cross"
EVENT_STOCH_REVERSAL = "stoch_reversal"
EVENT_BREAKOUT20 = "breakout20"

EVENT_TYPES = (
    EVENT_BOLL_EXTREME,
    EVENT_RSI_REVERSAL,
    EVENT_MACD_CROSS,
    EVENT_EMA_CROSS,
    EVENT_STOCH_REVERSAL,
    EVENT_BREAKOUT20,
)

BOLL_PERIOD = 20
BOLL_STD = 2.0
BOLL_LOW_POS = 0.15
BOLL_HIGH_POS = 0.85
RSI_PERIOD = 14
RSI_LOW, RSI_HIGH = 30.0, 70.0
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
EMA_FAST, EMA_SLOW = 10, 50
STOCH_K, STOCH_D = 14, 3
STOCH_LOW, STOCH_HIGH = 20.0, 80.0
BREAKOUT_LOOKBACK = 20


def boll_extreme_series(close: pd.Series) -> pd.Series:
    """Band-position extreme: pos ≤ 0.15 (polarity BUY) / ≥ 0.85 (SELL).

    Polarity is ONLY a reporting tag (which side the indicator seems to
    hint at) — the bracket still fires BOTH ledgers at every event.
    """
    boll = calculate_bollinger(close, period=BOLL_PERIOD, std_dev=BOLL_STD)
    band_range = (boll.upper - boll.lower).replace(0, pd.NA)
    pos = (close - boll.lower) / band_range
    out = pd.Series("", index=close.index, dtype=object)
    out[pos.notna() & (pos <= BOLL_LOW_POS)] = "BUY"
    out[pos.notna() & (pos >= BOLL_HIGH_POS)] = "SELL"
    return out


def rsi_reversal_series(close: pd.Series) -> pd.Series:
    """RSI(14) crossing 30 upward (BUY) / 70 downward (SELL)."""
    rsi = calculate_rsi(close, period=RSI_PERIOD)
    prev, cur = rsi.shift(1), rsi
    out = pd.Series("", index=close.index, dtype=object)
    prev_lo = prev.notna() & cur.notna() & (prev < RSI_LOW) & (cur >= RSI_LOW)
    prev_hi = prev.notna() & cur.notna() & (prev > RSI_HIGH) & (cur <= RSI_HIGH)
    out[prev_lo] = "BUY"
    out[prev_hi] = "SELL"
    return out


def macd_cross_series(close: pd.Series) -> pd.Series:
    """MACD(12,26,9) macd/signal cross: up = BUY, down = SELL."""
    macd = calculate_macd(close, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
    spread = macd.macd_line - macd.signal_line
    prev = spread.shift(1)
    out = pd.Series("", index=close.index, dtype=object)
    up = prev.notna() & spread.notna() & (prev <= 0) & (spread > 0)
    down = prev.notna() & spread.notna() & (prev >= 0) & (spread < 0)
    out[up] = "BUY"
    out[down] = "SELL"
    return out


def ema_cross_series(close: pd.Series) -> pd.Series:
    """EMA(10)/EMA(50) cross: up = BUY, down = SELL."""
    fast = close.ewm(span=EMA_FAST, adjust=False).mean()
    slow = close.ewm(span=EMA_SLOW, adjust=False).mean()
    spread = fast - slow
    prev = spread.shift(1)
    out = pd.Series("", index=close.index, dtype=object)
    up = prev.notna() & spread.notna() & (prev <= 0) & (spread > 0)
    down = prev.notna() & spread.notna() & (prev >= 0) & (spread < 0)
    out[up] = "BUY"
    out[down] = "SELL"
    return out


def stoch_reversal_series(high: pd.Series, low: pd.Series,
                          close: pd.Series) -> pd.Series:
    """Slow %K(14,3) crossing 20 upward (BUY) / 80 downward (SELL)."""
    stoch = calculate_stochastic(high, low, close, k_period=STOCH_K,
                                 d_period=STOCH_D)
    k = stoch.k_line
    prev, cur = k.shift(1), k
    out = pd.Series("", index=close.index, dtype=object)
    prev_lo = prev.notna() & cur.notna() & (prev < STOCH_LOW) & (cur >= STOCH_LOW)
    prev_hi = prev.notna() & cur.notna() & (prev > STOCH_HIGH) & (cur <= STOCH_HIGH)
    out[prev_lo] = "BUY"
    out[prev_hi] = "SELL"
    return out


def breakout20_series(close: pd.Series, high: pd.Series,
                      low: pd.Series) -> pd.Series:
    """Breakout of the prior 20-bar range — reuse the bet entries family B."""
    from broky.bet.entries import breakout_signal_series

    return breakout_signal_series(close, high, low, lookback=BREAKOUT_LOOKBACK)


_DETECTORS = {
    EVENT_BOLL_EXTREME: lambda df: boll_extreme_series(df["close"]),
    EVENT_RSI_REVERSAL: lambda df: rsi_reversal_series(df["close"]),
    EVENT_MACD_CROSS: lambda df: macd_cross_series(df["close"]),
    EVENT_EMA_CROSS: lambda df: ema_cross_series(df["close"]),
    EVENT_STOCH_REVERSAL: lambda df: stoch_reversal_series(
        df["high"], df["low"], df["close"]),
    EVENT_BREAKOUT20: lambda df: breakout20_series(
        df["close"], df["high"], df["low"]),
}


def event_polarity_series(event_type: str, bars: pd.DataFrame) -> pd.Series:
    """Per-bar polarity (''/'BUY'/'SELL') for `event_type`.

    The polarity is the direction the indicator HINTS at — used only to
    split a ledger's events into CONT (bet with the hint) and REV (against
    it). The bracket itself always fires both sides; no theory enters the
    resolution.
    """
    if event_type not in _DETECTORS:
        raise ValueError(f"unknown event type {event_type!r}")
    series = _DETECTORS[event_type](bars)
    return series.fillna("")


def event_fire_indices(event_type: str, bars: pd.DataFrame) -> list[int]:
    """Positions (iloc indices) of bars where `event_type` fires.

    `bars` must be OHLC with columns open/high/low/close, ascending time.
    """
    polarity = event_polarity_series(event_type, bars)
    mask = (polarity == "BUY") | (polarity == "SELL")
    return [int(i) for i in polarity.index[mask.to_numpy()]]


def all_event_indices(bars: pd.DataFrame) -> dict[str, list[int]]:
    """Fire indices for every event type — the full bracket grid input."""
    return {ev: event_fire_indices(ev, bars) for ev in EVENT_TYPES}
