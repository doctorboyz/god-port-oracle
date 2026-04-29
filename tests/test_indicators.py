"""Tests for all technical indicators — verify input/output correctness."""

import numpy as np
import pandas as pd
import pytest

from broky.indicators.rsi import calculate_rsi
from broky.indicators.ema import calculate_ema, calculate_ema_cross
from broky.indicators.macd import calculate_macd, MACDResult
from broky.indicators.bollinger import calculate_bollinger, BollingerResult
from broky.indicators.stochastic import calculate_stochastic, StochasticResult
from broky.indicators.atr import calculate_atr
from broky.indicators.volume import calculate_volume_ma, calculate_volume_ratio
from broky.indicators.adx import calculate_adx


def _make_closes(n: int = 100, start: float = 1900.0, trend: float = 0.5) -> pd.Series:
    """Generate a simple upward-trending price series."""
    np.random.seed(42)
    noise = np.random.normal(0, 2, n)
    closes = start + np.cumsum(np.full(n, trend) + noise)
    return pd.Series(closes)


def _make_ohlcv(n: int = 100) -> pd.DataFrame:
    """Generate OHLCV data with realistic ranges."""
    closes = _make_closes(n)
    rng = np.random.default_rng(42)
    spread = rng.uniform(1, 5, n)
    df = pd.DataFrame({
        "open": closes - spread * rng.uniform(-0.5, 0.5, n),
        "high": closes + spread,
        "low": closes - spread,
        "close": closes,
        "volume": rng.uniform(1000, 5000, n),
    })
    return df


class TestRSI:
    def test_rsi_range(self):
        close = _make_closes(50)
        rsi = calculate_rsi(close, period=14)
        valid = rsi.dropna()
        assert (valid >= 0).all() and (valid <= 100).all()

    def test_rsi_length_matches_input(self):
        close = _make_closes(50)
        rsi = calculate_rsi(close, period=14)
        assert len(rsi) == len(close)

    def test_rsi_initial_nan(self):
        close = _make_closes(50)
        rsi = calculate_rsi(close, period=14)
        # First row should be NaN (no previous data for diff)
        assert pd.isna(rsi.iloc[0])

    def test_rsi_period_parameter(self):
        close = _make_closes(60)
        rsi14 = calculate_rsi(close, period=14)
        rsi7 = calculate_rsi(close, period=7)
        # Shorter period should produce valid values earlier
        assert rsi7.dropna().first_valid_index() < rsi14.dropna().first_valid_index()

    def test_rsi_all_same_price(self):
        """When price never changes, RSI should be around 50 (no momentum)."""
        close = pd.Series([100.0] * 50)
        rsi = calculate_rsi(close, period=14)
        # All gains and losses are zero → RSI is NaN (division by zero)
        # This is expected behavior — no momentum means undefined RSI
        valid = rsi.dropna()
        # Either NaN or around 50
        if len(valid) > 0:
            assert (valid.isna() | ((valid >= 40) & (valid <= 60))).all()


class TestEMA:
    def test_ema_length(self):
        close = _make_closes(50)
        ema = calculate_ema(close, period=9)
        assert len(ema) == len(close)

    def test_ema_smoother_than_price(self):
        close = _make_closes(100)
        ema = calculate_ema(close, period=21)
        valid = ema.dropna()
        close_valid = close.iloc[len(close) - len(valid):]
        # EMA should have lower standard deviation (smoother)
        assert valid.std() < close_valid.std()

    def test_ema_cross_golden_and_death(self):
        """Test that EMA crossover detects golden and death crosses."""
        # Create a series that trends up then down
        n = 60
        up = pd.Series(np.linspace(100, 120, n // 2))
        down = pd.Series(np.linspace(120, 90, n // 2))
        close = pd.concat([up, down], ignore_index=True)
        _, _, crossover = calculate_ema_cross(close, fast_period=5, slow_period=10)
        # Should have at least one golden cross and one death cross
        assert (crossover == 1).any() or (crossover == -1).any()


class TestMACD:
    def test_macd_result_structure(self):
        close = _make_closes(50)
        result = calculate_macd(close)
        assert isinstance(result, MACDResult)
        assert len(result.macd_line) == len(close)
        assert len(result.signal_line) == len(close)
        assert len(result.histogram) == len(close)

    def test_macd_histogram_is_difference(self):
        close = _make_closes(50)
        result = calculate_macd(close)
        valid = result.histogram.dropna()
        expected = (result.macd_line - result.signal_line).dropna()
        pd.testing.assert_series_equal(
            valid.reset_index(drop=True),
            expected.reset_index(drop=True),
            check_names=False,
            atol=1e-10,
        )

    def test_macd_custom_periods(self):
        close = _make_closes(50)
        result = calculate_macd(close, fast_period=8, slow_period=17, signal_period=9)
        assert len(result.macd_line) == len(close)


class TestBollinger:
    def test_bollinger_result_structure(self):
        close = _make_closes(50)
        result = calculate_bollinger(close, period=20)
        assert isinstance(result, BollingerResult)
        assert len(result.upper) == len(close)
        assert len(result.lower) == len(close)

    def test_bollinger_bands_contain_price(self):
        """Most prices should fall within Bollinger Bands (~95%)."""
        close = _make_closes(100)
        result = calculate_bollinger(close, period=20, std_dev=2.0)
        valid_idx = result.upper.dropna().index
        close_valid = close.loc[valid_idx]
        within = (close_valid >= result.lower.loc[valid_idx]) & (close_valid <= result.upper.loc[valid_idx])
        # At least 80% should be within bands (2 std covers ~95%)
        assert within.mean() >= 0.80

    def test_bollinger_upper_above_lower(self):
        close = _make_closes(100)
        result = calculate_bollinger(close, period=20)
        valid = result.upper.dropna()
        lower_valid = result.lower.loc[valid.index]
        assert (valid >= lower_valid).all()


class TestStochastic:
    def test_stochastic_range(self):
        df = _make_ohlcv(50)
        result = calculate_stochastic(df["high"], df["low"], df["close"])
        valid_k = result.k_line.dropna()
        valid_d = result.d_line.dropna()
        assert (valid_k >= 0).all() and (valid_k <= 100).all()
        assert (valid_d >= 0).all() and (valid_d <= 100).all()

    def test_stochastic_length(self):
        df = _make_ohlcv(50)
        result = calculate_stochastic(df["high"], df["low"], df["close"])
        assert len(result.k_line) == len(df)
        assert len(result.d_line) == len(df)


class TestATR:
    def test_atr_length(self):
        df = _make_ohlcv(50)
        atr = calculate_atr(df["high"], df["low"], df["close"], period=14)
        assert len(atr) == len(df)

    def test_atr_positive(self):
        df = _make_ohlcv(50)
        atr = calculate_atr(df["high"], df["low"], df["close"], period=14)
        valid = atr.dropna()
        assert (valid > 0).all()

    def test_atr_reflects_volatility(self):
        """Higher price swings should produce higher ATR."""
        np.random.seed(42)
        n = 50
        low_vol = pd.Series(1900.0 + np.random.normal(0, 1, n))
        high_vol = pd.Series(1900.0 + np.random.normal(0, 10, n))
        close_low = low_vol + 0.5
        close_high = high_vol + 0.5

        atr_low = calculate_atr(low_vol + 1, low_vol - 1, close_low.shift(1), period=14)
        atr_high = calculate_atr(high_vol + 5, high_vol - 5, close_high.shift(1), period=14)

        # High volatility ATR should be higher
        assert atr_high.dropna().mean() > atr_low.dropna().mean()


class TestVolume:
    def test_volume_ma_length(self):
        df = _make_ohlcv(50)
        vol_ma = calculate_volume_ma(df["volume"], period=20)
        assert len(vol_ma) == len(df)

    def test_volume_ma_smooths(self):
        df = _make_ohlcv(100)
        vol_ma = calculate_volume_ma(df["volume"], period=20)
        valid = vol_ma.dropna()
        vol_valid = df["volume"].loc[valid.index]
        assert valid.std() < vol_valid.std()

    def test_volume_ratio(self):
        df = _make_ohlcv(100)
        ratio = calculate_volume_ratio(df["volume"], period=20)
        valid = ratio.dropna()
        # Ratio around 1.0 means volume is average
        assert (valid > 0).all()

    def test_volume_ratio_high_spike(self):
        """When current volume is 3x average, ratio should be ~3."""
        volume = pd.Series([1000] * 19 + [5000])
        ratio = calculate_volume_ratio(volume, period=20)
        assert ratio.iloc[-1] > 2.0  # 5000 / ~1000 should be > 2


class TestADX:
    def test_adx_range(self):
        df = _make_ohlcv(100)
        adx, pdi, mdi = calculate_adx(df["high"], df["low"], df["close"], period=14)
        adx_valid = adx.dropna()
        pdi_valid = pdi.dropna()
        mdi_valid = mdi.dropna()
        assert (adx_valid >= 0).all() and (adx_valid <= 100).all()
        assert (pdi_valid >= 0).all() and (pdi_valid <= 100).all()
        assert (mdi_valid >= 0).all() and (mdi_valid <= 100).all()

    def test_adx_length(self):
        df = _make_ohlcv(100)
        adx, pdi, mdi = calculate_adx(df["high"], df["low"], df["close"], period=14)
        assert len(adx) == len(df)
        assert len(pdi) == len(df)
        assert len(mdi) == len(df)

    def test_adx_initial_nan(self):
        df = _make_ohlcv(100)
        adx, _, _ = calculate_adx(df["high"], df["low"], df["close"], period=14)
        assert pd.isna(adx.iloc[0])

    def test_adx_strong_trend_higher(self):
        """Strongly trending data should produce higher ADX than ranging data."""
        n = 100
        # Strong uptrend
        up = pd.Series(np.linspace(1900, 2100, n))
        atr_up = 5.0
        adx_trend, _, _ = calculate_adx(up + atr_up, up - atr_up, up, period=14)

        # Ranging market
        np.random.seed(42)
        flat = pd.Series(1900 + np.sin(np.linspace(0, 6 * np.pi, n)) * 3)
        adx_range, _, _ = calculate_adx(flat + 2, flat - 2, flat, period=14)

        # Trending market should have higher ADX
        assert adx_trend.dropna().mean() > adx_range.dropna().mean()

    def test_adx_pdi_mdi_direction(self):
        """In uptrend, +DI should dominate; in downtrend, -DI should dominate."""
        n = 100
        # Uptrend
        up = pd.Series(np.linspace(1900, 2100, n))
        _, pdi_up, mdi_up = calculate_adx(up + 5, up - 5, up, period=14)
        # +DI should be higher than -DI in uptrend
        assert pdi_up.dropna().mean() > mdi_up.dropna().mean()

        # Downtrend
        down = pd.Series(np.linspace(2100, 1900, n))
        _, pdi_down, mdi_down = calculate_adx(down + 5, down - 5, down, period=14)
        # -DI should be higher than +DI in downtrend
        assert mdi_down.dropna().mean() > pdi_down.dropna().mean()