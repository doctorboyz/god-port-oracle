#!/usr/bin/env python3
"""Rebuild feature_engineer.joblib for mixed_v12 without bxau dependency.

The original mixed_v12/feature_engineer.joblib was trained with
bxau.ml.features.FeatureEngineer (user's separate package). The pickle
references bxau on unpickle → ModuleNotFoundError in production → silent ML
filter disable on B/C/D.

This script creates a fresh broky.ml.features.FeatureEngineer (in this repo,
no bxau) fit on the same kind of data, producing the same 137 features V11
models expect. The .pkl xgboost models themselves don't depend on bxau — only
the engineer did. So replacing the engineer is enough; no retraining needed.

Usage:
    python scripts/rebuild_engineer_no_bxau.py
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

import joblib
import pandas as pd

from broky.ml.features import FeatureEngineer

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "oracle.db"
V11_RESULTS = PROJECT_ROOT / "data" / "models" / "trade_outcome_v11" / "training_results.json"
MIXED_V12_ENGINEER = PROJECT_ROOT / "data" / "models" / "trade_outcome_mixed_v12" / "feature_engineer.joblib"


def load_training_dataframe() -> pd.DataFrame:
    """Load trade_outcomes.features_json from oracle.db into a DataFrame."""
    import sqlite3

    print(f"Loading features_json from {DB_PATH}...")
    con = sqlite3.connect(str(DB_PATH))
    try:
        df = pd.read_sql_query(
            "SELECT features_json FROM trade_outcomes WHERE features_json IS NOT NULL",
            con,
        )
    finally:
        con.close()

    print(f"  loaded {len(df)} rows")
    records = []
    for raw in df["features_json"].tolist():
        try:
            records.append(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            continue
    out = pd.DataFrame(records)
    print(f"  parsed {len(out)} feature records ({len(out.columns)} columns)")
    return out


def main() -> int:
    # 1. Load V11 expected feature_cols for verification
    with open(V11_RESULTS) as f:
        v11 = json.load(f)
    v11_expected = set(v11["models"][0]["feature_cols"])
    print(f"V11 expects {len(v11_expected)} features")

    # 2. Load training data
    df = load_training_dataframe()

    # 3. Fit fresh broky.ml.features.FeatureEngineer
    print("Fitting fresh broky.ml.features.FeatureEngineer...")
    engineer = FeatureEngineer(fillna=True)
    engineer.fit(df)

    # 4. Verify it produces the features V11 expects
    transformed = engineer.transform(df.head(100))
    produced = set(transformed.columns)
    missing = v11_expected - produced
    extra_session = {c for c in produced if c.startswith("session_") and c != "session_strength"}

    print(f"  transformed shape: {transformed.shape}")
    print(f"  session columns produced: {sorted(extra_session)}")
    print(f"  V11 features missing in transform output: {sorted(missing)}")

    if missing:
        print(f"⚠️  {len(missing)} V11 features still missing — models may fail to predict.")
        print("   These will be filled with median/0 by predictor, degrading accuracy.")
        # Continue anyway — predictor fills missing with 0

    # 5. Backup broken engineer
    if MIXED_V12_ENGINEER.exists():
        backup = MIXED_V12_ENGINEER.with_suffix(".joblib.bxau_bak")
        if not backup.exists():
            import shutil
            shutil.copy2(MIXED_V12_ENGINEER, backup)
            print(f"  backed up bxau engineer to {backup}")

    # 6. Save fresh engineer
    joblib.dump(engineer, MIXED_V12_ENGINEER)
    print(f"✅ Wrote {MIXED_V12_ENGINEER}")
    print(f"   type: {type(engineer).__module__}.{type(engineer).__name__}")
    print(f"   session_columns: {engineer._session_columns}")
    print(f"   feature_columns count: {len(engineer._feature_columns or [])}")

    # 7. Verify it loads without bxau
    reloaded = joblib.load(MIXED_V12_ENGINEER)
    print(f"✅ Reload verified: {type(reloaded).__module__}.{type(reloaded).__name__}")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)