# Exit Plan: Remove `regime_encoded` Ordinal Feature

**Status**: Drafted 2026-06-28
**Owner**: God Port Oracle agent
**Related**: ISSUE-024, learning [[one-hot-regime-encoding]], [[mixed-v12-bxau-broken]]

## Why this exit plan exists

`regime_encoded` is an **ordinal encoding** of the categorical `regime` field
(`trending=1, ranging=0, volatile=2`). Ordinal encoding is semantically wrong
for regime — there is no natural order; volatile is not "twice as much" as
trending. The one-hot encoding (`regime_trending`, `regime_ranging`,
`regime_volatile`) was added in 2026-06-19 (learning
[[one-hot-regime-encoding]]) to fix this.

`regime_encoded` is kept only as a **backward-compat shim** so that V4 models
(which were trained with the ordinal feature) continue to load and predict
after the one-hot features were added. It is technical debt.

## Current state (2026-06-28)

| Model | Uses `regime_encoded`? | Uses one-hot? | Status |
|-------|------------------------|---------------|--------|
| V4 | ✅ (in feature_cols) | ❌ | **Production** — A + B/C/D |
| v5/v6 | ❌ | ✅ | Trained, not deployed (V4 wins backtest) |
| V11 | ❌ | ✅ | SELL models, abandoned (overfit, see [[mixed-v12-bxau-broken]]) |
| V12 BUY | ❌ | ✅ | BUY models, not deployed |

Code references to `regime_encoded`:
- `broky/ml/features.py:202,335,548` — production transform path
- `broky/ml/trade_outcome_trainer.py:347,360` — training feature list
- `scripts/backfill_v6_training_data.py:664` — backfill writes it
- `tests/test_regime_consistency.py` — tests ordinal values (would need deletion)

## Trigger for removal

`regime_encoded` can be removed **only when ALL of these are true**:

1. **V4 is fully retired from production** — no account (A, B, C, D) uses
   `ML_MODEL_DIR=trade_outcome_v4` or `ML_MODEL_DIR_A=trade_outcome_v4`.
2. **A replacement model is deployed** that was trained with one-hot only
   (no `regime_encoded` in its `feature_cols`) and beats V4 in backtest
   (PF > V4's 1.77 at threshold 0.65 — the bar set by
   [[mixed-v12-bxau-broken]]).
3. **No live_trades row references V4** as `ml_model_version` for any open
   trade — wait for V4-attributed trades to close, OR force-close them on
   demo accounts.

## Why V4 cannot be retired yet (2026-06-28)

V4 is the **only** model that:
- Loads without bxau in production (mixed_v12 broken, v6 not deployed)
- Beats every other model in backtest (PF 1.77 A / 1.06 B / 1.44 C at 0.65)
- Has verified real-money performance on Account A

Until a v6+ or V13 model clears the bar (backtest PF > 1.77 at 0.65 with one-hot
only features), V4 stays. `regime_encoded` stays with it.

## Steps to remove (when trigger is met)

1. **Verify no V4 references in production**
   ```bash
   ssh vpsdeluna 'docker compose -f /root/god-port-oracle/docker-compose.vps.yml exec -T oracle-engine printenv | grep -i ml_model_dir'
   ssh vpsdeluna 'docker compose -f /root/god-port-oracle/docker-compose.vps.yml exec -T oracle-engine-train printenv | grep -i ml_model_dir'
   # Both must NOT point to trade_outcome_v4
   ```

2. **Verify no open V4-attributed trades**
   ```sql
   SELECT COUNT(*) FROM live_trades
   WHERE ml_model_version = 'trade_outcome_v4' AND is_open = 1;
   -- Must be 0
   ```

3. **Remove `regime_encoded` from features.py**
   - Line 202: remove `"regime": "regime_encoded"` from `ENCODED_CATEGORICAL_MAP`
   - Line 335: remove the `result["regime_encoded"] = ...` block
   - Line 548: remove `"regime_encoded"` from the encoded-columns loop

4. **Remove `regime_encoded` from trainer**
   - `broky/ml/trade_outcome_trainer.py:347,360` — drop from feature list and
     regime group

5. **Remove from backfill**
   - `scripts/backfill_v6_training_data.py:664` — drop from written columns

6. **Delete obsolete tests**
   - `tests/test_regime_consistency.py:125-177` — these test ordinal encoding
     values (trending=1, ranging=0, volatile=2). Replace with one-hot tests
     if not already covered.

7. **Run full test suite**
   ```bash
   pytest tests/ -q
   ```
   All tests must pass with `regime_encoded` removed.

8. **Verify with `scripts/verify_deploy.sh`**
   - After deploy, run `./scripts/verify_deploy.sh`
   - All checks must PASS on both containers.

9. **Mark ISSUE-024 closed** in `ψ/issues/issues.jsonl` with the commit hash
   and test results as the verification artifact.

## Rollback plan

If V4 needs to be re-deployed urgently after removal:
- `git revert` the removal commit (features.py + trainer + backfill + tests)
- Re-deploy V4: `ML_MODEL_DIR_A=/app/data/models/trade_outcome_v4`
- Run `./scripts/verify_deploy.sh`

The revert restores `regime_encoded` because V4's `feature_cols` lists it; the
predictor will fail to find the column and produce `no prediction` without the
revert.

## Estimated timeline

- Earliest: when a v6+ or V13 model clears backtest PF > 1.77 at 0.65 with
  one-hot only features.
- Realistic: 1-2 months of training experiments (V11 overfit, V12 BUY not yet
  proven, v6 not deployed).
- Blocker: must solve the "training PF ≠ trading PF" problem first
  (see [[mixed-v12-bxau-broken]] — V11 SELL training PF=3.0 → real PF=1.28).

Do NOT remove `regime_encoded` speculatively. V4 is load-bearing.