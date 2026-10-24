"""Causal proof test: watchdog must cover the farm2 container (2026-10-24).

Hypothesis
----------
farm_watchdog hardcodes oracle-engine-farm1 in CONTAINERS and in the
regression scan's `docker logs` call. Deploying the trend-following axis
(farm2) adds a second engine container that the watchdog is blind to: a
dead farm2 container or a 'spread unavailable' regression inside it would
alert no one — exactly the ISSUE-097/094 silence class the watchdog exists
to close, reborn one group later.

Causal proof
------------
Expected containers are derived from the generated compose (itself built
from farm_variants.yml), so any group present in the farm is checked:
- docker ps missing oracle-engine-farm2 → alert naming farm2
- 'spread unavailable' in farm2's log only → regression alert
- missing farm1 alone must still alert (control, axis-1 kept)
- unreadable compose → falls back to farm1 (no blind watchdog on a
  half-deployed box)
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import farm_watchdog as wd  # noqa: E402

_TWO_GROUP_COMPOSE = """\
services:
  paper-fetcher:
    container_name: paper-fetcher
  oracle-engine-farm1:
    container_name: oracle-engine-farm1
  oracle-engine-farm2:
    container_name: oracle-engine-farm2
"""


def _patch_compose(monkeypatch, tmp_path, text: str = _TWO_GROUP_COMPOSE) -> None:
    compose = tmp_path / "docker-compose.paper-farm.yml"
    compose.write_text(text)
    monkeypatch.setattr(wd, "COMPOSE_FILE", compose)


def _patch_docker(monkeypatch, ps_output: str, logs: dict[str, str]) -> None:
    """Fake docker CLI: `docker ps` returns ps_output, `docker logs <name>`
    returns logs.get(name, "") for engine containers."""

    class _Result:
        def __init__(self, stdout: str):
            self.stdout = stdout

    def fake_run(cmd, **kwargs):
        if "ps" in cmd:
            return _Result(ps_output)
        for name, out in logs.items():
            if name in cmd:
                return _Result(out)
        return _Result("")

    monkeypatch.setattr(wd, "_docker_bin", lambda: "docker")
    monkeypatch.setattr(wd.subprocess, "run", fake_run)


_PS_FARM1_ONLY = (
    "paper-fetcher: Up 2 hours\n"
    "oracle-engine-farm1: Up 2 hours (healthy)\n"
)
_PS_ALL_UP = _PS_FARM1_ONLY + "oracle-engine-farm2: Up 10 minutes (healthy)\n"


class TestWatchdogFarm2Causal:

    def test_dead_farm2_container_flagged(self, monkeypatch, tmp_path):
        # RED before the fix: farm2 wasn't in CONTAINERS, so a missing
        # farm2 container was silently accepted as healthy
        _patch_compose(monkeypatch, tmp_path)
        _patch_docker(monkeypatch, ps_output=_PS_FARM1_ONLY, logs={})

        problem = wd.check_containers()
        assert problem is not None and "oracle-engine-farm2" in problem, (
            "a farm2 container missing from docker ps is a farm problem the "
            "watchdog must catch — axis 2 must not be watched by omission"
        )

    def test_dead_farm1_still_flagged(self, monkeypatch, tmp_path):
        # Control: axis-1 coverage must survive the farm2 addition
        _patch_compose(monkeypatch, tmp_path)
        _patch_docker(
            monkeypatch,
            ps_output="paper-fetcher: Up 2 hours\noracle-engine-farm2: Up 10 min\n",
            logs={},
        )

        problem = wd.check_containers()
        assert problem is not None and "oracle-engine-farm1" in problem, (
            "farm1 missing must still alert — the axis-2 expansion must not "
            "weaken axis-1 monitoring"
        )

    def test_all_up_healthy(self, monkeypatch, tmp_path):
        # Control: the normal state stays silent
        _patch_compose(monkeypatch, tmp_path)
        _patch_docker(monkeypatch, ps_output=_PS_ALL_UP, logs={})

        assert wd.check_containers() is None

    def test_regression_in_farm2_log_caught(self, monkeypatch, tmp_path):
        # RED before the fix: the regression scan only ran docker logs on
        # oracle-engine-farm1 — a farm2-only 'spread unavailable' passed
        _patch_compose(monkeypatch, tmp_path)
        _patch_docker(
            monkeypatch,
            ps_output=_PS_ALL_UP,
            logs={"oracle-engine-farm2":
                  "INFO cycle ok\nWARN spread unavailable (MT5 disconnected?)"},
        )
        since = datetime(2026, 10, 24, 0, 0)

        problem = wd.check_regression(since)
        assert problem is not None and "spread unavailable" in problem, (
            "a fixed-bug signature in farm2's log must alert — regression "
            "coverage is per-container, farm2 is in the farm"
        )

    def test_regression_in_farm1_still_caught(self, monkeypatch, tmp_path):
        # Control: farm1-only regression keeps alerting after the loop change
        _patch_compose(monkeypatch, tmp_path)
        _patch_docker(
            monkeypatch,
            ps_output=_PS_ALL_UP,
            logs={"oracle-engine-farm1": "WARN spread unavailable"},
        )
        since = datetime(2026, 10, 24, 0, 0)

        problem = wd.check_regression(since)
        assert problem is not None and "spread unavailable" in problem

    def test_no_regression_silent(self, monkeypatch, tmp_path):
        _patch_compose(monkeypatch, tmp_path)
        _patch_docker(
            monkeypatch, ps_output=_PS_ALL_UP,
            logs={"oracle-engine-farm1": "ok", "oracle-engine-farm2": "ok"},
        )
        since = datetime(2026, 10, 24, 0, 0)

        assert wd.check_regression(since) is None

    def test_missing_compose_falls_back_to_farm1(self, monkeypatch, tmp_path):
        # A half-deployed box (compose gone) must still watchdog farm1 —
        # fail-open to the known container, never to "check nothing"
        monkeypatch.setattr(wd, "COMPOSE_FILE", tmp_path / "not_there.yml")
        _patch_docker(monkeypatch, ps_output=_PS_FARM1_ONLY, logs={})

        assert wd.check_containers() is None, (
            "fallback = farm1-only watchlist; farm1 up means healthy, not "
            "'compose unreadable so everything counts as up'"
        )