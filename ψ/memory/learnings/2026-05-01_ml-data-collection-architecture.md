# ML Data Collection Architecture — Lesson Learned

**Date**: 2026-05-01
**Context**: Designed and partially implemented ML-driven trading system with 3 MT5 demo accounts

## Key Insight

**ML trading system goal is DATA COLLECTION, not profitable trading.**

The 4 signal groups (Volume, OB/OS, MA, Sentiment) fire independently with simple triggers — not because they're profitable, but because they capture diverse market conditions. The ML model will later discover which indicator combinations yield the highest win rate.

## Architecture Decision

- 3 Docker containers (Wine + MT5 + mt5linux), one per demo account
- 4 indicator groups with independent trigger rules
- When ANY group triggers → record ALL indicator values (full feature snapshot)
- SQLite for data storage (simple, sufficient for thousands of trades)
- PyTorch multi-branch attention network for deep learning

## Why Simple Triggers

Simple triggers fire more often → more data points → better ML training. Overly selective triggers would bias the dataset. The ML model will discover selectivity through attention weights and permutation importance.

## Why Separate Accounts

Different balance/leverage combinations are control variables. This lets ML learn whether certain indicator patterns work better under specific capital conditions.

## Cross-Group Correlations Are the Gold

A volume signal that coincides with RSI oversold may have much higher WR than either signal alone. Full feature snapshots capture these interactions.

## Timeline Reality

Phase 4+ requires real MT5 connection → weeks of data collection → then ML can start. This is a "next month" project, not "next session."

## Applicable To

- Signal group engine design for any multi-factor trading system
- ML feature engineering for time-series prediction
- Docker-based multi-account trading infrastructure

## Why

Phase 1.5 showed WR ceiling at 53% with current indicators. ML approach aims to find indicator combinations that break through this ceiling. The Thai trader's philosophy ("ท่าง่าย ทำซ้ำได้ วัดผลได้") reinforces that measurability is key — you can't improve what you can't measure.