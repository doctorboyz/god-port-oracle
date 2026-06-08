"""Test regime classification consistency across all consumers.

ISSUE-015: Volatile regime was functionally nonexistent because:
1. BW threshold 0.035/0.04 was too high for M5 data (max boll_bw = 0.014)
2. Trainer used ADX-only fallback that never produced "volatile"
3. Thresholds were inconsistent across files (0.035 vs 0.04)

Fix: Single source of truth via classify_regime() with VOLATILE_BW_THRESHOLD=0.01
"""

import pytest
from broky.signals.generator import classify_regime, VOLATILE_BW_THRESHOLD, TRENDING_ADX_THRESHOLD, RANGING_ADX_THRESHOLD


class TestClassifyRegime:
    """Test classify_regime() — single source of truth for regime classification."""

    def test_trending_high_adx_normal_bw(self):
        """High ADX with normal BW → trending."""
        assert classify_regime(30, 0.005) == "trending"

    def test_volatile_high_adx_wide_bw(self):
        """High ADX with wide BW → volatile (threshold now appropriate for M5)."""
        assert classify_regime(30, VOLATILE_BW_THRESHOLD + 0.005) == "volatile"

    def test_volatile_at_threshold_excluded(self):
        """BW exactly at threshold → NOT volatile (uses >, not >=)."""
        assert classify_regime(30, VOLATILE_BW_THRESHOLD) == "trending"

    def test_volatile_just_above_threshold(self):
        """BW just above threshold → volatile."""
        assert classify_regime(30, VOLATILE_BW_THRESHOLD + 0.0001) == "volatile"

    def test_ranging_moderate_adx(self):
        """ADX 20-25 → ranging (trend forming)."""
        assert classify_regime(22, None) == "ranging"

    def test_ranging_low_adx(self):
        """ADX below 20 → ranging (no trend)."""
        assert classify_regime(15, None) == "ranging"

    def test_trending_at_25_without_bw(self):
        """ADX 25 without BW → trending (can't check volatility)."""
        assert classify_regime(25, None) == "trending"

    def test_trending_at_25_with_normal_bw(self):
        """ADX 25 with normal BW → trending."""
        assert classify_regime(25, 0.005) == "trending"

    def test_volatile_at_25_with_wide_bw(self):
        """ADX exactly 25 with wide BW → volatile."""
        assert classify_regime(25, 0.02) == "volatile"

    def test_no_bw_high_adx_trending(self):
        """No BW data → defaults to trending when ADX high."""
        assert classify_regime(30, None) == "trending"

    def test_zero_bw_trending(self):
        """Zero BW → trending (0 is not > threshold)."""
        assert classify_regime(30, 0.0) == "trending"

    def test_threshold_is_appropriate_for_m5(self):
        """VOLATILE_BW_THRESHOLD should be appropriate for M5 XAUUSD data.

        M5 XAUUSD boll_bw typically ranges 0.002-0.014.
        A threshold of 0.01 captures top ~10% of high-ADX periods.
        The old threshold (0.035) was impossible to reach on M5.
        """
        assert VOLATILE_BW_THRESHOLD <= 0.015, (
            f"BW threshold {VOLATILE_BW_THRESHOLD} is too high for M5 data "
            f"(max observed boll_bw is ~0.014)"
        )
        assert VOLATILE_BW_THRESHOLD >= 0.005, (
            f"BW threshold {VOLATILE_BW_THRESHOLD} is too low — "
            f"would classify too many periods as volatile"
        )


class TestRegimeConsistency:
    """Test that all consumers use classify_regime() consistently."""

    def test_predictor_uses_classify_regime(self):
        """Predictor should import and use classify_regime, not inline logic."""
        from broky.ml import trade_outcome_predictor
        # Verify the module imports classify_regime
        import inspect
        source = inspect.getsource(trade_outcome_predictor)
        assert "classify_regime" in source, "predictor should use classify_regime()"
        # Should NOT have hardcoded thresholds
        assert "> 0.04" not in source, "predictor should not hardcode BW threshold"
        assert "> 0.035" not in source, "predictor should not hardcode BW threshold"

    def test_trainer_uses_classify_regime(self):
        """Trainer should import and use classify_regime, not ADX-only fallback."""
        from broky.ml import trade_outcome_trainer
        import inspect
        source = inspect.getsource(trade_outcome_trainer)
        assert "classify_regime" in source, "trainer should use classify_regime()"
        # Should NOT have ADX-only regime fallback
        assert '"trending" if pd.notna(v) and v > 25 else "ranging"' not in source, \
            "trainer should not use ADX-only regime fallback"

    def test_synth_pipeline_uses_classify_regime(self):
        """Synth pipeline should import and use classify_regime."""
        from broky.backtest import synth_pipeline
        import inspect
        source = inspect.getsource(synth_pipeline)
        assert "classify_regime" in source, "synth_pipeline should use classify_regime()"
        # Should NOT have hardcoded thresholds
        assert "> 0.035" not in source, "synth_pipeline should not hardcode BW threshold"

    def test_scalp_generator_uses_classify_regime(self):
        """Scalp generator should import and use classify_regime."""
        from broky.signals import scalp_generator
        import inspect
        source = inspect.getsource(scalp_generator)
        assert "classify_regime" in source, "scalp_generator should use classify_regime()"
        # Should NOT have hardcoded thresholds
        assert "> 0.04" not in source, "scalp_generator should not hardcode BW threshold"


class TestRegimeEncoding:
    """Test that regime encoding is consistent and meaningful."""

    def test_regime_encoded_no_negative_values(self):
        """regime_encoded should not use -1 for volatile (semantically misleading).

        The old encoding volatile=-1 implied volatile < ranging, which is wrong.
        Now volatile=2, which is at least not misleading.
        """
        from broky.ml.features import FeatureEngineer
        import pandas as pd

        fe = FeatureEngineer(fillna=False)
        df = pd.DataFrame({
            "regime": ["trending", "ranging", "volatile", "unknown"],
            "adx": [30, 15, 35, 10],
            "boll_pct_b": [0.5, 0.3, 0.8, 0.2],
            "boll_bw": [0.01, 0.005, 0.02, 0.003],
            "ema_9": [2000, 2000, 2000, 2000],
            "ema_21": [1990, 1990, 1990, 1990],
            "plus_di": [25, 15, 30, 10],
            "minus_di": [20, 20, 10, 20],
        })
        result = fe.transform(df)

        # Check encoding values
        regime_vals = result["regime_encoded"].unique()
        assert -1 not in regime_vals, (
            "regime_encoded should not contain -1 (old volatile encoding). "
            f"Got values: {regime_vals}"
        )
        # trending=1, ranging=0, volatile=2
        assert 1 in regime_vals, "trending should encode to 1"
        assert 0 in regime_vals, "ranging should encode to 0"

    def test_regime_encoding_values(self):
        """Verify exact encoding: trending=1, ranging=0, volatile=2."""
        from broky.ml.features import FeatureEngineer
        import pandas as pd

        fe = FeatureEngineer(fillna=False)
        df = pd.DataFrame({
            "regime": ["trending", "ranging", "volatile"],
            "adx": [30, 15, 35],
            "boll_pct_b": [0.5, 0.3, 0.8],
            "boll_bw": [0.01, 0.005, 0.02],
            "ema_9": [2000, 2000, 2000],
            "ema_21": [1990, 1990, 1990],
            "plus_di": [25, 15, 30],
            "minus_di": [20, 20, 10],
        })
        result = fe.transform(df)

        assert result.loc[0, "regime_encoded"] == 1, "trending should encode to 1"
        assert result.loc[1, "regime_encoded"] == 0, "ranging should encode to 0"
        assert result.loc[2, "regime_encoded"] == 2, "volatile should encode to 2"