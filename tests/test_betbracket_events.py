"""Bracket event library tests — causality (no lookahead) + exact fires.

Every detector must fire only on bars whose information is fully known
at that bar's close, and must fire on the specific bars a hand-built
series says it should.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from broky.betbracket import events as ev


def bars_from_closes(closes, spread=1.0):
    """OHLC frame where high/low straddle each close deterministically."""
    close = pd.Series(closes, dtype=float)
    high = close + spread
    low = close - spread
    return pd.DataFrame({
        "open": close.shift(1).fillna(close.iloc[0]),
        "high": high, "low": low, "close": close,
    })


def test_boll_extreme_fires_at_band_edges():
    # dead-flat stretch (band collapses, pos undefined) then one spike bar
    closes = [100.0] * 40 + [108.0]
    df = bars_from_closes(closes)
    fires = ev.event_fire_indices(ev.EVENT_BOLL_EXTREME, df)
    assert fires == [40]                       # the spike bar, nothing else


def test_rsi_reversal_fires_on_cross_not_inside():
    # falling series drives RSI low; one up bar crosses 30 upward
    closes = [100.0] + [100.0 - 0.6 * i for i in range(1, 30)]
    closes.append(closes[-1] + 3.0)
    df = bars_from_closes(closes)
    fires = ev.event_fire_indices(ev.EVENT_RSI_REVERSAL, df)
    assert fires == [len(closes) - 1]


def test_macd_cross_fires_on_sign_change():
    # flat then rally then dump — MACD crosses signal somewhere in each turn
    closes = [100.0] * 40 + [100.0 + 0.8 * i for i in range(1, 20)]
    closes += [closes[-1] - 0.9 * i for i in range(1, 20)]
    df = bars_from_closes(closes)
    fires = ev.event_fire_indices(ev.EVENT_MACD_CROSS, df)
    assert len(fires) >= 2
    # each fire bar must be a genuine sign change of macd-signal
    macd = ev.calculate_macd(df["close"], 12, 26, 9)
    spread = macd.macd_line - macd.signal_line
    for i in fires:
        assert (spread.iloc[i - 1] <= 0 < spread.iloc[i]) or \
               (spread.iloc[i - 1] >= 0 > spread.iloc[i])


def test_ema_cross_fires_on_sign_change():
    closes = [100.0] * 60 + [100.0 + 1.5 * i for i in range(1, 15)]
    closes += [closes[-1] - 2.0 * i for i in range(1, 15)]
    df = bars_from_closes(closes)
    fires = ev.event_fire_indices(ev.EVENT_EMA_CROSS, df)
    fast = df["close"].ewm(span=10, adjust=False).mean()
    slow = df["close"].ewm(span=50, adjust=False).mean()
    for i in fires:
        assert (fast.iloc[i - 1] <= slow.iloc[i - 1]) != \
               (fast.iloc[i] <= slow.iloc[i])


def test_stoch_reversal_fires_on_20_80_cross():
    # grind down (slow %K deep below 20), then one strong up bar
    closes = [100.0] + [100.0 - 0.4 * i for i in range(1, 30)]
    closes.append(closes[-1] + 6.0)
    df = bars_from_closes(closes, spread=0.5)
    fires = ev.event_fire_indices(ev.EVENT_STOCH_REVERSAL, df)
    assert fires[-1] == len(closes) - 1
    stoch = ev.calculate_stochastic(df["high"], df["low"], df["close"], 14, 3)
    k = stoch.k_line
    for i in fires:
        crossed_up = k.iloc[i - 1] < 20 <= k.iloc[i]
        crossed_down = k.iloc[i - 1] > 80 >= k.iloc[i]
        assert crossed_up or crossed_down


def test_breakout20_matches_farm_entries():
    rng = np.random.default_rng(7)
    closes = 100 + np.cumsum(rng.normal(0, 1.5, 200))
    df = bars_from_closes(closes.tolist(), spread=0.8)
    fires = ev.event_fire_indices(ev.EVENT_BREAKOUT20, df)
    from broky.bet.entries import breakout_signal_series
    sig = breakout_signal_series(df["close"], df["high"], df["low"], lookback=20)
    expected = [int(i) for i in sig.index[sig.isin(["BUY", "SELL"])]]
    assert fires == expected


@pytest.mark.parametrize("event_type", ev.EVENT_TYPES)
def test_no_lookahead_truncation(event_type):
    """Fires up to bar k are identical whether or not the future exists.

    Compute the fire set on the full series, then on the series
    truncated right after the first fire: the fire must be identical —
    detectors may only consume bars up to and including the fire bar.
    """
    rng = np.random.default_rng(3)
    closes = 100 + np.cumsum(rng.normal(0, 2.0, 300))
    df = bars_from_closes(closes.tolist(), spread=1.2)
    full = ev.event_fire_indices(event_type, df)
    assert full, f"{event_type} never fired on the random walk — detector broken"
    k = full[0]
    truncated = ev.event_fire_indices(event_type, df.iloc[: k + 1])
    assert truncated == full[:1]


def test_all_event_indices_covers_grid():
    rng = np.random.default_rng(11)
    closes = 100 + np.cumsum(rng.normal(0, 2.0, 400))
    df = bars_from_closes(closes.tolist(), spread=1.5)
    grid = ev.all_event_indices(df)
    assert set(grid) == set(ev.EVENT_TYPES)
    assert all(isinstance(v, list) for v in grid.values())
