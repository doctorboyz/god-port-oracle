"""Trade outcome trainer — train ML models on actual trade results.

Uses live_trades (win/loss) as labels instead of forward-looking price labels.
Supports regime-specific and direction-specific models based on feature importance analysis.

Key insight: The feature importance analysis showed:
- BUY and SELL need different models (SELL WR=35% vs BUY WR=49%)
- Trending and ranging need different features (ichimoku vs mfi/volume)
- Top consensus features: ichimoku cloud, ema_50, sma_50, ema_200, dema_21, sma_10
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.model_selection import StratifiedKFold, TimeSeriesSplit, cross_val_score
from sklearn.preprocessing import LabelEncoder

from broky.ml.features import FeatureEngineer

logger = logging.getLogger(__name__)

# Consensus top features from feature importance analysis (2026-05-13)
# These appeared in top 15 across ALL 3 methods: RF, GB, correlation
CONSENSUS_FEATURES = [
    "ichimoku_senkou_b", "ichimoku_senkou_a", "ema_50", "sma_50",
    "ema_200", "dema_21", "sma_10",
]

# Extended feature set: consensus + direction/regime-specific important features
EXTENDED_FEATURES = CONSENSUS_FEATURES + [
    # Volume/flow (important for ranging regime)
    "mfi", "tick_volume_ratio", "obv_slope", "ad_line_slope", "cmf",
    # Momentum (important for direction classification)
    "macd_hist", "adx", "plus_di", "minus_di",
    # Volatility (important for SL/TP sizing)
    "atr", "atr_to_price", "boll_pct_b", "boll_bw",
    # Sentiment (correlates with PnL)
    "fear_greed_value", "gold_bias_strength",
    # Short-term reference
    "sma_20", "ema_21", "rsi", "cci",
    # Multi-timeframe price context
    "h1_close", "h4_close", "d1_close", "m5_high", "m5_low",
]

# Categorical features that need encoding
CATEGORICAL_COLS = ["session", "d1_trend", "h4_trend", "price_vs_cloud", "mfi_signal"]

# Direction-specific feature sets from XGBoost importance analysis (2026-05-21)
# BUY relies more on ichimoku cloud + volatility, SELL on DI + money flow
BUY_TOP_FEATURES = [
    "dema_21", "atr", "ema_200", "ichimoku_senkou_b", "boll_bw",
    "fear_greed_value", "atr_to_price", "ichimoku_senkou_a", "sma_10",
    "ema_9_21_diff", "macd_hist", "ema_21", "adx", "ad_line_slope", "sma_20",
]
SELL_TOP_FEATURES = [
    "dema_21", "session_strength", "price_vs_cloud", "sma_10",
    "atr_to_price", "ema_200", "plus_di", "atr", "macd_hist",
    "fear_greed_value", "minus_di", "ema_50", "sma_20", "mfi",
    "tick_volume_ratio",
]

# All features used for training
ALL_FEATURE_COLS = EXTENDED_FEATURES + CATEGORICAL_COLS


@dataclass
class TradeOutcomeConfig:
    """Configuration for trade outcome training."""

    experiment_name: str = "trade_outcome_v1"
    description: str = ""

    # Feature selection: "consensus" (7 features), "extended" (30+), or "all"
    feature_set: str = "extended"

    # Confidence threshold: only train on trades with confidence >= this
    min_confidence: float = 0.45

    # Minimum samples per regime/direction split
    min_samples: int = 100

    # Train separate models per regime/direction?
    regime_specific: bool = True
    direction_specific: bool = True

    # Model type: "rf" (Random Forest) or "gb" (Gradient Boosting)
    model_type: str = "gb"

    # Cross-validation folds
    cv_folds: int = 5

    # Test ratio
    test_ratio: float = 0.2

    # XGBoost hyperparameters
    xgb_max_depth: int = 3
    xgb_min_child_weight: int = 5
    xgb_subsample: float = 0.8
    xgb_colsample_bytree: float = 0.8
    xgb_reg_lambda: float = 1.0
    xgb_learning_rate: float = 0.05
    xgb_scale_pos_weight: bool = True
    xgb_n_estimators: int = 200

    # RandomForest hyperparameters
    rf_n_estimators: int = 200
    rf_max_depth: int = 8
    rf_min_samples_leaf: int = 5

    # Sample weight: live trades get this multiplier vs synthetic
    live_weight: float = 3.0

    # Paths (relative to working dir; use "data/models" for Docker volume persistence)
    model_dir: str = "data/models"

    # Exclude phantom trades (execution artifacts)
    exclude_phantom: bool = True

    # Exclude low-confidence trades (noise)
    exclude_low_confidence: bool = True

    def get_feature_cols(self, direction: Optional[str] = None) -> list[str]:
        if self.feature_set == "consensus":
            return CONSENSUS_FEATURES + CATEGORICAL_COLS
        elif self.feature_set == "direction_specific" and direction:
            if direction.upper() == "BUY":
                return list(BUY_TOP_FEATURES)
            else:
                return list(SELL_TOP_FEATURES)
        elif self.feature_set == "extended":
            return ALL_FEATURE_COLS
        else:
            return ALL_FEATURE_COLS

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}


@dataclass
class ModelResult:
    """Result from training a single model."""

    name: str
    model_type: str
    feature_cols: list[str]
    n_samples: int
    n_train: int
    n_test: int
    cv_accuracy: float
    cv_std: float
    test_accuracy: float
    win_rate: float
    profit_factor: float
    feature_importance: dict[str, float]
    model_path: str = ""
    config: dict = field(default_factory=dict)


class TradeOutcomeTrainer:
    """Train ML models on actual trade outcomes (win/loss labels).

    Instead of predicting future price direction (UP/FLAT/DOWN),
    this trainer predicts whether a trade will be profitable (WIN/LOSS)
    based on the features at entry time.

    This directly optimizes for what we care about: trade profitability.
    """

    def __init__(self, config: Optional[TradeOutcomeConfig] = None):
        self.config = config or TradeOutcomeConfig()
        self._last_engineer: Optional[FeatureEngineer] = None

    def load_data(self, db_path: Optional[Path] = None) -> pd.DataFrame:
        """Load trade_outcomes with features_json expanded into columns.

        Uses the trade_outcomes table (backfilled from live_trades + signals + feature_snapshots).

        Returns DataFrame with one row per trade, features + outcome label.
        """
        from metty.core.db import get_connection

        conn = get_connection(db_path)

        # Load trade_outcomes with explicit columns (avoid duplicate cols from JOIN)
        # LEFT JOIN allows synthetic trades (negative trade_id) to be included
        query = """
            SELECT
                to_.id AS outcome_id,
                to_.trade_id,
                to_.direction AS to_direction,
                to_.trading_mode,
                to_.strategy_id,
                to_.outcome_label,
                to_.profit,
                to_.profit_pct,
                to_.exit_reason,
                to_.features_json,
                to_.account_id,
                lt.confidence,
                lt.regime,
                lt.pnl,
                lt.pnl_pct AS lt_pnl_pct,
                lt.direction AS lt_direction
            FROM trade_outcomes to_
            LEFT JOIN live_trades lt ON lt.id = to_.trade_id
            WHERE to_.features_json IS NOT NULL
              AND to_.outcome_label != 'BREAKEVEN'
        """
        params: list = []

        if self.config.exclude_phantom:
            query += " AND (lt.exit_reason IS NULL OR lt.exit_reason != 'phantom')"

        query += " ORDER BY to_.created_at ASC"

        df = pd.read_sql(query, conn, params=params)
        conn.close()

        if df.empty:
            logger.warning("No trade_outcome rows found — run backfill_trade_outcomes() first")
            return df

        # Expand features_json into columns
        features_df = pd.json_normalize(df["features_json"].apply(json.loads))

        # Add metadata columns (use to_direction as fallback for synthetic trades
        # where live_trades JOIN returns NULL)
        # CRITICAL: use to_.profit (available for ALL trades including synthetic)
        # NOT lt.pnl (NULL for synthetic trades → labels all synthetic as LOSS)
        features_df["pnl"] = df["profit"].values
        features_df["pnl_pct"] = df["profit_pct"].values
        features_df["confidence"] = df["confidence"].fillna(0.5).values
        # Regime: prefer live_trades, fallback to deriving from ADX in features
        regime_lt = df["regime"]
        if "adx" in features_df.columns:
            adx_derived = features_df["adx"].apply(
                lambda v: "trending" if pd.notna(v) and v > 25 else "ranging"
            )
            features_df["regime"] = regime_lt.fillna(adx_derived).values
        else:
            features_df["regime"] = regime_lt.fillna("unknown").values
        features_df["direction"] = df["lt_direction"].fillna(df["to_direction"]).values
        features_df["trading_mode"] = df["trading_mode"].values
        features_df["is_open"] = 0
        features_df["account_id"] = df["account_id"].values
        features_df["exit_reason"] = df["exit_reason"].values
        features_df["is_synthetic"] = df["strategy_id"].apply(
            lambda s: 1 if s and "premium_backfill" in str(s) else 0
        ).values

        logger.info("Loaded %d trade-outcome rows from trade_outcomes table", len(features_df))
        return features_df

    def prepare_features(
        self, df: pd.DataFrame, feature_cols: list[str]
    ) -> tuple[pd.DataFrame, pd.Series, object, list[str], np.ndarray]:
        """Engineer features and create binary labels (1=WIN, 0=LOSS).

        Returns (X, y, engineer, feature_names, sample_weights).
        """
        # Extract sample weights BEFORE feature engineering (row count must match)
        if "is_synthetic" in df.columns:
            is_syn = df["is_synthetic"].values
            sample_weights = np.where(is_syn == 1, 1.0, self.config.live_weight)
        else:
            sample_weights = np.ones(len(df))

        engineer = FeatureEngineer(fillna=True)
        engineer.fit(df)
        df_transformed = engineer.transform(df)

        # Get actual feature columns available after engineering
        available = [c for c in feature_cols if c in df_transformed.columns]

        # Add derived features from FeatureEngineer
        derived = [c for c in df_transformed.columns
                   if c in ("ema_9_21_diff", "di_diff", "boll_pct_b_clipped",
                             "price_vs_cloud_encoded", "d1_trend_encoded",
                             "h4_trend_encoded", "mfi_signal_encoded")
                   or c.startswith("session_")]
        available += derived
        available = list(dict.fromkeys(available))  # deduplicate preserving order

        # Remove raw categorical string columns that have encoded replacements
        # to avoid train/serve skew (predictor drops strings, uses encodings)
        raw_cats_with_encodings = {
            "session": [c for c in available if c.startswith("session_")],
            "d1_trend": ["d1_trend_encoded"],
            "h4_trend": ["h4_trend_encoded"],
            "price_vs_cloud": ["price_vs_cloud_encoded"],
            "mfi_signal": ["mfi_signal_encoded"],
        }
        for raw_col, encoded_cols in raw_cats_with_encodings.items():
            if raw_col in available and any(e in available for e in encoded_cols):
                available.remove(raw_col)

        X = df_transformed[available].copy()
        y = (df["pnl"] > 0).astype(int)

        # Encode any remaining string columns (fallback for unencoded categoricals)
        label_encoders: dict[str, LabelEncoder] = {}
        for col in X.select_dtypes(include=["object", "string"]).columns:
            le = LabelEncoder()
            X[col] = le.fit_transform(X[col].astype(str))
            label_encoders[col] = le

        # Fill NaN with median for numeric columns
        for col in X.select_dtypes(include=[np.number]).columns:
            X[col] = X[col].fillna(X[col].median())

        # Drop any columns that still aren't numeric
        X = X.select_dtypes(include=[np.number])

        self._last_engineer = engineer
        return X, y, engineer, list(X.columns), sample_weights

    def train_single(
        self,
        X: pd.DataFrame,
        y: pd.Series,
        name: str,
        sample_weights: Optional[np.ndarray] = None,
    ) -> ModelResult:
        """Train a single model and evaluate with CV + holdout."""
        from sklearn.metrics import accuracy_score

        # Time-based split (no look-ahead)
        split_idx = int(len(X) * (1 - self.config.test_ratio))
        X_train, X_test = X.iloc[:split_idx], X.iloc[split_idx:]
        y_train, y_test = y.iloc[:split_idx], y.iloc[split_idx:]

        # Split sample weights if provided
        train_weights = None
        if sample_weights is not None:
            train_weights = sample_weights[:split_idx]

        if len(X_train) < self.config.min_samples:
            logger.warning("Too few samples for %s: %d (need %d)",
                           name, len(X_train), self.config.min_samples)
            return ModelResult(
                name=name, model_type=self.config.model_type,
                feature_cols=list(X.columns), n_samples=len(X),
                n_train=len(X_train), n_test=len(X_test),
                cv_accuracy=0, cv_std=0, test_accuracy=0,
                win_rate=y.mean(), profit_factor=0,
                feature_importance={},
            )

        if y_train.nunique() < 2:
            logger.warning("Skipping %s: y_train has only 1 class", name)
            return ModelResult(
                name=name, model_type=self.config.model_type,
                feature_cols=list(X.columns), n_samples=len(X),
                n_train=len(X_train), n_test=len(X_test),
                cv_accuracy=0, cv_std=0, test_accuracy=0,
                win_rate=y.mean(), profit_factor=0,
                feature_importance={},
            )

        # Select model
        if self.config.model_type == "rf":
            model = RandomForestClassifier(
                n_estimators=self.config.rf_n_estimators,
                max_depth=self.config.rf_max_depth,
                min_samples_leaf=self.config.rf_min_samples_leaf,
                random_state=42, n_jobs=-1,
            )
        elif self.config.model_type == "xgb":
            from xgboost import XGBClassifier
            xgb_kwargs: dict = dict(
                n_estimators=self.config.xgb_n_estimators,
                max_depth=self.config.xgb_max_depth,
                min_child_weight=self.config.xgb_min_child_weight,
                subsample=self.config.xgb_subsample,
                colsample_bytree=self.config.xgb_colsample_bytree,
                learning_rate=self.config.xgb_learning_rate,
                random_state=42, n_jobs=-1, eval_metric="logloss",
                reg_lambda=self.config.xgb_reg_lambda,
            )
            if self.config.xgb_scale_pos_weight:
                n_pos = int(y_train.sum())
                n_neg = len(y_train) - n_pos
                xgb_kwargs["scale_pos_weight"] = n_neg / max(n_pos, 1)
            model = XGBClassifier(**xgb_kwargs)
        else:
            model = GradientBoostingClassifier(
                n_estimators=200, max_depth=5, random_state=42,
            )

        # Cross-validation — prefer TimeSeriesSplit to avoid look-ahead
        n_splits = min(self.config.cv_folds, 5)
        try:
            tscv = TimeSeriesSplit(n_splits=n_splits)
            cv_scores = cross_val_score(model, X_train, y_train, cv=tscv, scoring="accuracy")
        except ValueError:
            # Fallback to stratified if too few samples per class
            cv = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=42)
            cv_scores = cross_val_score(model, X_train, y_train, cv=cv, scoring="accuracy")

        # Train on full training set (with sample weights if available)
        fit_kwargs: dict = {}
        if train_weights is not None:
            fit_kwargs["sample_weight"] = train_weights
        model.fit(X_train, y_train, **fit_kwargs)

        # Save model to disk for live prediction
        self._save_model(model, name)

        # Test evaluation
        y_pred = model.predict(X_test)
        test_acc = accuracy_score(y_test, y_pred)

        # Profit factor simulation
        pf = self._profit_factor(y_test, y_pred)

        # Feature importance (access base estimator if calibrated)
        base_estimator = model.estimator if hasattr(model, "estimator") else model
        if hasattr(base_estimator, "feature_importances_"):
            importance = dict(zip(X.columns, base_estimator.feature_importances_))
        else:
            importance = {}
        top_importance = dict(sorted(
            importance.items(), key=lambda x: x[1], reverse=True,
        )[:20])

        return ModelResult(
            name=name,
            model_type=self.config.model_type,
            feature_cols=list(X.columns),
            n_samples=len(X),
            n_train=len(X_train),
            n_test=len(X_test),
            cv_accuracy=float(cv_scores.mean()),
            cv_std=float(cv_scores.std()),
            test_accuracy=float(test_acc),
            win_rate=float(y.mean()),
            profit_factor=pf,
            feature_importance=top_importance,
        )

    def train(self, db_path: Optional[Path] = None) -> list[ModelResult]:
        """Train models: overall + regime/direction-specific.

        Returns list of ModelResult for each trained model.
        """
        df = self.load_data(db_path)
        if len(df) < 50:
            raise ValueError(f"Not enough data: {len(df)} rows (need 50+)")

        feature_cols = self.config.get_feature_cols()
        X, y, engineer, available_cols, sample_weights = self.prepare_features(df, feature_cols)

        results: list[ModelResult] = []

        # 1. Overall model
        logger.info("Training overall model on %d samples...", len(X))
        overall = self.train_single(X, y, "overall", sample_weights=sample_weights)
        overall.config = self.config.to_dict()
        results.append(overall)

        # 2. Regime-specific models
        if self.config.regime_specific and "regime" in df.columns:
            for regime in df["regime"].unique():
                mask = df["regime"] == regime
                if mask.sum() < self.config.min_samples:
                    logger.info("Skipping regime=%s: only %d samples", regime, mask.sum())
                    continue
                X_sub = X[mask].reset_index(drop=True)
                y_sub = y[mask].reset_index(drop=True)
                w_sub = sample_weights[mask.values] if sample_weights is not None else None
                logger.info("Training regime=%s model on %d samples...", regime, len(X_sub))
                result = self.train_single(X_sub, y_sub, f"regime_{regime}", sample_weights=w_sub)
                results.append(result)

        # 3. Direction-specific models (with direction-specific features)
        if self.config.direction_specific and "direction" in df.columns:
            for direction in df["direction"].unique():
                mask = df["direction"] == direction
                if mask.sum() < self.config.min_samples:
                    logger.info("Skipping direction=%s: only %d samples", direction, mask.sum())
                    continue
                X_sub = X[mask].reset_index(drop=True)
                y_sub = y[mask].reset_index(drop=True)
                w_sub = sample_weights[mask.values] if sample_weights is not None else None
                # Filter to direction-specific features
                dir_cols = self.config.get_feature_cols(direction=direction)
                dir_cols_avail = [c for c in dir_cols if c in X_sub.columns]
                if dir_cols_avail:
                    X_sub = X_sub[dir_cols_avail]
                logger.info("Training direction=%s model on %d samples, %d features...",
                            direction, len(X_sub), len(X_sub.columns))
                result = self.train_single(X_sub, y_sub, f"direction_{direction}", sample_weights=w_sub)
                results.append(result)

        # 4. Regime x Direction models (with direction-specific features)
        if self.config.regime_specific and self.config.direction_specific:
            if "regime" in df.columns and "direction" in df.columns:
                for regime in df["regime"].unique():
                    for direction in df["direction"].unique():
                        mask = (df["regime"] == regime) & (df["direction"] == direction)
                        if mask.sum() < self.config.min_samples:
                            continue
                        X_sub = X[mask].reset_index(drop=True)
                        y_sub = y[mask].reset_index(drop=True)
                        w_sub = sample_weights[mask.values] if sample_weights is not None else None
                        # Filter to direction-specific features
                        dir_cols = self.config.get_feature_cols(direction=direction)
                        dir_cols_avail = [c for c in dir_cols if c in X_sub.columns]
                        if dir_cols_avail:
                            X_sub = X_sub[dir_cols_avail]
                        logger.info("Training %s_%s model on %d samples, %d features...",
                                    regime, direction, len(X_sub), len(X_sub.columns))
                        result = self.train_single(
                            X_sub, y_sub, f"{regime}_{direction}", sample_weights=w_sub,
                        )
                        results.append(result)

        # Save results summary
        self._save_results(results, db_path)

        return results

    def _save_model(self, model: object, name: str) -> None:
        """Save trained model to disk for live prediction."""
        import joblib

        model_dir = Path(self.config.model_dir) / self.config.experiment_name
        model_dir.mkdir(parents=True, exist_ok=True)
        model_path = model_dir / f"{name}_model.pkl"
        joblib.dump(model, model_path)

    def _profit_factor(self, y_true: pd.Series, y_pred: np.ndarray) -> float:
        """Calculate profit factor from predictions.

        PF = total wins / total losses (in prediction context).
        """
        wins = int(((y_pred == 1) & (y_true == 1)).sum())
        false_wins = int(((y_pred == 1) & (y_true == 0)).sum())
        missed_wins = int(((y_pred == 0) & (y_true == 1)).sum())
        correct_losses = int(((y_pred == 0) & (y_true == 0)).sum())

        gross_profit = max(wins, 1)  # Each win = 1 unit
        gross_loss = max(false_wins, 1)  # Each false win = 1 unit loss

        return round(gross_profit / gross_loss, 2)

    def _save_results(self, results: list[ModelResult], db_path: Optional[Path] = None) -> None:
        """Save training results and feature engineer to disk."""
        import joblib

        model_dir = Path(self.config.model_dir) / self.config.experiment_name
        model_dir.mkdir(parents=True, exist_ok=True)

        # Save feature engineer for consistent transforms during prediction
        engineer_path = model_dir / "feature_engineer.joblib"
        joblib.dump(self._last_engineer, engineer_path)

        summary = {
            "experiment": self.config.experiment_name,
            "config": self.config.to_dict(),
            "categorical_cols": CATEGORICAL_COLS,
            "timestamp": pd.Timestamp.now().isoformat(),
            "models": [],
        }

        for r in results:
            summary["models"].append({
                "name": r.name,
                "model_type": r.model_type,
                "n_samples": r.n_samples,
                "n_train": r.n_train,
                "n_test": r.n_test,
                "cv_accuracy": round(r.cv_accuracy, 4),
                "cv_std": round(r.cv_std, 4),
                "test_accuracy": round(r.test_accuracy, 4),
                "win_rate": round(r.win_rate, 4),
                "profit_factor": r.profit_factor,
                "feature_importance": {
                    k: float(round(v, 4)) for k, v in list(r.feature_importance.items())[:15]
                },
                "feature_cols": r.feature_cols,
            })

        output_path = model_dir / "training_results.json"
        output_path.write_text(json.dumps(summary, indent=2))
        logger.info("Results saved to %s", output_path)


def main():
    """CLI entry point for trade outcome training."""
    import argparse

    parser = argparse.ArgumentParser(description="Train trade outcome models")
    parser.add_argument("--experiment", default="trade_outcome_v1")
    parser.add_argument("--feature-set", default="extended", choices=["consensus", "extended", "all", "direction_specific"])
    parser.add_argument("--min-confidence", type=float, default=0.45)
    parser.add_argument("--min-samples", type=int, default=100)
    parser.add_argument("--model-type", default="gb", choices=["rf", "gb", "xgb"])
    parser.add_argument("--regime-specific", action="store_true", default=True)
    parser.add_argument("--direction-specific", action="store_true", default=True)
    parser.add_argument("--no-regime", action="store_true")
    parser.add_argument("--no-direction", action="store_true")
    parser.add_argument("--db-path", type=str, default=None)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--xgb-max-depth", type=int, default=3)
    parser.add_argument("--xgb-min-child-weight", type=int, default=5)
    parser.add_argument("--xgb-subsample", type=float, default=0.8)
    parser.add_argument("--xgb-colsample-bytree", type=float, default=0.8)
    parser.add_argument("--xgb-reg-lambda", type=float, default=1.0)
    parser.add_argument("--xgb-learning-rate", type=float, default=0.05)
    parser.add_argument("--no-scale-pos-weight", action="store_true")
    parser.add_argument("--xgb-n-estimators", type=int, default=200)
    parser.add_argument("--rf-n-estimators", type=int, default=200)
    parser.add_argument("--rf-max-depth", type=int, default=8)
    parser.add_argument("--rf-min-samples-leaf", type=int, default=5)
    parser.add_argument("--live-weight", type=float, default=3.0)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = TradeOutcomeConfig(
        experiment_name=args.experiment,
        feature_set=args.feature_set,
        min_confidence=args.min_confidence,
        min_samples=args.min_samples,
        model_type=args.model_type,
        regime_specific=not args.no_regime,
        direction_specific=not args.no_direction,
        xgb_max_depth=args.xgb_max_depth,
        xgb_min_child_weight=args.xgb_min_child_weight,
        xgb_subsample=args.xgb_subsample,
        xgb_colsample_bytree=args.xgb_colsample_bytree,
        xgb_reg_lambda=args.xgb_reg_lambda,
        xgb_learning_rate=args.xgb_learning_rate,
        xgb_scale_pos_weight=not args.no_scale_pos_weight,
        xgb_n_estimators=args.xgb_n_estimators,
        rf_n_estimators=args.rf_n_estimators,
        rf_max_depth=args.rf_max_depth,
        rf_min_samples_leaf=args.rf_min_samples_leaf,
        live_weight=args.live_weight,
    )

    trainer = TradeOutcomeTrainer(config)
    db_path = Path(args.db_path) if args.db_path else None
    results = trainer.train(db_path=db_path)

    print("\n" + "=" * 60)
    print("TRADE OUTCOME TRAINING RESULTS")
    print("=" * 60)

    for r in results:
        print(f"\n  {r.name}:")
        print(f"    Samples: {r.n_samples} (train={r.n_train}, test={r.n_test})")
        print(f"    CV Accuracy: {r.cv_accuracy:.1%} +/- {r.cv_std:.1%}")
        print(f"    Test Accuracy: {r.test_accuracy:.1%}")
        print(f"    Win Rate: {r.win_rate:.1%}")
        print(f"    Profit Factor: {r.profit_factor:.2f}")
        if r.feature_importance:
            top3 = list(r.feature_importance.items())[:3]
            print(f"    Top features: {', '.join(f'{k}({v:.3f})' for k, v in top3)}")

    print(f"\nTotal models trained: {len(results)}")


if __name__ == "__main__":
    main()