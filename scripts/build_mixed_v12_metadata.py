#!/usr/bin/env python3
"""Build training_results.json for trade_outcome_mixed_v12.

The mixed_v12 directory was assembled by copying V11 SELL models + V12 BUY models
but the metadata file (training_results.json) was never created. Without it,
TradeOutcomePredictor silently disables the ML filter (see broky/ml/trade_outcome_predictor.py
line 58-60), so B/C/D have been trading unfiltered.

This script merges:
  - V11 training_results.json (overall, regime_*, direction_SELL, *_SELL)
  - V12 BUY training_results.json (direction_BUY, *_BUY)

For shared model names (overall, regime_trending/ranging/volatile) we prefer V11
metrics because V11's PF values (2.5-3.0) are realistic while V12 BUY's (PF=857)
are classification-PF artifacts from scale_pos_weight on imbalanced data.

The .pkl files in mixed_v12/ are already in place from the original copy.
This script ONLY writes the missing training_results.json metadata.

Usage:
    python scripts/build_mixed_v12_metadata.py
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
V11_RESULTS = PROJECT_ROOT / "data" / "models" / "trade_outcome_v11" / "training_results.json"
V12_BUY_RESULTS = PROJECT_ROOT / "data" / "models" / "trade_outcome_v12_buy" / "trade_outcome_v12_buy" / "training_results.json"
MIXED_V12_DIR = PROJECT_ROOT / "data" / "models" / "trade_outcome_mixed_v12"
OUTPUT = MIXED_V12_DIR / "training_results.json"


def _normalize_v11_model(m: dict) -> dict:
    """Normalize V11 model entry to predictor's expected schema.

    V11 uses keys: accuracy_050, pf_050, n_train, n_test, win_rate, test_accuracy,
    top_features, name, optuna_params, feature_cols.
    Predictor reads: name, test_accuracy, n_samples, win_rate, feature_cols.
    """
    return {
        "name": m["name"],
        "test_accuracy": m.get("test_accuracy", 0),
        "n_samples": m.get("n_train", 0) + m.get("n_test", 0),
        "n_train": m.get("n_train", 0),
        "n_test": m.get("n_test", 0),
        "win_rate": m.get("win_rate", 0),
        "profit_factor": m.get("pf_065", m.get("pf_050", 0)),  # PF @ 0.65 confidence
        "feature_cols": m.get("feature_cols", []),
        "top_features": m.get("top_features", {}),
        "source": "v11",
    }


def _normalize_v12_buy_model(m: dict) -> dict:
    """Normalize V12 BUY model entry to predictor's expected schema.

    V12 BUY uses standard trainer schema: name, test_accuracy, win_rate,
    profit_factor, n_samples, feature_cols, feature_importance.
    """
    return {
        "name": m["name"],
        "test_accuracy": m.get("test_accuracy", 0),
        "n_samples": m.get("n_samples", 0),
        "n_train": m.get("n_train", 0),
        "n_test": m.get("n_test", 0),
        "win_rate": m.get("win_rate", 0),
        "profit_factor": m.get("profit_factor", 0),
        "feature_cols": m.get("feature_cols", []),
        "feature_importance": m.get("feature_importance", {}),
        "source": "v12_buy",
    }


def main() -> None:
    if not V11_RESULTS.exists():
        raise SystemExit(f"V11 training_results not found: {V11_RESULTS}")
    if not V12_BUY_RESULTS.exists():
        raise SystemExit(f"V12 BUY training_results not found: {V12_BUY_RESULTS}")
    if not MIXED_V12_DIR.exists():
        raise SystemExit(f"mixed_v12 directory not found: {MIXED_V12_DIR}")

    with open(V11_RESULTS) as f:
        v11 = json.load(f)
    with open(V12_BUY_RESULTS) as f:
        v12_buy = json.load(f)

    v11_models = {m["name"]: _normalize_v11_model(m) for m in v11.get("models", [])}
    v12_buy_models = {m["name"]: _normalize_v12_buy_model(m) for m in v12_buy.get("models", [])}

    # Merge: SELL models + overall + regime_* from V11; BUY models from V12 BUY
    merged: dict[str, dict] = {}

    # Shared models (overall, regime_trending, regime_ranging, regime_volatile) — prefer V11
    # because V11 PF is realistic (2.5-3.0) while V12 BUY PF is overfit artifact (857)
    for name in ("overall", "regime_trending", "regime_ranging", "regime_volatile"):
        if name in v11_models:
            merged[name] = v11_models[name]
        elif name in v12_buy_models:
            merged[name] = v12_buy_models[name]

    # SELL models from V11
    for name, m in v11_models.items():
        if name.endswith("_SELL") or name == "direction_SELL":
            merged[name] = m

    # BUY models from V12 BUY
    for name, m in v12_buy_models.items():
        if name.endswith("_BUY") or name == "direction_BUY":
            merged[name] = m

    # Verify each merged model has a corresponding .pkl file in mixed_v12
    missing_pkl = []
    for name in merged:
        pkl = MIXED_V12_DIR / f"{name}_model.pkl"
        if not pkl.exists():
            missing_pkl.append(f"{name}_model.pkl")

    if missing_pkl:
        print(f"⚠️  Missing .pkl files for: {missing_pkl}")
        print("   These models will be skipped by predictor (warns but continues).")

    # Build config — use V12 BUY's config (has categorical_cols + feature_set)
    # but override experiment_name + description
    config = dict(v12_buy.get("config", {}))
    config["experiment_name"] = "trade_outcome_mixed_v12"
    config["description"] = (
        "Mixed model: V11 SELL (PF 2.5-3.0 realistic) + V12 BUY (overfit, "
        "needs retrain). Built by scripts/build_mixed_v12_metadata.py to fix "
        "silent ML filter disable on B/C/D."
    )
    # Preserve categorical_cols from V12 BUY (V11 had none, predictor defaults are correct)
    # Note: 'regime' in categorical_cols is for V12 BUY; V11 models used regime_encoded + one-hot
    # The predictor's FeatureEngineer handles both via the per-model feature_cols.

    output = {
        "experiment": "trade_outcome_mixed_v12",
        "timestamp": v12_buy.get("timestamp", ""),
        "config": config,
        "categorical_cols": v12_buy.get("categorical_cols",
                                        ["session", "d1_trend", "h4_trend",
                                         "price_vs_cloud", "mfi_signal"]),
        "models": list(merged.values()),
        "sources": {
            "v11": str(V11_RESULTS),
            "v12_buy": str(V12_BUY_RESULTS),
            "note": "SELL models from V11; BUY models from V12 BUY; overall+regime from V11",
        },
    }

    # Backup existing if present (shouldn't be, but safe)
    if OUTPUT.exists():
        backup = OUTPUT.with_suffix(".json.bak")
        shutil.copy2(OUTPUT, backup)
        print(f"  Backed up existing file to {backup}")

    with open(OUTPUT, "w") as f:
        json.dump(output, f, indent=2)

    print(f"✅ Wrote {OUTPUT}")
    print(f"   Models: {len(merged)}")
    sell = [n for n in merged if "SELL" in n]
    buy = [n for n in merged if "BUY" in n]
    other = [n for n in merged if "SELL" not in n and "BUY" not in n]
    print(f"   SELL models ({len(sell)}): {sorted(sell)}")
    print(f"   BUY models ({len(buy)}): {sorted(buy)}")
    print(f"   Shared ({len(other)}): {sorted(other)}")


if __name__ == "__main__":
    main()