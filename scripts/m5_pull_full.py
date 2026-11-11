#!/usr/bin/env python3
"""Pull the full M5 bar history the bridge can serve (deep tick era).

The farm fetcher's store only keeps the newest ~15k M5 bars (~74 days),
but the bracket grid needs closed bars across the whole ~10-month tick
era. This script asks the SAME read-only bridge (copy_rates_from_pos_json)
for the maximum M5 depth and writes it to
data/paper-feed/ticks/XAUUSD_M5_full.csv — a SEPARATE file from the
fetcher's XAUUSD_M5.csv, which is never touched.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd  # noqa: E402

from scripts.paper_feed_fetcher import BridgeFeed  # noqa: E402

OUT_PATH = _ROOT / "data" / "paper-feed" / "ticks" / "XAUUSD_M5_full.csv"
REQUEST_COUNT = 75_000   # bridge serves ≥70k M5 bars (probe 2026-11-12:
                         # 50k→2026-03-02, 70k→2025-11-17; 100k returns
                         # nothing — payload too large for one rpyc call)


def main() -> int:
    feed = BridgeFeed()
    # _copy(tf_id, count) is the class's single bridge read; MT5 M5 id
    from metty.bridge.client import MT5_TIMEFRAMES

    if not feed._connect():
        print("bridge connect failed")
        return 1
    df = feed._copy(MT5_TIMEFRAMES["M5"], REQUEST_COUNT)
    if df is None:
        # shared-terminal heal (same posture as the fetcher: one
        # idempotent initialize(), never shutdown)
        feed._conn.root.initialize()
        df = feed._copy(MT5_TIMEFRAMES["M5"], REQUEST_COUNT)
    if df is None or df.empty:
        print("bridge returned no M5 bars")
        return 1
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    out = df.reset_index()
    out.to_csv(OUT_PATH, index=False)
    print(f"wrote {len(out)} M5 bars -> {OUT_PATH}")
    print(f"first bar: {out['time'].iloc[0]}  last bar: {out['time'].iloc[-1]}")
    # continuity: count gaps larger than 5 minutes (weekends expected)
    ts = pd.to_datetime(out["time"])
    gaps = (ts.diff().dt.total_seconds().dropna() > 300) & \
           (ts.diff().dt.total_seconds().dropna() < 2 * 86_400)
    print(f"intra-day gaps > 5min (excl. weekends): {int(gaps.sum())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
