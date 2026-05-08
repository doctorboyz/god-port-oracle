---
name: live-collector-architecture
description: Live data collector architecture — sentiment caching, calendar awareness, MT5 bridge fallback
type: project
---

# Live Data Collector Architecture

## Data Flow
```
LiveCollector.run_once()
  ├── _load_candles_from_bridge()  # MT5 async → CSV fallback
  ├── _get_sentiment()              # Fear & Greed (15min cache) + Finnhub news
  ├── _get_calendar()               # Finnhub calendar (1hr cache)
  ├── _compute_snapshot()           # All 4 groups + Broky indicators + sentiment
  └── insert_signal + insert_feature_snapshot → SQLite
```

## Caching Strategy
- Fear & Greed: 15-minute cache (updates ~daily)
- Finnhub news: 15-minute cache (updates ~hourly on free tier)
- Calendar events: 1-hour cache (daily events don't change often)
- MT5 candles: No cache (always fresh)

## Account IDs
- 1 = historical (from CSV collection)
- 2 = live_A ($100, 1:2000, Exness Standard)
- 3 = live_B ($500, 1:500, Exness Pro)
- 4 = live_C ($1000, 1:500, Exness Raw Spread)

## CLI Usage
```bash
python3 -m metty.cli collect-live --cycles 1           # test
python3 -m metty.cli collect-live --interval 300       # 5min continuous
python3 -m metty.cli collect-live --account A --verbose
```

## Key Limitation
Current CSV fallback uses the LAST 500 bars from historical CSV. When MT5 bridge connects, it will use live data instead. The collector auto-falls back from bridge to CSV.

**Why**: Designed for Phase 5 ML data collection. Bridge-first, CSV-fallback ensures collection works even during development.

**How to apply**: When connecting MT5 bridge on VPS, test with `--cycles 1` first, then run continuous.