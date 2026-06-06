---
name: v4-model-deployment-fresh-start
description: v4 model with 10 sub-models including volatile regime deployed to VPS; accounts reset for fresh performance tracking
metadata:
  type: learning
  source: rrr: god-port-oracle
  date: 2026-06-04
---

# v4 Model Deployed — Fresh Start

**What**: v4 model deployed to VPS with all 10 sub-models loaded:
- overall, regime_trending, regime_ranging, regime_volatile
- direction_BUY, direction_SELL
- trending_BUY, trending_SELL, ranging_BUY, ranging_SELL

**Volatile model**: 74.2% accuracy, PF 1.50 (was 0% before — zero data)

**Accounts reset**:
- A: Standard $100 (1:2000 leverage)
- B: Pro $500 (1:500 leverage)  
- C: Raw Spread $1000 (1:500 leverage)

**ML filter**: Enabled on all 6 traders (Swing A/B/C + M5Scalp A/B/C), health checks passing, loss_threshold=65%

**What changed from v2.best**:
1. 2,925 synthetic trades added to training data (1,004 bearish + 1,921 bullish)
2. Volatile regime now has data (was 0% before)
3. H4 trend features included
4. Volatile threshold lowered from 0.04 to 0.035
5. sklearn version pinned in Dockerfile

**How to apply**: Monitor v4 performance for 24-48 hours. Compare win rate, PF, MaxDD per account. If volatile model underperforms, consider data augmentation.

## Related

- [[sklearn-version-pinning-v4-deploy]] — sklearn version fix that enabled this deployment