"""Causal proof test: MT5_BRIDGE_MAX_RETRIES=0 must mean "bridge off", silently.

Hypothesis
----------
Brokerless farm engines set MT5_BRIDGE_MAX_RETRIES=0 intending "there is no
bridge — don't try". Today connect() skips the loop (range(1,1) is empty)
but still logs `Failed to connect to MT5 bridge at mt5pXX:50XX after 0
attempts` at ERROR level on every bridge touch — 11,520 ERROR lines / 24h
on the farm (10 accounts x 4 calls x 288 cycles), drowning real signal in
the logs. A disabled bridge is a configuration, not a fault.

Causal proof
------------
MAX_RETRIES=0 → connect() returns False, rpyc.connect is NEVER invoked,
and no ERROR record is emitted. Control: MAX_RETRIES=1 → rpyc.connect
attempted exactly once (existing behavior preserved for live/VPS, which
default to 3).

This test FAILS (RED) before the fix — the no-ERROR assertion trips on the
current `Failed to connect ... after 0 attempts` line — PASSES (GREEN) after.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
from types import SimpleNamespace

import pytest

from metty.core.models import AccountConfig

_BRIDGE_LOGGER = "metty.bridge.client"


def _reload_client(monkeypatch, retries: str) -> object:
    """Reload metty.bridge.client so MAX_RETRIES re-reads the env."""
    monkeypatch.setenv("MT5_BRIDGE_MAX_RETRIES", retries)
    monkeypatch.setenv("MT5_BRIDGE_RETRY_DELAY", "0")
    import metty.bridge.client as client_mod

    return importlib.reload(client_mod)


@pytest.fixture(autouse=True)
def _restore_client_module(monkeypatch):
    yield
    monkeypatch.delenv("MT5_BRIDGE_MAX_RETRIES", raising=False)
    monkeypatch.delenv("MT5_BRIDGE_RETRY_DELAY", raising=False)
    import metty.bridge.client as client_mod

    importlib.reload(client_mod)  # restore default MAX_RETRIES=3 for the suite


class TestBridgeDisabledModeCausal:
    """MAX_RETRIES=0 = bridge intentionally off: no attempt, no ERROR log."""

    def test_zero_retries_skips_connection_and_stays_silent(self, monkeypatch, caplog):
        client_mod = _reload_client(monkeypatch, "0")
        assert client_mod.MAX_RETRIES == 0

        calls = {"n": 0}

        class _ForbiddenRpyc:
            @staticmethod
            def connect(*args, **kwargs):
                calls["n"] += 1
                raise AssertionError("must not touch rpyc when bridge disabled")

        monkeypatch.setattr(client_mod, "rpyc", _ForbiddenRpyc)

        cfg = AccountConfig(name="t", broker_login="1", broker_server="s", balance=100.0)
        bridge = client_mod.MT5Bridge(cfg)

        with caplog.at_level(logging.DEBUG, logger=_BRIDGE_LOGGER):
            ok = asyncio.run(bridge.connect())

        assert ok is False, "disabled bridge must report not-connected"
        assert calls["n"] == 0, "disabled bridge must not attempt rpyc.connect"
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert not errors, (
            f"disabled bridge must not log ERROR (config, not fault): {errors}"
        )

    def test_one_retry_still_attempts_once(self, monkeypatch, caplog):
        # Control: retries=1 keeps the existing fail-fast path (farm pre-noise)
        client_mod = _reload_client(monkeypatch, "1")
        assert client_mod.MAX_RETRIES == 1

        calls = {"n": 0}

        class _BoomRpyc:
            @staticmethod
            def connect(*args, **kwargs):
                calls["n"] += 1
                raise ConnectionRefusedError("boom")

        monkeypatch.setattr(client_mod, "rpyc", _BoomRpyc)

        cfg = AccountConfig(name="t", broker_login="1", broker_server="s", balance=100.0)
        bridge = client_mod.MT5Bridge(cfg)

        with caplog.at_level(logging.DEBUG, logger=_BRIDGE_LOGGER):
            ok = asyncio.run(bridge.connect())

        assert ok is False
        assert calls["n"] == 1, "retries=1 must attempt exactly once"