"""Trade outcome predictor — WIN/LOSS prediction for live trade filtering.

Uses models trained by TradeOutcomeTrainer to predict whether a trade
will be profitable (WIN) or losing (LOSS) based on features at signal time.

Unlike the direction predictor (UP/FLAT/DOWN), this directly optimizes
for what we care about: trade profitability.

Usage:
    predictor = TradeOutcomePredictor(model_dir="models/trade_outcome_v1")
    skip, reason = predictor.should_skip(signal_features, regime, direction)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

DEFAULT_LOSS_THRESHOLD = 0.65
MIN_MODEL_ACCURACY = 0.40


class TradeOutcomePredictor:
    """Load trained trade outcome models and filter trades in real-time.

    Uses regime x direction models for best specificity, with fallback
    to overall model if specific model is unavailable.
    """

    def __init__(
        self,
        model_dir: str | None = None,
        loss_threshold: float = DEFAULT_LOSS_THRESHOLD,
    ):
        import os
        if model_dir is None:
            model_dir = os.environ.get("ML_MODEL_DIR", "data/models/trade_outcome_v2")
        self.model_dir = Path(model_dir)
        self.loss_threshold = loss_threshold
        self._models: dict[str, object] = {}
        self._model_info: dict[str, dict] = {}
        self._feature_cols: list[str] = []
        self._engineer = None  # FeatureEngineer for transforming features
        self.enabled = False
        self._load_results()

    def _load_results(self) -> None:
        results_path = self.model_dir / "training_results.json"
        if not results_path.exists():
            logger.warning("No training results at %s — ML filter disabled", results_path)
            return

        with open(results_path) as f:
            training_results = json.load(f)

        config = training_results.get("config", {})
        categorical_cols = config.get("categorical_cols", ["session", "d1_trend", "h4_trend", "price_vs_cloud", "mfi_signal"])
        self._feature_cols = config.get("feature_cols", [])

        for m in training_results.get("models", []):
            name = m["name"]
            test_acc = m.get("test_accuracy", 0)
            self._model_info[name] = {
                "test_accuracy": test_acc,
                "n_samples": m.get("n_samples", 0),
                "win_rate": m.get("win_rate", 0),
                "feature_cols": m.get("feature_cols", []),
            }

        # Load models from pickle files
        import joblib

        loaded = 0
        for name in self._model_info:
            model_path = self.model_dir / f"{name}_model.pkl"
            if model_path.exists():
                try:
                    self._models[name] = joblib.load(model_path)
                    loaded += 1
                except Exception as e:
                    logger.warning("Failed to load %s: %s", name, e)

        # Load feature engineer for consistent transforms
        import joblib

        engineer_path = self.model_dir / "feature_engineer.joblib"
        if engineer_path.exists():
            try:
                self._engineer = joblib.load(engineer_path)
                logger.info("Loaded feature engineer from %s", engineer_path)
            except Exception as e:
                logger.warning("Failed to load feature engineer: %s", e)

        if loaded > 0:
            self.enabled = True
            logger.info(
                "TradeOutcomePredictor: %d models loaded, loss_threshold=%.0f%%",
                loaded,
                self.loss_threshold * 100,
            )
        else:
            logger.warning("No model files found — ML filter disabled")

    def predict_loss_proba(
        self,
        features: dict[str, float | str],
        regime: Optional[str] = None,
        direction: Optional[str] = None,
    ) -> Optional[float]:
        """Predict probability of LOSS. Returns None if unavailable."""
        if not self.enabled:
            return None

        model_names = []
        if regime and direction:
            model_names.append(f"{regime}_{direction}")
        if direction:
            model_names.append(f"direction_{direction}")
        if regime:
            model_names.append(f"regime_{regime}")
        model_names.append("overall")

        for name in model_names:
            model = self._models.get(name)
            if model is None:
                continue
            info = self._model_info.get(name, {})
            if info.get("test_accuracy", 0) < MIN_MODEL_ACCURACY:
                continue
            try:
                proba = self._predict(model, features, info.get("feature_cols", []))
                if proba is not None:
                    return proba
            except Exception:
                continue

        return None

    def _predict(
        self,
        model: object,
        features: dict[str, float | str],
        feature_cols: list[str],
    ) -> Optional[float]:
        cols = feature_cols if feature_cols else self._feature_cols
        if not cols:
            return None

        # Build DataFrame from raw features
        df = pd.DataFrame([features])

        # Apply FeatureEngineer transforms if available
        if self._engineer is not None:
            try:
                df = self._engineer.transform(df)
                # FeatureEngineer produces correct ordinal/one-hot encodings.
                # Drop original string columns to prevent single-row LabelEncoder
                # from overwriting them with wrong values (always 0 on 1-row fit).
                str_cols = df.select_dtypes(include=["object", "string"]).columns.tolist()
                df = df.drop(columns=str_cols, errors="ignore")
            except Exception:
                pass

        # No FeatureEngineer means we cannot produce correct encodings.
        # Single-row LabelEncoder always produces 0, which mismatches training.
        # Better to disable predictions entirely than to silently produce wrong values.
        if self._engineer is None:
            logger.warning("No FeatureEngineer loaded — cannot produce correct encodings, returning None")
            return None

        # Ensure numeric and fill NaN
        X = df.select_dtypes(include=[np.number]).fillna(0)

        # Match feature columns from training
        available_cols = [c for c in cols if c in X.columns]
        if not available_cols:
            return None
        X = X[available_cols]

        proba = model.predict_proba(X)
        # Training labels: 0=LOSS, 1=WIN from (df["pnl"] > 0).astype(int)
        # Verify class order matches our assumption
        loss_idx = list(model.classes_).index(0) if 0 in model.classes_ else 0
        return float(proba[0][loss_idx])  # P(LOSS)

    def should_skip(
        self,
        features: dict[str, float | str],
        regime: Optional[str] = None,
        direction: Optional[str] = None,
    ) -> tuple[bool, str]:
        """Determine if trade should be skipped based on ML prediction.

        Returns (skip: bool, reason: str).
        """
        if not self.enabled:
            return False, "ML filter disabled"

        proba_loss = self.predict_loss_proba(features, regime, direction)
        if proba_loss is None:
            return False, "no model available"

        if proba_loss > self.loss_threshold:
            return True, f"ML filter: P(LOSS)={proba_loss:.0%} > {self.loss_threshold:.0%}"
        return False, f"ML filter: P(LOSS)={proba_loss:.0%} OK"

    def get_risk_multiplier(
        self,
        features: dict[str, float | str],
        regime: Optional[str] = None,
        direction: Optional[str] = None,
    ) -> tuple[float, str]:
        """Convert P(LOSS) to position size multiplier for risk-scaling.

        Instead of hard blocking trades, reduce position size based on risk:
        - P(LOSS) < 0.50: full size (1.0) — model thinks trade will win
        - P(LOSS) 0.50–0.85: linear scaling from 1.0 down to 0.0
        - P(LOSS) > 0.85: skip trade (0.0) — model very confident of loss

        Returns (multiplier: float 0.0-1.0, reason: str).
        """
        if not self.enabled:
            return 1.0, "ML filter disabled"

        proba_loss = self.predict_loss_proba(features, regime, direction)
        if proba_loss is None:
            return 1.0, "no model available"

        if proba_loss <= 0.50:
            return 1.0, f"ML risk: P(LOSS)={proba_loss:.0%}, full size"
        if proba_loss >= 0.85:
            return 0.0, f"ML risk: P(LOSS)={proba_loss:.0%}, skip"

        multiplier = (0.85 - proba_loss) / 0.35
        return round(multiplier, 2), f"ML risk: P(LOSS)={proba_loss:.0%}, {multiplier:.0%} size"

    def get_model_accuracy(self, name: str) -> float:
        info = self._model_info.get(name, {})
        return info.get("test_accuracy", 0)


def compute_features_from_candles(
    candles: dict[str, pd.DataFrame],
    direction: str,
    spread: float = 0,
    d1_trend: str = "neutral",
    h4_trend: str = "unknown",
    session: str = "unknown",
    sentiment: dict | None = None,
) -> dict[str, float | str]:
    """Compute ML features from candle data for trade outcome prediction.

    Mirrors the features stored in feature_snapshots table, which is what
    the models were trained on via features_json.
    """
    m5 = candles.get("M5")
    if m5 is None or m5.empty:
        return {}

    close = m5["close"].astype(float)
    high = m5["high"].astype(float)
    low = m5["low"].astype(float)
    volume = m5["volume"].astype(float) if "volume" in m5.columns else pd.Series(1, index=close.index)

    features: dict[str, float | str] = {
        "session": session,
        "d1_trend": d1_trend,
        "h4_trend": h4_trend,
    }

    # Price
    price = float(close.iloc[-1])
    features["spread"] = float(spread) if spread is not None else 0.0

    # EMAs
    for period in [9, 21, 50, 200]:
        features[f"ema_{period}"] = float(close.ewm(span=period, adjust=False).mean().iloc[-1])

    # SMAs
    for period in [10, 20, 50]:
        features[f"sma_{period}"] = float(close.rolling(period).mean().iloc[-1])

    # DEMA 21
    ema21 = close.ewm(span=21, adjust=False).mean()
    ema21_ema = ema21.ewm(span=21, adjust=False).mean()
    features["dema_21"] = float(2 * ema21.iloc[-1] - ema21_ema.iloc[-1])

    # TEMA 21
    ema1 = close.ewm(span=21, adjust=False).mean()
    ema2 = ema1.ewm(span=21, adjust=False).mean()
    ema3 = ema2.ewm(span=21, adjust=False).mean()
    features["tema_21"] = float(3 * ema1.iloc[-1] - 3 * ema2.iloc[-1] + ema3.iloc[-1])

    # RSI
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-6)
    features["rsi"] = float(100 - (100 / (1 + rs.iloc[-1]))) if pd.notna(rs.iloc[-1]) else 50.0

    # Stochastics
    low14 = low.rolling(14).min()
    high14 = high.rolling(14).max()
    pct_k = 100 * (close - low14) / (high14 - low14 + 1e-9)
    features["stoch_k"] = float(pct_k.iloc[-1])
    features["stoch_d"] = float(pct_k.rolling(3).mean().iloc[-1])

    # Williams %R
    features["williams_r"] = float(-100 * (high14.iloc[-1] - close.iloc[-1]) / (high14.iloc[-1] - low14.iloc[-1] + 1e-9))

    # CCI
    tp = (high + low + close) / 3
    sma_tp = tp.rolling(20).mean()
    mad = tp.rolling(20).apply(lambda x: float(np.abs(x - x.mean()).mean()), raw=True)
    features["cci"] = float((tp.iloc[-1] - sma_tp.iloc[-1]) / (0.015 * mad.iloc[-1])) if mad.iloc[-1] > 0 else 0

    # ROC
    features["roc"] = float(100 * (close.iloc[-1] / close.iloc[-14] - 1)) if len(close) > 14 else 0

    # DeMarker
    de_max = high.diff().where(high.diff() > 0, 0)
    de_min = (-low.diff()).where(low.diff() < 0, 0)
    dem_sum_max = de_max.rolling(13).sum()
    dem_sum_min = de_min.rolling(13).sum()
    features["demarker"] = float(dem_sum_max.iloc[-1] / (dem_sum_max.iloc[-1] + dem_sum_min.iloc[-1] + 1e-9))

    # MACD
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    features["macd_hist"] = float(macd_line.iloc[-1] - signal_line.iloc[-1])

    # ADX/DI
    tr = pd.DataFrame({"hl": high - low, "hc": abs(high - close.shift(1)), "lc": abs(low - close.shift(1))}).max(axis=1)
    atr14 = tr.rolling(14).mean()
    up = high.diff()
    dn = -low.diff()
    plus_dm = up.where((up > 0) & (up > dn), 0)
    minus_dm = dn.where((dn > 0) & (dn > up), 0)
    plus_di_series = 100 * (plus_dm.rolling(14).mean() / atr14.replace(0, 1e-6))
    minus_di_series = 100 * (minus_dm.rolling(14).mean() / atr14.replace(0, 1e-6))
    dx = 100 * abs(plus_di_series - minus_di_series) / (plus_di_series + minus_di_series + 1e-6)
    features["adx"] = float(dx.rolling(14).mean().iloc[-1]) if pd.notna(dx.rolling(14).mean().iloc[-1]) else 0
    features["plus_di"] = float(plus_di_series.iloc[-1]) if pd.notna(plus_di_series.iloc[-1]) else 0
    features["minus_di"] = float(minus_di_series.iloc[-1]) if pd.notna(minus_di_series.iloc[-1]) else 0

    # ATR
    features["atr"] = float(atr14.iloc[-1]) if pd.notna(atr14.iloc[-1]) else 0
    features["atr_to_price"] = float(atr14.iloc[-1] / price) if features["atr"] > 0 else 0

    # Bollinger Bands
    sma20 = close.rolling(20).mean()
    std20 = close.rolling(20).std()
    bb_range = 4 * std20
    features["boll_pct_b"] = float((close.iloc[-1] - (sma20 - 2 * std20).iloc[-1]) / bb_range.iloc[-1]) if bb_range.iloc[-1] > 0 else 0.5
    features["boll_bw"] = float(bb_range.iloc[-1] / sma20.iloc[-1]) if sma20.iloc[-1] > 0 else 0

    # MFI
    typical = (high + low + close) / 3
    raw_mf = typical * volume
    pos_mf = raw_mf.where(typical > typical.shift(1), 0).rolling(14).sum()
    neg_mf = raw_mf.where(typical < typical.shift(1), 0).rolling(14).sum()
    mfi_series = 100 - (100 / (1 + pos_mf / neg_mf.replace(0, 1e-6)))
    features["mfi"] = float(mfi_series.iloc[-1]) if pd.notna(mfi_series.iloc[-1]) else 50.0

    # OBV
    obv = (np.sign(close.diff()) * volume).cumsum()
    features["obv"] = float(obv.iloc[-1])
    features["obv_slope"] = float((obv.iloc[-1] - obv.iloc[-14]) / 14) if len(obv) > 14 else 0

    # A/D Line
    clv = ((close - low) - (high - close)) / (high - low + 1e-9)
    ad = (clv * volume).cumsum()
    features["ad_line"] = float(ad.iloc[-1])
    features["ad_line_slope"] = float((ad.iloc[-1] - ad.iloc[-14]) / 14) if len(ad) > 14 else 0

    # CMF
    mfv = ((close - low) - (high - close)) / (high - low + 1e-9) * volume
    mfv_sum = mfv.rolling(20).sum()
    vol_sum = volume.rolling(20).sum()
    features["cmf"] = float(mfv_sum.iloc[-1] / vol_sum.iloc[-1]) if vol_sum.iloc[-1] > 0 else 0

    # Volume
    vol_sma20 = volume.rolling(20).mean()
    features["tick_volume_ratio"] = float(volume.iloc[-5:].mean() / vol_sma20.iloc[-1]) if vol_sma20.iloc[-1] > 0 else 1.0
    features["volume_roc"] = float(100 * (volume.iloc[-1] / volume.iloc[-14] - 1)) if len(volume) > 14 else 0

    # Ichimoku
    high9 = high.rolling(9).max()
    low9 = low.rolling(9).min()
    high26 = high.rolling(26).max()
    low26 = low.rolling(26).min()
    high52 = high.rolling(52).max()
    low52 = low.rolling(52).min()
    features["ichimoku_tenkan"] = float((high9 + low9).iloc[-1] / 2)
    features["ichimoku_kijun"] = float((high26 + low26).iloc[-1] / 2)
    features["ichimoku_senkou_a"] = float(((high9 + low9) / 2 + (high26 + low26) / 2).iloc[-1] / 2)
    features["ichimoku_senkou_b"] = float((high52 + low52).iloc[-1] / 2)
    features["ichimoku_chikou"] = float(close.iloc[-26]) if len(close) > 26 else price

    # Price vs Cloud
    if features["ichimoku_senkou_a"] > features["ichimoku_senkou_b"]:
        cloud_top = features["ichimoku_senkou_a"]
        cloud_bottom = features["ichimoku_senkou_b"]
    else:
        cloud_top = features["ichimoku_senkou_b"]
        cloud_bottom = features["ichimoku_senkou_a"]
    if price > cloud_top:
        features["price_vs_cloud"] = "above"
    elif price < cloud_bottom:
        features["price_vs_cloud"] = "below"
    else:
        features["price_vs_cloud"] = "inside"

    # VWAP
    vwap = (typical * volume).cumsum() / volume.cumsum()
    features["vwap_offset_pct"] = float(100 * (price - vwap.iloc[-1]) / vwap.iloc[-1]) if vwap.iloc[-1] > 0 else 0

    # Sentiment (use real values if provided, otherwise neutral defaults)
    _sent = sentiment or {}
    features["fear_greed_value"] = _sent.get("fear_greed_value", 50.0)
    features["gold_bias_strength"] = _sent.get("gold_bias_strength", 50.0)
    features["news_sentiment"] = _sent.get("news_sentiment", 0.0)

    # spread_ratio: must match training formula from SentimentGroup.compute_indicators()
    # Training: (current_high - current_low) / rolling_20_avg(high - low)
    # NOT spread / ATR (that was wrong — different value range)
    if len(high) >= 20 and len(low) >= 20:
        bar_range = float(high.iloc[-1] - low.iloc[-1])
        avg_range = float((high - low).rolling(20).mean().iloc[-1])
        features["spread_ratio"] = bar_range / avg_range if avg_range > 0 else float("nan")
    else:
        features["spread_ratio"] = float("nan")

    # long_short_ratio: always NaN at training time (no broker data available)
    features["long_short_ratio"] = float("nan")

    # session_strength: must match training formula from SentimentGroup._session_strength()
    # Training: 0.2-1.0 based on UTC hour, NOT 50.0
    hour = m5.index[-1].hour if hasattr(m5.index[-1], "hour") else 0
    if 13 <= hour <= 16:
        features["session_strength"] = 1.0   # London/NY overlap
    elif 8 <= hour <= 16:
        features["session_strength"] = 0.7   # London
    elif 13 <= hour <= 22:
        features["session_strength"] = 0.7   # NY
    elif 0 <= hour <= 8:
        features["session_strength"] = 0.4   # Asian
    else:
        features["session_strength"] = 0.2   # Off-hours

    # MFI signal (derived from MFI value computed above)
    mfi_val = features.get("mfi", 50.0)
    features["mfi_signal"] = "oversold" if mfi_val < 20 else ("overbought" if mfi_val > 80 else "neutral")

    # Balance/leverage (not available before trade entry)
    features["balance_at_entry"] = 0.0
    features["leverage_at_entry"] = 0.0

    # Multi-timeframe price context
    # Use float("nan") instead of None so these columns stay numeric dtype
    # and get properly imputed by FeatureEngineer instead of being dropped
    h1 = candles.get("H1")
    h4 = candles.get("H4")
    d1 = candles.get("D1")
    features["h1_close"] = float(h1["close"].iloc[-1]) if h1 is not None and not h1.empty else float("nan")
    features["h4_close"] = float(h4["close"].iloc[-1]) if h4 is not None and not h4.empty else float("nan")
    features["d1_close"] = float(d1["close"].iloc[-1]) if d1 is not None and not d1.empty else float("nan")
    features["m5_high"] = float(m5["high"].iloc[-1]) if m5 is not None and not m5.empty else float("nan")
    features["m5_low"] = float(m5["low"].iloc[-1]) if m5 is not None and not m5.empty else float("nan")

    return features

