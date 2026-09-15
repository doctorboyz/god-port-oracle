"""Ensemble predictor — combines N TradeOutcomePredictor models with a gate.

Wraps multiple ``TradeOutcomePredictor`` instances and aggregates their
``predict_loss_proba`` outputs via one of four gates:

- ``or``  — block if ANY model reports P(LOSS) > threshold  (conservative)
- ``and`` — block only if ALL models report P(LOSS) > threshold
- ``avg`` — block if mean(P(LOSS)) > threshold
- ``max`` — block if max(P(LOSS)) > threshold  (equivalent to ``or`` with
            a single threshold; kept for explicitness)

Designed for the V4+v6 OR-gate ensemble that robustly passes PF>1.5 on
Account B in-sample and out-of-sample (ISSUE-025). NOT deployed to
Account A (IRON LAW) — this module is opt-in per container via env var
``ML_ENSEMBLE_MODE`` and ``ML_ENSEMBLE_THRESH``.

The wrapper mirrors the ``TradeOutcomePredictor`` public interface
(``predict_loss_proba``, ``should_skip``, ``get_risk_multiplier``) so it
can be a drop-in replacement in the live pipeline when Phase 4 of the
forward-test plan ([[ensemble-b-forward-test-plan]]) is approved.

Usage::

    from broky.ml.ensemble_predictor import EnsemblePredictor

    ep = EnsemblePredictor(
        model_dirs=["data/models/trade_outcome_v4", "data/models/trade_outcome_v6"],
        mode="or",
        loss_threshold=0.50,
    )
    skip, reason = ep.should_skip(features, regime="trending", direction="BUY")
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from broky.ml.trade_outcome_predictor import TradeOutcomePredictor


VALID_MODES = ("or", "and", "avg", "max")


@dataclass
class EnsembleConfig:
    """Configuration for an EnsemblePredictor."""

    model_dirs: list[str]
    mode: str = "or"
    loss_threshold: float = 0.50
    # Per-model loss_threshold passed down to each TradeOutcomePredictor.
    # Use 1.0 so each sub-predictor never hard-skips on its own; the
    # ensemble gate is the single source of truth for blocking.
    member_loss_threshold: float = 1.0

    def __post_init__(self) -> None:
        if not self.model_dirs:
            raise ValueError("EnsembleConfig.model_dirs must contain at least one model dir")
        if len(self.model_dirs) < 2:
            raise ValueError(
                "EnsembleConfig.model_dirs needs >=2 models — "
                "use TradeOutcomePredictor directly for single-model filtering"
            )
        if self.mode not in VALID_MODES:
            raise ValueError(f"EnsembleConfig.mode must be one of {VALID_MODES}, got {self.mode!r}")
        if not (0.0 < self.loss_threshold < 1.0):
            raise ValueError(
                f"EnsembleConfig.loss_threshold must be in (0, 1), got {self.loss_threshold}"
            )


class EnsemblePredictor:
    """N-model ensemble predictor with configurable gating.

    Public interface mirrors ``TradeOutcomePredictor`` so this can be a
    drop-in replacement in code paths that currently use a single
    predictor. The key difference: ``predict_loss_proba`` returns the
    gated loss probability (the value used by the gate decision), not a
    single model's probability. Use ``predict_all`` to inspect individual
    member predictions for logging / shadow mode.
    """

    def __init__(
        self,
        model_dirs: Optional[list[str]] = None,
        mode: str = "or",
        loss_threshold: Optional[float] = None,
        config: Optional[EnsembleConfig] = None,
    ) -> None:
        if config is not None:
            self.config = config
        else:
            dirs = model_dirs or _env_model_dirs()
            thr = loss_threshold if loss_threshold is not None else _env_threshold(0.50)
            mode = mode or os.environ.get("ML_ENSEMBLE_MODE", "or")
            self.config = EnsembleConfig(
                model_dirs=dirs,
                mode=mode,
                loss_threshold=thr,
            )

        self.members: list[tuple[str, TradeOutcomePredictor]] = []
        for mdir in self.config.model_dirs:
            p = TradeOutcomePredictor(
                model_dir=mdir,
                loss_threshold=self.config.member_loss_threshold,
            )
            self.members.append((mdir, p))

        # An ensemble is "enabled" if at least one member is enabled.
        self.enabled = any(p.enabled for _, p in self.members)

    # ------------------------------------------------------------------
    # Health (mirrors TradeOutcomePredictor.health_check for drop-in use)
    # ------------------------------------------------------------------

    def health_check(self) -> tuple[bool, str]:
        """Verify every enabled member can produce a prediction.

        Returns (healthy, reason). Healthy only if at least one member is
        enabled AND every enabled member's own health_check passes. A
        single failing member degrades the ensemble — we report unhealthy
        so the caller can decide to disable, but we do not raise.
        """
        enabled_members = [(d, p) for d, p in self.members if p.enabled]
        if not enabled_members:
            return False, "no ensemble member enabled"
        failures: list[str] = []
        for mdir, p in enabled_members:
            ok, reason = p.health_check()
            if not ok:
                failures.append(f"{Path(mdir).name}: {reason}")
        if failures:
            return False, "; ".join(failures)
        names = ", ".join(Path(d).name for d, _ in enabled_members)
        return True, f"ensemble {self.config.mode} OK ({len(enabled_members)}/{len(self.members)}: {names})"

    # ------------------------------------------------------------------
    # Prediction
    # ------------------------------------------------------------------

    def predict_all(
        self,
        features: dict[str, float | str],
        regime: Optional[str] = None,
        direction: Optional[str] = None,
    ) -> list[tuple[str, Optional[float], Optional[str]]]:
        """Query every member. Returns list of (model_dir, proba_loss, model_name).

        Members that fail or are disabled return (model_dir, None, None).
        """
        out: list[tuple[str, Optional[float], Optional[str]]] = []
        for mdir, p in self.members:
            if not p.enabled:
                out.append((mdir, None, None))
                continue
            try:
                lp, name = p.predict_loss_proba(features=features, regime=regime, direction=direction)
            except Exception:
                lp, name = None, None
            out.append((mdir, lp, name))
        return out

    def _gate_loss(self, losses: list[float]) -> tuple[bool, float]:
        """Apply the gate to a list of member loss probabilities.

        Returns (block, gated_value). ``gated_value`` is the scalar used by
        the gate (mean for ``avg``, max for ``max``/``or``, min for
        ``and`` — picked so a single threshold comparison works for every
        mode). ``block`` is True if the gated_value exceeds the threshold.
        """
        thr = self.config.loss_threshold
        mode = self.config.mode
        if mode == "or":
            gated = max(losses)
            return gated > thr, gated
        if mode == "and":
            gated = min(losses)
            return gated > thr, gated
        if mode == "avg":
            gated = sum(losses) / len(losses)
            return gated > thr, gated
        if mode == "max":
            gated = max(losses)
            return gated > thr, gated
        # Unreachable — config validates mode in __post_init__
        raise ValueError(f"unknown mode {mode!r}")

    def predict_loss_proba(
        self,
        features: dict[str, float | str],
        regime: Optional[str] = None,
        direction: Optional[str] = None,
    ) -> tuple[Optional[float], Optional[str]]:
        """Return (gated_loss_proba, ensemble_label).

        The gated_loss_proba is the scalar used by the gate (e.g. max for
        ``or`` mode, mean for ``avg``). The ensemble_label is a string like
        ``"ensemble_or(2/2)"`` indicating mode and how many members
        contributed. Returns (None, None) if no member produced a
        prediction.
        """
        if not self.enabled:
            return None, None

        results = self.predict_all(features, regime=regime, direction=direction)
        losses = [lp for _, lp, _ in results if lp is not None]
        if not losses:
            return None, None

        _, gated = self._gate_loss(losses)
        label = f"ensemble_{self.config.mode}({len(losses)}/{len(self.members)})"
        return float(gated), label

    # ------------------------------------------------------------------
    # Decision API (mirrors TradeOutcomePredictor)
    # ------------------------------------------------------------------

    def should_skip(
        self,
        features: dict[str, float | str],
        regime: Optional[str] = None,
        direction: Optional[str] = None,
    ) -> tuple[bool, str]:
        """Decide whether to skip a trade based on the ensemble gate.

        Returns (skip: bool, reason: str). If any member is disabled or
        fails, the ensemble errs conservative: a missing prediction is
        treated as P(LOSS)=0 (not blocking) for ``or``/``max`` and as
        P(LOSS)=0 for ``and``/``avg`` — i.e. missing data never blocks a
        trade on its own. Override by checking ``predict_all`` if
        strict-mode is needed.
        """
        if not self.enabled:
            return False, "ensemble disabled"

        results = self.predict_all(features, regime=regime, direction=direction)
        losses = [lp for _, lp, _ in results if lp is not None]
        if not losses:
            return False, "no ensemble member produced a prediction"

        block, gated = self._gate_loss(losses)
        if block:
            members_str = ", ".join(
                f"{Path(mdir).name}={lp:.2f}" if lp is not None else f"{Path(mdir).name}=NA"
                for mdir, lp, _ in results
            )
            return True, (
                f"ensemble {self.config.mode} gate: {gated:.2f} > "
                f"{self.config.loss_threshold:.2f} [{members_str}]"
            )
        return False, f"ensemble {self.config.mode} gate: {gated:.2f} OK"

    def get_risk_multiplier(
        self,
        features: dict[str, float | str],
        regime: Optional[str] = None,
        direction: Optional[str] = None,
    ) -> tuple[float, str, Optional[float], Optional[str]]:
        """Convert the gated loss probability into a position size multiplier.

        Same shape as ``TradeOutcomePredictor.get_risk_multiplier``:
        - gated P(LOSS) < 0.50: full size (1.0)
        - gated P(LOSS) 0.50–0.85: linear scaling down to 0.0
        - gated P(LOSS) > 0.85: skip (0.0)

        For ``or``/``max`` modes the gated value is the worst-case loss
        proba, so this is conservative. For ``avg`` it's the mean.
        """
        if not self.enabled:
            return 1.0, "ensemble disabled", None, None

        proba_loss, label = self.predict_loss_proba(features, regime=regime, direction=direction)
        if proba_loss is None:
            return 1.0, "no ensemble member produced a prediction", None, None

        if proba_loss <= 0.50:
            return 1.0, f"ensemble risk: P(LOSS)={proba_loss:.0%}, full size", proba_loss, label
        if proba_loss >= 0.85:
            return 0.0, f"ensemble risk: P(LOSS)={proba_loss:.0%}, skip", proba_loss, label

        # Linear scaling from 1.0 at 0.50 down to 0.0 at 0.85
        mult = (0.85 - proba_loss) / (0.85 - 0.50)
        return float(mult), f"ensemble risk: P(LOSS)={proba_loss:.0%}, scale {mult:.2f}", proba_loss, label


def _env_model_dirs() -> list[str]:
    """Read ML_ENSEMBLE_MODEL_DIRS env var (colon-separated)."""
    raw = os.environ.get("ML_ENSEMBLE_MODEL_DIRS", "")
    dirs = [d.strip() for d in raw.split(":") if d.strip()]
    if not dirs:
        raise ValueError(
            "EnsemblePredictor requires model_dirs argument or "
            "ML_ENSEMBLE_MODEL_DIRS env var (colon-separated paths)"
        )
    return dirs


def _env_threshold(default: float) -> float:
    raw = os.environ.get("ML_ENSEMBLE_THRESH", "")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default