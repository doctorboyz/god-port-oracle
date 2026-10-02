"""Causal proof test: MT5 re-login gives up permanently while terminal still booting (ISSUE-093).

Hypothesis
----------
Bug: _mt5_health_check (scripts/oracle_runner.py) attempts exactly ONE
unspaced login per 300s cycle and ignores initialize()'s return value. When
MT5 containers are recreated, the terminal GUI finishes auto-login SEVERAL
MINUTES after the healthcheck turns healthy (live incident 2026-09-24:
auto-login gave up 02:48, terminal self-logged-in 03:03 — 15 min of
"Authorization failed" noise + zero signal coverage). The startup
ensure_mt5_logged_in also logs a false "manual VNC login required" — the
situation self-heals once the terminal finishes booting.

Causal proof
------------
Stub the account config + rpyc with a fake bridge whose terminal is "still
booting" (account_info None, initialize False, login False). Before fix:
login called ONCE with ZERO spacing → the cycle gives up, matching the
incident. After fix: MT5_RELOGIN_ATTEMPTS spaced by
MT5_RELOGIN_SPACING_SECONDS, and the MT5_AUTO_LOGIN=0 guard (brokerless
paper farm) never touches rpyc at all.

RED before fix, GREEN after. The healthy-account control passes both sides.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import oracle_runner as runner  # noqa: E402


class _FakeRoot:
    """Fake rpyc connection root exposing the bridge surface we touch."""

    def __init__(self, script):
        self._script = script

    def account_info(self):
        return self._script.next_account_info()

    def initialize(self):
        return self._script.next_initialize()

    def login(self, login, password, server):
        return self._script.next_login()

    def last_error(self):
        return (-6, "Terminal: Authorization failed")


class _FakeConn:
    def __init__(self, script):
        self.root = _FakeRoot(script)

    def close(self):
        pass


class _BridgeScript:
    """Per-test scripted bridge responses + call recording."""

    def __init__(self, account_infos=None, initializes=None, logins=None):
        self._account_infos = list(account_infos or [])
        self._initializes = list(initializes or [])
        self._logins = list(logins or [])
        self.login_calls = 0
        self.initialize_calls = 0
        self.connect_calls = 0

    def next_account_info(self):
        return self._account_infos.pop(0) if self._account_infos else None

    def next_initialize(self):
        self.initialize_calls += 1
        return self._initializes.pop(0) if self._initializes else False

    def next_login(self):
        self.login_calls += 1
        return self._logins.pop(0) if self._logins else False

    def connect(self, host, port, config=None):
        self.connect_calls += 1
        return _FakeConn(self)


class _SleepRecorder:
    def __init__(self):
        self.sleeps: list[float] = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)


def _install_bridge(monkeypatch, script: _BridgeScript, sleeps: _SleepRecorder, account="B"):
    cfg = SimpleNamespace(
        display_name=f"P{account}",
        bridge_host=f"mt5{account.lower()}",
        bridge_internal_port=8001,
        broker_login="123456",
        broker_password="secret",
        broker_server="Exness-MT5Trial17",
    )
    monkeypatch.setattr(runner, "get_account_config", lambda name: cfg)
    monkeypatch.setattr("rpyc.connect", script.connect)
    monkeypatch.setattr(runner, "time", SimpleNamespace(sleep=sleeps.sleep))
    monkeypatch.delenv("MT5_AUTO_LOGIN", raising=False)
    return cfg


class TestMt5ReloginSpacedRetryCausal:
    """Causal proof: failed login must retry with spacing, not give up after one attempt."""

    def test_failed_login_retries_spaced(self, monkeypatch):
        """RED before fix: one attempt, zero sleeps. GREEN: 3 spaced attempts.

        Terminal still booting → initialize() fails with (-6, Authorization
        failed) on every attempt. The fix intentionally skips login() when
        initialize() failed (login on an uninitialized terminal is a doomed
        RPC), so the retry chain is proven via initialize calls + spacing.
        """
        monkeypatch.setattr(runner, "MT5_RELOGIN_ATTEMPTS", 3, raising=False)
        monkeypatch.setattr(runner, "MT5_RELOGIN_SPACING_SECONDS", 60, raising=False)
        script = _BridgeScript()  # terminal still booting: everything fails
        sleeps = _SleepRecorder()
        _install_bridge(monkeypatch, script, sleeps)

        assert runner._mt5_health_check("B") is False

        assert script.initialize_calls == 3, (
            f"expected 3 spaced attempts, got {script.initialize_calls} — "
            "health check gives up after a single attempt while the terminal "
            "is still booting (ISSUE-093)"
        )
        assert sleeps.sleeps == [60, 60], (
            f"expected 60s spacing between attempts, got {sleeps.sleeps}"
        )

    def test_login_failures_also_retry_spaced(self, monkeypatch):
        """initialize succeeds but terminal still authorizing: login retried spaced."""
        monkeypatch.setattr(runner, "MT5_RELOGIN_ATTEMPTS", 3, raising=False)
        monkeypatch.setattr(runner, "MT5_RELOGIN_SPACING_SECONDS", 60, raising=False)
        script = _BridgeScript(
            initializes=[True, True, True],
            logins=[False, False, False],
        )
        sleeps = _SleepRecorder()
        _install_bridge(monkeypatch, script, sleeps)

        assert runner._mt5_health_check("B") is False
        assert script.login_calls == 3, (
            f"expected 3 spaced login attempts, got {script.login_calls}"
        )
        assert sleeps.sleeps == [60, 60]

    def test_auto_login_disabled_skips_all_attempts(self, monkeypatch):
        """Brokerless guard (paper farm pins MT5_AUTO_LOGIN=0): zero rpyc traffic."""
        monkeypatch.setattr(runner, "MT5_RELOGIN_ATTEMPTS", 3, raising=False)
        monkeypatch.setattr(runner, "MT5_RELOGIN_SPACING_SECONDS", 60, raising=False)
        script = _BridgeScript()
        sleeps = _SleepRecorder()
        _install_bridge(monkeypatch, script, sleeps)
        monkeypatch.setenv("MT5_AUTO_LOGIN", "0")

        assert runner._mt5_health_check("B") is False
        assert script.connect_calls == 0, "MT5_AUTO_LOGIN=0 must not touch rpyc at all"
        assert script.login_calls == 0
        assert sleeps.sleeps == []

    def test_healthy_account_no_relogin(self, monkeypatch):
        """Control: account_info present → True, zero login attempts (passes both sides)."""
        monkeypatch.setattr(runner, "MT5_RELOGIN_ATTEMPTS", 3, raising=False)
        monkeypatch.setattr(runner, "MT5_RELOGIN_SPACING_SECONDS", 60, raising=False)
        script = _BridgeScript(account_infos=[{"balance": 1000.0, "equity": 1000.0}])
        sleeps = _SleepRecorder()
        _install_bridge(monkeypatch, script, sleeps)

        assert runner._mt5_health_check("B") is True
        assert script.login_calls == 0
        assert script.initialize_calls == 0
        assert sleeps.sleeps == []

    def test_second_attempt_success_returns_true(self, monkeypatch):
        """Terminal finishes booting mid-retry: first attempt fails, second succeeds."""
        monkeypatch.setattr(runner, "MT5_RELOGIN_ATTEMPTS", 3, raising=False)
        monkeypatch.setattr(runner, "MT5_RELOGIN_SPACING_SECONDS", 60, raising=False)
        script = _BridgeScript(
            account_infos=[None, None, {"balance": 1000.0, "equity": 1000.0}],
            initializes=[False, True],
            logins=[True],
        )
        sleeps = _SleepRecorder()
        _install_bridge(monkeypatch, script, sleeps)

        assert runner._mt5_health_check("B") is True
        assert script.login_calls == 1, "login must only run after a successful initialize"
        assert sleeps.sleeps == [60], f"expected exactly one 60s spacing, got {sleeps.sleeps}"