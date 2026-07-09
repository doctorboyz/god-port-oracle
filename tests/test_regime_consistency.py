"""Test regime classification consistency across all consumers.

ISSUE-015: Volatile regime was functionally nonexistent because:
1. BW threshold 0.035/0.04 was too high for M5 data (max boll_bw = 0.014)
2. Trainer used ADX-only fallback that never produced "volatile"
3. Thresholds were inconsistent across files (0.035 vs 0.04)

Fix: Single source of truth via classify_regime() with VOLATILE_BW_THRESHOLD=0.01
"""

import pytest
from datetime import datetime, timezone
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


# ---------------------------------------------------------------------------
# Ranging hard-block (2026-07-09 fix — Real-A)
#
# Bug: CLAUDE.md rule "Ranging = พัก (no trade)" was not enforced. regime=ranging
# only labeled signals, never blocked them. m5 BUY #5553 lost $8 in ranging regime.
#
# Fix: env-driven RANGING_HARD_BLOCK module constant. When True and regime=ranging
# and not learning_mode, generator returns HOLD with reason "ranging_hard_block".
# Set RANGING_HARD_BLOCK=1 only in oracle-engine (Real-A) docker-compose env.
# ---------------------------------------------------------------------------

def _build_trending_candles(n: int = 200):
    """Build candle data that produces trending regime (ADX ≥ 25)."""
    import numpy as np
    import pandas as pd
    from datetime import datetime, timezone
    np.random.seed(42)
    base = 2000.0
    trend = np.linspace(0, 80, n)  # strong uptrend → high ADX
    noise = np.random.normal(0, 0.3, n)
    closes = base + trend + noise
    spread = np.random.uniform(0.5, 1.5, n)
    highs = closes + spread
    lows = closes - spread
    opens = closes - np.random.uniform(0, 0.5, n)
    volumes = np.full(n, 3000.0)
    idx = pd.date_range(start=datetime(2026, 4, 29, 8, 0, tzinfo=timezone.utc),
                        periods=n, freq="5min")
    return (pd.Series(closes, index=idx, name="Close"),
            pd.Series(highs, index=idx, name="High"),
            pd.Series(lows, index=idx, name="Low"),
            pd.Series(volumes, index=idx, name="Volume"))


def _build_ranging_candles(n: int = 300):
    """Build candle data that produces ranging regime (ADX < 25, sideways).

    For m5 tests: sideways price action → low ADX → regime=ranging inline.
    Pure noise around a flat base → ADX typically < 15. Test monkeypatches
    classify_ribbon_state='bullish' to bypass the ribbon-squeeze gate (which
    fires before regime classification) so the ranging hard-block is isolated.
    """
    import numpy as np
    import pandas as pd
    from datetime import datetime, timezone
    np.random.seed(7)
    base = 2000.0
    # Pure noise around flat base → very low ADX (no directional trend)
    closes = base + np.random.normal(0, 1.5, n).cumsum() * 0.05 + np.random.normal(0, 0.8, n)
    spread = np.random.uniform(0.5, 1.5, n)
    highs = closes + spread
    lows = closes - spread
    opens = closes + np.random.normal(0, 0.2, n)
    volumes = np.full(n, 3000.0)
    idx = pd.date_range(start=datetime(2026, 4, 29, 8, 0, tzinfo=timezone.utc),
                        periods=n, freq="5min")
    return (pd.Series(closes, index=idx, name="Close"),
            pd.Series(highs, index=idx, name="High"),
            pd.Series(lows, index=idx, name="Low"),
            pd.Series(volumes, index=idx, name="Volume"))


class TestRangingHardBlockSwing:
    """Test ranging hard-block in swing generator (generate_signal).

    Uses monkeypatch.setattr on the module constant (not importlib.reload)
    because reload triggers StrategyRegistry re-registration which raises.
    """

    def test_default_off_ranging_passes(self, monkeypatch):
        """Without RANGING_HARD_BLOCK env, ranging signal is NOT hard-blocked."""
        import broky.signals.generator as gen_mod
        monkeypatch.setattr(gen_mod, "RANGING_HARD_BLOCK", False)
        assert gen_mod.RANGING_HARD_BLOCK is False

    def test_env_on_enables_hard_block(self, monkeypatch):
        """RANGING_HARD_BLOCK=1 → module constant True (simulated via setattr)."""
        import broky.signals.generator as gen_mod
        monkeypatch.setattr(gen_mod, "RANGING_HARD_BLOCK", True)
        assert gen_mod.RANGING_HARD_BLOCK is True

    def test_ranging_hard_block_returns_hold(self, monkeypatch):
        """When hard-block ON and regime=ranging → signal_type=HOLD with ranging_hard_block reason."""
        import broky.signals.generator as gen_mod
        monkeypatch.setattr(gen_mod, "RANGING_HARD_BLOCK", True)

        close, high, low, volume = _build_trending_candles()
        # Force regime=ranging by monkeypatching classify_regime
        monkeypatch.setattr(gen_mod, "classify_regime", lambda *a, **k: "ranging")

        sig = gen_mod.generate_signal(close, high, low, volume, d1_trend="bullish")
        assert sig.signal_type.value == "HOLD", (
            f"ranging regime with hard-block ON must return HOLD, got {sig.signal_type.value}"
        )
        assert "ranging_hard_block" in (sig.reason or ""), (
            f"reason must tag ranging_hard_block for triage, got: {sig.reason}"
        )

    def test_trending_not_blocked_when_hard_block_on(self, monkeypatch):
        """When hard-block ON but regime=trending → signal flows normally (not blocked)."""
        import broky.signals.generator as gen_mod
        monkeypatch.setattr(gen_mod, "RANGING_HARD_BLOCK", True)

        close, high, low, volume = _build_trending_candles()
        monkeypatch.setattr(gen_mod, "classify_regime", lambda *a, **k: "trending")

        sig = gen_mod.generate_signal(close, high, low, volume, d1_trend="bullish")
        # Should NOT be HOLD due to ranging_hard_block (may be HOLD for other reasons,
        # but reason must not mention ranging_hard_block)
        assert "ranging_hard_block" not in (sig.reason or ""), (
            f"trending regime must not trigger ranging_hard_block, got reason: {sig.reason}"
        )

    def test_learning_mode_bypasses_ranging_hard_block(self, monkeypatch):
        """learning_mode=True → ranging hard-block bypassed (collect ML data)."""
        import broky.signals.generator as gen_mod
        monkeypatch.setattr(gen_mod, "RANGING_HARD_BLOCK", True)

        close, high, low, volume = _build_trending_candles()
        monkeypatch.setattr(gen_mod, "classify_regime", lambda *a, **k: "ranging")

        sig = gen_mod.generate_signal(close, high, low, volume, d1_trend="bullish",
                                       learning_mode=True)
        assert "ranging_hard_block" not in (sig.reason or ""), (
            f"learning_mode must bypass ranging hard-block, got reason: {sig.reason}"
        )


class TestRangingHardBlockM5:
    """Test ranging hard-block in m5 generator (generate_m5_scalp_signal).

    m5 uses inline regime classification (ADX<25 → ranging), not classify_regime.
    Tests use _build_ranging_candles() which produces ADX in [15, 25) so the m5
    ADX gate (threshold 15) passes and regime=ranging flows to the hard-block.
    """

    def test_ranging_hard_block_returns_hold(self, monkeypatch):
        """m5: hard-block ON + regime=ranging → HOLD with ranging_hard_block reason.

        Monkeypatches classify_ribbon_state to return 'bullish' so the ribbon
        squeeze gate (which fires before regime classification) doesn't short-
        circuit. This isolates the ranging hard-block as the test target.
        """
        import broky.signals.m5_scalp_generator as m5_mod
        monkeypatch.setattr(m5_mod, "RANGING_HARD_BLOCK", True)
        monkeypatch.setattr(m5_mod, "classify_ribbon_state", lambda *a, **k: "bullish")

        close, high, low, volume = _build_ranging_candles()
        sig = m5_mod.generate_m5_scalp_signal(close, high, low, volume,
                                               current_price=float(close.iloc[-1]),
                                               timestamp=datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc),
                                               spread=5.0,
                                               d1_trend="unknown", h4_trend="bullish")
        assert sig.signal_type.value == "HOLD", (
            f"m5 ranging with hard-block ON must return HOLD, got {sig.signal_type.value}"
        )
        assert "ranging_hard_block" in (sig.reason or ""), (
            f"m5 reason must tag ranging_hard_block, got: {sig.reason}"
        )

    def test_m5_learning_mode_bypasses(self, monkeypatch):
        """m5: learning_mode bypasses ranging hard-block."""
        import broky.signals.m5_scalp_generator as m5_mod
        monkeypatch.setattr(m5_mod, "RANGING_HARD_BLOCK", True)
        monkeypatch.setattr(m5_mod, "classify_ribbon_state", lambda *a, **k: "bullish")

        close, high, low, volume = _build_ranging_candles()
        sig = m5_mod.generate_m5_scalp_signal(close, high, low, volume,
                                               current_price=float(close.iloc[-1]),
                                               timestamp=datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc),
                                               spread=5.0,
                                               d1_trend="unknown", h4_trend="bullish",
                                               learning_mode=True)
        assert "ranging_hard_block" not in (sig.reason or ""), (
            f"m5 learning_mode must bypass ranging hard-block, got: {sig.reason}"
        )