"""Signal Integrator — combine weighted score + ML prediction for final signal."""

from __future__ import annotations

import logging
from typing import Optional

from broky.ml.predictor import MLPredictor

logger = logging.getLogger(__name__)

# Integration boost/penalty values
ML_AGREE_BOOST = 0.15      # ML agrees with weighted score
ML_DISAGREE_PENALTY = 0.20  # ML disagrees with weighted score
ML_FLAT_PENALTY = 0.10     # ML predicts flat (no direction)
ML_MIN_CONFIDENCE = 0.40   # Below this, ML prediction is ignored
MIN_SIGNAL_CONFIDENCE = 0.30  # Below this, signal becomes HOLD


class IntegrationResult:
    """Result of combining weighted score + ML prediction."""

    __slots__ = (
        "original_direction", "original_confidence",
        "ml_direction", "ml_confidence", "ml_probabilities",
        "integrated_direction", "integrated_confidence",
        "agreement", "adjustment",
    )

    def __init__(
        self,
        original_direction: str,
        original_confidence: float,
        ml_direction: str,
        ml_confidence: float,
        ml_probabilities: dict,
        integrated_direction: str,
        integrated_confidence: float,
        agreement: str,
        adjustment: float,
    ):
        self.original_direction = original_direction
        self.original_confidence = original_confidence
        self.ml_direction = ml_direction
        self.ml_confidence = ml_confidence
        self.ml_probabilities = ml_probabilities
        self.integrated_direction = integrated_direction
        self.integrated_confidence = integrated_confidence
        self.agreement = agreement  # "agree", "disagree", "flat", "ignored"
        self.adjustment = adjustment


class SignalIntegrator:
    """Combine weighted score signal with ML prediction.

    The ML model acts as a CONFIRMATION filter, not a replacement:
    - Agree: boost confidence
    - Disagree: reduce confidence
    - ML below threshold: ignored
    - Confidence drops below minimum: signal becomes HOLD
    """

    def __init__(self, predictor: MLPredictor):
        self.predictor = predictor

    def integrate(
        self,
        signal_direction: str,
        signal_confidence: float,
        snapshot: dict,
    ) -> IntegrationResult:
        """Integrate a weighted score signal with ML prediction.

        Args:
            signal_direction: BUY, SELL, or HOLD from weighted score.
            signal_confidence: Confidence from weighted score (0-1).
            snapshot: Feature snapshot dict for ML prediction.

        Returns:
            IntegrationResult with adjusted direction and confidence.
        """
        # Get ML prediction
        ml_result = self.predictor.predict(snapshot)
        ml_direction = ml_result["direction"]
        ml_confidence = ml_result["confidence"]
        ml_probabilities = ml_result["probabilities"]

        # If original signal is HOLD, ML can't help
        if signal_direction == "HOLD":
            return IntegrationResult(
                original_direction=signal_direction,
                original_confidence=signal_confidence,
                ml_direction=ml_direction,
                ml_confidence=ml_confidence,
                ml_probabilities=ml_probabilities,
                integrated_direction="HOLD",
                integrated_confidence=signal_confidence,
                agreement="hold_original",
                adjustment=0.0,
            )

        # If ML confidence is too low, ignore it
        if ml_confidence < ML_MIN_CONFIDENCE:
            return IntegrationResult(
                original_direction=signal_direction,
                original_confidence=signal_confidence,
                ml_direction=ml_direction,
                ml_confidence=ml_confidence,
                ml_probabilities=ml_probabilities,
                integrated_direction=signal_direction,
                integrated_confidence=signal_confidence,
                agreement="ignored",
                adjustment=0.0,
            )

        # Map BUY/SELL to UP/DOWN for comparison
        signal_trend = "UP" if signal_direction == "BUY" else "DOWN"

        # Determine agreement
        if ml_direction == signal_trend:
            # ML agrees with signal
            adjustment = ML_AGREE_BOOST
            agreement = "agree"
        elif ml_direction == "FLAT":
            # ML predicts flat — mild penalty
            adjustment = -ML_FLAT_PENALTY
            agreement = "flat"
        else:
            # ML disagrees with signal
            adjustment = -ML_DISAGREE_PENALTY
            agreement = "disagree"

        # Apply adjustment
        new_confidence = signal_confidence + adjustment
        new_confidence = max(0.0, min(1.0, new_confidence))

        # If confidence drops below minimum, signal becomes HOLD
        if new_confidence < MIN_SIGNAL_CONFIDENCE:
            integrated_direction = "HOLD"
        else:
            integrated_direction = signal_direction

        return IntegrationResult(
            original_direction=signal_direction,
            original_confidence=signal_confidence,
            ml_direction=ml_direction,
            ml_confidence=ml_confidence,
            ml_probabilities=ml_probabilities,
            integrated_direction=integrated_direction,
            integrated_confidence=new_confidence,
            agreement=agreement,
            adjustment=adjustment,
        )