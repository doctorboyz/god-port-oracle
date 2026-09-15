"""Unit tests for broky.ml.ensemble_predictor gating logic.

Tests the gate decisions (or/and/avg/max) using stub predictors so no
real model files are required. The goal is to verify the gating math and
the conservative-failure behavior, not the underlying TradeOutcomePredictor
(that has its own tests).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pytest

from broky.ml.ensemble_predictor import EnsemblePredictor, EnsembleConfig


# ---------------------------------------------------------------------------
# Test stub — mimics TradeOutcomePredictor's public interface
# ---------------------------------------------------------------------------


@dataclass
class StubPredictor:
    """Minimal stand-in for TradeOutcomePredictor.

    Returns a scripted loss_proba for any call. ``enabled`` controls
    whether the member contributes. ``raise_on_call`` simulates a runtime
    error in predict_loss_proba.
    """

    loss_proba: float
    enabled: bool = True
    raise_on_call: bool = False
    model_dir: str = "stub"

    def predict_loss_proba(
        self,
        features: dict[str, float | str],
        regime: Optional[str] = None,
        direction: Optional[str] = None,
    ) -> tuple[Optional[float], Optional[str]]:
        if self.raise_on_call:
            raise RuntimeError("stub raised")
        return self.loss_proba, "stub_model"


def _build_ensemble(probas: list[float], mode: str, threshold: float = 0.50) -> EnsemblePredictor:
    """Build an EnsemblePredictor with stub members returning ``probas``."""
    ep = EnsemblePredictor.__new__(EnsemblePredictor)
    ep.config = EnsembleConfig(
        model_dirs=[f"stub_{i}" for i in range(len(probas))],
        mode=mode,
        loss_threshold=threshold,
    )
    ep.members = [
        (f"stub_{i}", StubPredictor(loss_proba=p, model_dir=f"stub_{i}"))
        for i, p in enumerate(probas)
    ]
    ep.enabled = any(p.enabled for _, p in ep.members)
    return ep


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestEnsembleConfig:
    def test_requires_at_least_two_models(self):
        with pytest.raises(ValueError, match=">=2 models"):
            EnsembleConfig(model_dirs=["only_one"], mode="or", loss_threshold=0.5)

    def test_requires_non_empty(self):
        with pytest.raises(ValueError, match="at least one model dir"):
            EnsembleConfig(model_dirs=[], mode="or", loss_threshold=0.5)

    @pytest.mark.parametrize("bad_mode", ["", "xor", "majority", "OR"])
    def test_rejects_invalid_mode(self, bad_mode):
        with pytest.raises(ValueError, match="mode must be one of"):
            EnsembleConfig(model_dirs=["a", "b"], mode=bad_mode, loss_threshold=0.5)

    @pytest.mark.parametrize("bad_thr", [0.0, 1.0, -0.1, 1.5])
    def test_rejects_threshold_outside_open_interval(self, bad_thr):
        with pytest.raises(ValueError, match="loss_threshold must be in"):
            EnsembleConfig(model_dirs=["a", "b"], mode="or", loss_threshold=bad_thr)

    def test_accepts_valid_config(self):
        cfg = EnsembleConfig(model_dirs=["a", "b", "c"], mode="avg", loss_threshold=0.65)
        assert cfg.mode == "avg"
        assert cfg.loss_threshold == 0.65
        assert len(cfg.model_dirs) == 3


# ---------------------------------------------------------------------------
# OR-gate (the verified B ensemble mode)
# ---------------------------------------------------------------------------


class TestOrGate:
    """OR-gate blocks if ANY member reports P(LOSS) > threshold."""

    def test_blocks_when_any_member_exceeds(self):
        ep = _build_ensemble([0.30, 0.80], mode="or", threshold=0.50)
        skip, reason = ep.should_skip({}, regime="trending", direction="BUY")
        assert skip is True
        assert "0.80" in reason
        assert "or" in reason

    def test_passes_when_all_members_below(self):
        ep = _build_ensemble([0.30, 0.45], mode="or", threshold=0.50)
        skip, _ = ep.should_skip({}, regime="trending", direction="BUY")
        assert skip is False

    def test_passes_at_exact_threshold_not_blocked(self):
        # Strict > — equal threshold does not block
        ep = _build_ensemble([0.50, 0.20], mode="or", threshold=0.50)
        skip, _ = ep.should_skip({}, regime="trending", direction="BUY")
        assert skip is False

    def test_gated_value_is_max(self):
        ep = _build_ensemble([0.30, 0.80, 0.55], mode="or", threshold=0.50)
        proba, label = ep.predict_loss_proba({}, regime="trending", direction="BUY")
        assert proba == pytest.approx(0.80)
        assert label == "ensemble_or(3/3)"

    def test_v4_v6_breakthrough_scenario(self):
        """Reproduces the ISSUE-025 B-ensemble result: V4=0.30, v6=0.70
        with threshold 0.50 → block (the verified B pass scenario @0.50
        blocks trades where EITHER model distrusts)."""
        ep = _build_ensemble([0.30, 0.70], mode="or", threshold=0.50)
        skip, _ = ep.should_skip({}, regime="trending", direction="BUY")
        # v6=0.70 > 0.50 → OR-gate blocks
        assert skip is True


# ---------------------------------------------------------------------------
# AND-gate
# ---------------------------------------------------------------------------


class TestAndGate:
    """AND-gate blocks only if ALL members exceed threshold."""

    def test_passes_when_one_member_below(self):
        ep = _build_ensemble([0.30, 0.80], mode="and", threshold=0.50)
        skip, _ = ep.should_skip({}, regime="trending", direction="BUY")
        assert skip is False

    def test_blocks_when_all_members_exceed(self):
        ep = _build_ensemble([0.70, 0.80], mode="and", threshold=0.50)
        skip, _ = ep.should_skip({}, regime="trending", direction="BUY")
        assert skip is True

    def test_gated_value_is_min(self):
        ep = _build_ensemble([0.70, 0.80, 0.90], mode="and", threshold=0.50)
        proba, label = ep.predict_loss_proba({}, regime="trending", direction="BUY")
        assert proba == pytest.approx(0.70)
        assert label == "ensemble_and(3/3)"


# ---------------------------------------------------------------------------
# AVG-gate
# ---------------------------------------------------------------------------


class TestAvgGate:
    """AVG-gate blocks if mean(P(LOSS)) > threshold."""

    def test_blocks_when_mean_exceeds(self):
        ep = _build_ensemble([0.40, 0.70], mode="avg", threshold=0.50)
        # mean = 0.55 > 0.50 → block
        skip, _ = ep.should_skip({}, regime="trending", direction="BUY")
        assert skip is True

    def test_passes_when_mean_below(self):
        ep = _build_ensemble([0.30, 0.50], mode="avg", threshold=0.50)
        # mean = 0.40 — note 0.50 alone wouldn't block (strict >), mean passes
        skip, _ = ep.should_skip({}, regime="trending", direction="BUY")
        assert skip is False

    def test_gated_value_is_mean(self):
        ep = _build_ensemble([0.20, 0.40, 0.60], mode="avg", threshold=0.50)
        proba, _ = ep.predict_loss_proba({}, regime="trending", direction="BUY")
        assert proba == pytest.approx(0.40)


# ---------------------------------------------------------------------------
# MAX-gate (equivalent to OR with single threshold)
# ---------------------------------------------------------------------------


class TestMaxGate:
    def test_blocks_on_highest_loss(self):
        ep = _build_ensemble([0.10, 0.95, 0.20], mode="max", threshold=0.85)
        skip, _ = ep.should_skip({}, regime="trending", direction="BUY")
        assert skip is True

    def test_passes_when_max_below(self):
        ep = _build_ensemble([0.10, 0.80, 0.20], mode="max", threshold=0.85)
        skip, _ = ep.should_skip({}, regime="trending", direction="BUY")
        assert skip is False


# ---------------------------------------------------------------------------
# Disabled / failure handling (conservative-by-default)
# ---------------------------------------------------------------------------


class TestFailureHandling:
    def test_disabled_ensemble_never_blocks(self):
        ep = _build_ensemble([0.99, 0.99], mode="or", threshold=0.50)
        ep.enabled = False
        skip, reason = ep.should_skip({}, regime="trending", direction="BUY")
        assert skip is False
        assert "disabled" in reason

    def test_all_members_disabled_returns_no_block(self):
        ep = _build_ensemble([0.99, 0.99], mode="or", threshold=0.50)
        for _, p in ep.members:
            p.enabled = False
        skip, reason = ep.should_skip({}, regime="trending", direction="BUY")
        assert skip is False
        assert "no ensemble member" in reason

    def test_member_exception_does_not_crash_ensemble(self):
        """A raising member is treated as no-prediction (None), not a crash."""
        ep = _build_ensemble([0.30, 0.80], mode="or", threshold=0.50)
        # Make first member raise; second member 0.80 should still trigger block
        ep.members[0][1].raise_on_call = True
        skip, _ = ep.should_skip({}, regime="trending", direction="BUY")
        assert skip is True  # second member 0.80 > 0.50 → block

    def test_all_members_raise_returns_no_block(self):
        ep = _build_ensemble([0.30, 0.80], mode="or", threshold=0.50)
        for _, p in ep.members:
            p.raise_on_call = True
        skip, reason = ep.should_skip({}, regime="trending", direction="BUY")
        assert skip is False
        assert "no ensemble member" in reason

    def test_predict_all_reports_per_member_state(self):
        ep = _build_ensemble([0.30, 0.80], mode="or", threshold=0.50)
        ep.members[0][1].raise_on_call = True
        results = ep.predict_all({}, regime="trending", direction="BUY")
        assert len(results) == 2
        assert results[0][1] is None  # raised → None
        assert results[1][1] == 0.80  # OK


# ---------------------------------------------------------------------------
# health_check (mirrors TradeOutcomePredictor.health_check for drop-in use)
# ---------------------------------------------------------------------------


class StubHealthyPredictor(StubPredictor):
    """Stub that passes health_check (default StubPredictor has no health_check)."""

    def health_check(self) -> tuple[bool, str]:
        return True, "stub OK"


class StubUnhealthyPredictor(StubPredictor):
    """Stub that fails health_check."""

    def health_check(self) -> tuple[bool, str]:
        return False, "stub feature mismatch"


class TestHealthCheck:
    def _build_with(self, member_cls, probas: list[float]) -> EnsemblePredictor:
        ep = EnsemblePredictor.__new__(EnsemblePredictor)
        ep.config = EnsembleConfig(
            model_dirs=[f"stub_{i}" for i in range(len(probas))],
            mode="or",
            loss_threshold=0.50,
        )
        ep.members = [
            (f"stub_{i}", member_cls(loss_proba=p, model_dir=f"stub_{i}"))
            for i, p in enumerate(probas)
        ]
        ep.enabled = True
        return ep

    def test_all_members_healthy_returns_ok(self):
        ep = self._build_with(StubHealthyPredictor, [0.30, 0.40])
        ok, reason = ep.health_check()
        assert ok is True
        assert "ensemble or OK" in reason
        assert "2/2" in reason

    def test_any_member_unhealthy_returns_unhealthy(self):
        ep = EnsemblePredictor.__new__(EnsemblePredictor)
        ep.config = EnsembleConfig(
            model_dirs=["stub_a", "stub_b"], mode="or", loss_threshold=0.50
        )
        ep.members = [
            ("stub_a", StubHealthyPredictor(loss_proba=0.30, model_dir="stub_a")),
            ("stub_b", StubUnhealthyPredictor(loss_proba=0.40, model_dir="stub_b")),
        ]
        ep.enabled = True
        ok, reason = ep.health_check()
        assert ok is False
        assert "stub_b" in reason

    def test_no_enabled_members_returns_unhealthy(self):
        ep = self._build_with(StubHealthyPredictor, [0.30, 0.40])
        for _, p in ep.members:
            p.enabled = False
        ep.enabled = False
        ok, reason = ep.health_check()
        assert ok is False
        assert "no ensemble member enabled" in reason


# ---------------------------------------------------------------------------
# Risk multiplier (mirrors TradeOutcomePredictor.get_risk_multiplier shape)
# ---------------------------------------------------------------------------


class TestRiskMultiplier:
    def test_full_size_when_loss_low(self):
        ep = _build_ensemble([0.20, 0.30], mode="or", threshold=0.50)
        # max = 0.30 ≤ 0.50 → full size
        mult, reason, proba, label = ep.get_risk_multiplier(
            {}, regime="trending", direction="BUY"
        )
        assert mult == 1.0
        assert proba == pytest.approx(0.30)
        assert "full size" in reason
        assert label == "ensemble_or(2/2)"

    def test_skip_when_loss_very_high(self):
        ep = _build_ensemble([0.20, 0.95], mode="or", threshold=0.50)
        # max = 0.95 ≥ 0.85 → skip
        mult, reason, proba, _ = ep.get_risk_multiplier(
            {}, regime="trending", direction="BUY"
        )
        assert mult == 0.0
        assert "skip" in reason
        assert proba == pytest.approx(0.95)

    def test_linear_scaling_in_middle(self):
        ep = _build_ensemble([0.20, 0.60], mode="or", threshold=0.50)
        # max = 0.60, scale = (0.85 - 0.60) / (0.85 - 0.50) = 0.25 / 0.35
        mult, _, proba, _ = ep.get_risk_multiplier(
            {}, regime="trending", direction="BUY"
        )
        assert proba == pytest.approx(0.60)
        assert mult == pytest.approx((0.85 - 0.60) / (0.85 - 0.50), rel=1e-6)

    def test_disabled_returns_full_size(self):
        ep = _build_ensemble([0.99, 0.99], mode="or", threshold=0.50)
        ep.enabled = False
        mult, reason, proba, _ = ep.get_risk_multiplier(
            {}, regime="trending", direction="BUY"
        )
        assert mult == 1.0
        assert proba is None
        assert "disabled" in reason


# ---------------------------------------------------------------------------
# predict_loss_proba label format
# ---------------------------------------------------------------------------


class TestLabelFormat:
    def test_label_reports_contributing_and_total(self):
        ep = _build_ensemble([0.30, 0.80, 0.40], mode="or", threshold=0.50)
        _, label = ep.predict_loss_proba({}, regime="trending", direction="BUY")
        assert label == "ensemble_or(3/3)"

    def test_label_counts_only_contributing_members(self):
        ep = _build_ensemble([0.30, 0.80, 0.40], mode="or", threshold=0.50)
        # Disable middle member
        ep.members[1][1].enabled = False
        _, label = ep.predict_loss_proba({}, regime="trending", direction="BUY")
        assert label == "ensemble_or(2/3)"