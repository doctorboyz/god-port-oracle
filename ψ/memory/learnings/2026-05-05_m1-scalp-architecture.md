M1 Scalp Architecture — Parallel Mode with Swing

**Architecture**: Per-account threads: collector (300s M5), swing-trader (300s M5), scalp-trader (60s M1). Same oracle-engine, separate threads, separate risk config, tagged trades.

**Key files**:
- `broky/signals/scalp_generator.py` — M1 signal generator (EMA 5/13, MACD 6/13/5, ADX 7, Boll 10/1.5, Vol 10)
- `broky/risk/spread_filter.py` — `check_spread()` skip if spread > threshold
- `metty/execution/scalp_trader.py` — ScalpTrader with ScalpRiskConfig
- `metty/bridge/client.py` — PersistentMT5Bridge (keep-alive + auto-reconnect + get_symbol_info)

**Scalp vs Swing differences**:
- No D1 counter-trend filter (scalps too short)
- Session gate: London + Overlap only (no Asian/NY)
- Spread filter: skip if spread > 30 points
- Lower ADX threshold (15 vs 20), lower min confidence (0.55 vs 0.60)
- Tighter risk: 1% per trade, 1x ATR SL, 1.5x R:R, 20 min max hold, 3 min cooldown

**DB tagging**: All trades/signals/snapshots/candles have `trading_mode` + `strategy_id` columns. Swing = "swing-A/B/C", Scalp = "scalp-A/B/C".

**Deployment**: SCALP_ENABLED=0 by default. Set to 1 on VPS to enable. No position limit per account — scalp and swing coexist.