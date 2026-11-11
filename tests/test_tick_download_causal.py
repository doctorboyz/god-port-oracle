"""Causal tests for the tick downloader — two live failures, RED→GREEN.

(1) 2026-11-12 live: the January window blew the rpyc timeout and the
    exception killed the whole detached run at day 1 — one bad day must
    be skipped, progress kept, and the run continue.
(2) 2026-11-12 live: CALL_CAP=5M made the server try to ship ~66M ticks
    in one call — the cap must bound each request and the continuation
    loop must pick up a window denser than the cap.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts import tick_download as td


class StubBridge:
    """Fake ArchiveBridge — records requests, returns canned batches."""

    def __init__(self, batches_per_call: list[list[dict]] | None = None,
                 fail_for_epochs: set[int] | None = None):
        self.requests: list[tuple[int, int]] = []
        self.batches = batches_per_call or []
        self.fail_for_epochs = fail_for_epochs or set()

    def copy_ticks(self, from_epoch: int, count: int) -> list[dict]:
        self.requests.append((from_epoch, count))
        if from_epoch in self.fail_for_epochs:
            raise TimeoutError("result expired")
        if not self.batches:
            return []
        return self.batches.pop(0)

    def _close(self):
        pass


def _tick(epoch_s: float, msc: int) -> dict:
    return {"time_msc": msc, "time": int(epoch_s), "bid": 1.0,
            "ask": 1.2, "last": 1.1, "volume_real": 0.0, "flags": 1}


def test_bad_day_does_not_kill_run(tmp_path, monkeypatch):
    """A day whose window times out is skipped; the next day still runs.

    RED against the pre-fix code (exception propagated out of
    run_download, nothing after the bad day was ever attempted).
    """
    monkeypatch.setattr(td, "TICK_DIR", tmp_path)
    monkeypatch.setattr(td, "PROGRESS_FILE", tmp_path / "progress.json")
    monkeypatch.setattr(td, "day_path",
                       lambda day: tmp_path / f"ticks_{day.strftime('%Y-%m-%d')}.parquet")
    # freeze 'now' far in the future so both days look elapsed
    monkeypatch.setattr(td.time, "time", lambda: 2_000_000_000)

    bad_epoch = int(datetime(2026, 1, 2, 0, tzinfo=timezone.utc).timestamp())
    bridge = StubBridge(fail_for_epochs={bad_epoch})
    from_day = datetime(2026, 1, 2, tzinfo=timezone.utc)
    to_day = datetime(2026, 1, 3, tzinfo=timezone.utc)

    td.run_download(bridge, from_day, to_day)   # must NOT raise

    progress = td.load_progress()
    # day 2 failed → not marked complete; day 3 (weekend, empty) processed
    assert "2026-01-03" in progress["completed_days"]
    assert "2026-01-02" not in progress["completed_days"]


def test_dense_window_continues_past_call_cap(monkeypatch):
    """A window denser than CALL_CAP is fetched by continuation calls.

    RED against a naive single-request fetch: the first call returns
    exactly CALL_CAP ticks and stops short of the window end — the loop
    must re-request from the last tick's msc until the window is covered.
    """
    start = int(datetime(2026, 1, 2, 0, tzinfo=timezone.utc).timestamp())
    end = start + td.WINDOW_HOURS * 3600
    cap = td.CALL_CAP

    batch1 = [_tick(0, (start + 60) * 1000 + i) for i in range(cap)]
    batch2 = [_tick(0, (start + 120) * 1000 + i) for i in range(7)]
    bridge = StubBridge(batches_per_call=[batch1, batch2])

    out = td.fetch_window(bridge, start, end)
    assert len(out) == cap + 7
    # the continuation started right after the last tick of batch 1
    second_from = bridge.requests[1][0]
    assert second_from * 1000 >= batch1[-1]["time_msc"]


def test_call_cap_bounds_single_request():
    """Each bridge request must stay within the documented cap.

    Guards the live failure: an oversized count asks the server for
    months of ticks in one response (AsyncResultTimeout).
    """
    assert td.CALL_CAP <= 200_000
    bridge = StubBridge()
    start = int(datetime(2026, 1, 2, 0, tzinfo=timezone.utc).timestamp())
    try:
        td.fetch_window(bridge, start, start + 3600)
    except Exception:
        pass
    for _, count in bridge.requests:
        assert count == td.CALL_CAP
