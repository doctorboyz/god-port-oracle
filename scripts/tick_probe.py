#!/usr/bin/env python3
"""Read-only probe: how deep does Exness tick history go via the bridge?

Calls ONLY initialize / symbol_select / symbol_info / copy_ticks_from
(flags=1 INFO) — same read-only safety posture as the fetcher and the
bet engine. Never touches orders, positions or shutdown.

Output: the earliest tick timestamp the broker will serve for XAUUSD,
sampled by walking backwards from now in fixed-size day steps (we ask
for `count` ticks starting at each probe date and look at what comes
back — copy_ticks_from returns ticks at-or-after date_from, so the
first tick of the deepest still-non-empty window is the depth).
"""
from __future__ import annotations

import ast
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.bet_engine import TickFeed as BridgeTickFeed  # noqa: E402

SYMBOL = "XAUUSD"
DAY = 86_400


def probe(feed: BridgeTickFeed, from_epoch: int, count: int) -> list[dict]:
    ticks = feed._conn.root.copy_ticks_from(
        feed._resolved, int(from_epoch), count, 1,
    )
    if not ticks:
        return []
    return ast.literal_eval(str(ticks))


def main() -> int:
    feed = BridgeTickFeed()
    if not feed._connect():
        print("PROBE FAIL: cannot connect bridge")
        return 1
    print(f"bridge symbol resolved: {feed._resolved}")

    now = int(time.time())
    # coarse scan: ask for a handful of ticks at each 30-day step
    for days_back in (365, 330, 300, 270, 240, 210, 180, 150, 120, 90, 60, 30, 0):
        from_epoch = now - days_back * DAY
        ticks = probe(feed, from_epoch, 3)
        if not ticks:
            print(f"  -{days_back:>3}d ({datetime.fromtimestamp(from_epoch, tz=timezone.utc).date()}): EMPTY")
            continue
        first = ticks[0]
        first_utc = datetime.fromtimestamp(first["time"], tz=timezone.utc)
        print(f"  -{days_back:>3}d ask@{datetime.fromtimestamp(from_epoch, tz=timezone.utc).date()}"
              f" -> first tick {first_utc.isoformat()} n={len(ticks)} keys={sorted(first.keys())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
