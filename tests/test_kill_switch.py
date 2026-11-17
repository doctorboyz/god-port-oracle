"""Kill switch supervisor — behavioral tests (all bridges mocked).

doctorboyz order 2026-11-16: portfolio kill switch with 5 independent
triggers, group policies (demo/cent = flatten+stop+TG; real = flatten+
block+URGENT TG, never a container stop), JSON state persistence with
trigger dedup, and dry-run default.

These tests run against MOCKED RPyC bridges and a MOCKED docker socket
client — the order forbids touching any real container from this
machine. The mock records every call, which is also how the handle-safety
contract (the repo's "one bridge = one terminal handle" lesson) is
enforced: initialize/shutdown on the bridge must NEVER be called by the
supervisor.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scripts.kill_switch as ks  # noqa: E402
from scripts.kill_switch import (  # noqa: E402
    GROUP_CENT,
    GROUP_DEMO,
    GROUP_REAL,
    HandleSafeBridge,
    KillConfig,
    KillState,
    KillSwitchSupervisor,
    Observation,
    evaluate_triggers,
)

# ─── RPyC bridge mock ────────────────────────────────────────────────────


class FakeRoot:
    """Stands in for the RPyC service exposed by docker/mt5 bridges.

    Records initialize/shutdown so the handle-safety contract is testable,
    and order_send so flatten behavior is assertable. account_info is
    call-counted: since ISSUE-010 phase 2 the supervisor must NEVER read
    equity over the bridge (that polling disturbed the engines' single
    terminal handle) — any call is a contract break.
    """

    def __init__(self, equity=None, positions=None, mid=None, tick_bid=0.0):
        self.equity = equity
        self.positions = positions or []
        self.mid = mid
        self.tick_bid = tick_bid
        self.initialize_calls = 0
        self.shutdown_calls = 0
        self.account_info_calls = 0
        self.order_calls: list[tuple] = []
        self.closed_tickets: list = []

    def initialize(self):
        self.initialize_calls += 1
        return True

    def shutdown(self):
        self.shutdown_calls += 1
        return True

    def account_info(self):
        self.account_info_calls += 1
        if self.equity is None:
            return None
        return {"equity": self.equity, "balance": self.equity, "currency": "USD"}

    def positions_get(self, symbol=None, ticket=None):
        return [dict(p) for p in self.positions]

    def symbol_info_tick(self, symbol):
        if self.mid is None:
            return None
        return {"bid": self.mid, "ask": self.mid}

    def order_send(self, action, symbol, volume, order_type, price,
                   sl, tp, deviation, magic, comment, type_time, filling,
                   position):
        self.order_calls.append((action, symbol, volume, order_type, position))
        if position:
            self.closed_tickets.append(position)
            # a closed position is gone from the terminal
            self.positions = [p for p in self.positions
                               if p.get("ticket") != position]
        return {"retcode": 10009, "order": 1, "price": price, "volume": volume}


class FakeConn:
    def __init__(self, root: FakeRoot):
        self.root = root
        self.closed = False

    def close(self):
        self.closed = True


def install_bridge_mock(monkeypatch, roots_by_host_port: dict):
    """rpyc.connect(host, port, config=...) → FakeConn for the given bridge."""
    def fake_connect(host, port, config=None):
        return FakeConn(roots_by_host_port[(host, port)])

    monkeypatch.setattr(ks.rpyc, "connect", fake_connect)
    for root in roots_by_host_port.values():
        root.initialize_calls = 0
        root.shutdown_calls = 0


def install_tcp_probe(monkeypatch, ok=True):
    monkeypatch.setattr(ks, "probe_tcp", lambda host, port, timeout=5.0: ok)


def install_tg(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(ks, "_send_tg", lambda text: sent.append(text))
    return sent


class FakeDocker:
    def __init__(self):
        self.stops: list[str] = []

    def stop(self, container, t=30):
        self.stops.append(container)
        return {"success": True, "status": 204}


# ─── Config / env helpers ────────────────────────────────────────────────


def target_by_name(config: KillConfig, name: str):
    return next(t for t in config.targets if t.name == name)


def make_config(monkeypatch, tmp_path, **env):
    """Config from env, with the same wiring the compose file gives the
    service (groups, per-account engine containers, empty DB paths).

    HERMETIC: the full suite leaks MT5_BRIDGE_A_HOST=localhost from older
    modules that os.environ.setdefault without cleanup (found live
    2026-11-17: kill-switch REAL tests KeyError'd in the full run while
    passing alone). Every KILL_*/MT5_BRIDGE_*/MT5_SYMBOL_* leftover is
    deleted first — supervisor tests must not depend on ambient env."""
    import os

    for key in list(os.environ):
        if key.startswith(("KILL_", "MT5_BRIDGE_", "MT5_SYMBOL_")):
            monkeypatch.delenv(key, raising=False)
    defaults = {
        "KILL_SWITCH_DRY_RUN": "1",
        "KILL_STATE_PATH": str(tmp_path / "state.json"),
        "KILL_DEMO_ACCOUNTS": "B,C,D",
        "KILL_CENT_ACCOUNTS": "P1,P2,P3",
        "KILL_REAL_ACCOUNTS": "A",
        "KILL_ENGINE_CONTAINER_B": "oracle-engine-train",
        "KILL_ENGINE_CONTAINER_C": "oracle-engine-train",
        "KILL_ENGINE_CONTAINER_D": "oracle-engine-train",
        "KILL_ENGINE_CONTAINER_P1": "oracle-engine-p1",
        "KILL_ENGINE_CONTAINER_P2": "oracle-engine-p2",
        "KILL_ENGINE_CONTAINER_P3": "oracle-engine-p3",
    }
    defaults.update(env)
    for key, value in defaults.items():
        monkeypatch.setenv(key, value)
    return KillConfig.from_env()


def seeded_state(tmp_path, name: str, **fields) -> KillState:
    state = KillState(str(tmp_path / "state.json")).load()
    acct = state.account(name)
    acct.update(fields)
    state.save()
    return state


KS_TEST_NOW = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)


def seed_engine_db(tmp_path, name: str, equity, minutes_ago: float = 1.0,
                   peak=None, now: datetime | None = None) -> str:
    """A real sqlite file shaped like the engine's own DB, carrying the
    per-cycle equity snapshot the engine stamps (ISSUE-010 phase 2) —
    the supervisor's ONLY equity/heartbeat source since the bridge stop
    being one. Freshness is relative to the fixed test clock so trigger
    tests are deterministic."""
    now = now or KS_TEST_NOW
    path = tmp_path / f"oracle_{name.lower()}.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS accounts ("
        "id INTEGER PRIMARY KEY, name TEXT UNIQUE, balance REAL, leverage INTEGER,"
        "bridge_host TEXT, bridge_port INTEGER, signal_group TEXT, is_active BOOLEAN,"
        "created_at TEXT, current_equity REAL, equity_ts TEXT, peak_equity REAL)"
    )
    ts = (now - timedelta(minutes=minutes_ago)).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO accounts "
        "(id, name, balance, leverage, bridge_host, bridge_port, signal_group,"
        " current_equity, equity_ts, peak_equity) "
        "VALUES (1, ?, 0, 500, 'x', 8001, 'g', ?, ?, ?)",
        (name, equity, ts, peak),
    )
    conn.commit()
    conn.close()
    return str(path)


def ks_db_env(paths_by_name: dict) -> dict:
    """KILL_DB_PATH_<name> env wiring for make_config(**env)."""
    return {f"KILL_DB_PATH_{name}": path
            for name, path in paths_by_name.items()}


# ─── Observation factory ─────────────────────────────────────────────────


def obs(config: KillConfig, name: str, group=GROUP_DEMO, equity=None,
        mid=None, db_path=None, data_ok=None) -> Observation:
    t = target_by_name(config, name)
    return Observation(
        target=ks.AccountTarget(
            name=name, group=group, bridge_host=t.bridge_host,
            bridge_port=t.bridge_port, db_path=db_path,
            engine_container=t.engine_container,
        ),
        equity=equity, mid=mid, tcp_ok=True,
        data_ok=(equity is not None) if data_ok is None else data_ok,
    )


# ─── Handle safety (the repo lesson, made a hard contract) ───────────────


class TestHandleSafety:
    """The supervisor must NEVER call the bridge's initialize/shutdown —
    both mutate the process-global MT5 handle owned by the engine
    (docker/mt5/mt5_bridge_server.py exposed_initialize/exposed_shutdown).
    A cycling client with shutdown took down fetcher+engine once already."""

    def test_observe_never_touches_handle(self, monkeypatch, tmp_path):
        config = make_config(monkeypatch, tmp_path)
        db = seed_engine_db(tmp_path, "B", equity=1000.0)
        target = ks.AccountTarget(
            name="B", group=GROUP_DEMO, bridge_host="mt5b",
            bridge_port=8001, db_path=db,
            engine_container="oracle-engine-train",
        )
        root = FakeRoot(equity=1000.0, mid=4000.0)
        install_bridge_mock(monkeypatch, {("mt5b", 8001): root})
        install_tcp_probe(monkeypatch)
        ks._observe(target, KS_TEST_NOW, 10)
        assert root.initialize_calls == 0
        assert root.shutdown_calls == 0
        assert root.account_info_calls == 0  # equity is DB-sourced now

    def test_flatten_never_touches_handle(self, monkeypatch):
        root = FakeRoot(equity=1000.0, mid=4000.0, positions=[
            {"ticket": 11, "symbol": "XAUUSD", "volume": 0.01, "type": 0},
        ])
        with HandleSafeBridge("mt5b", 8001) as bridge:
            # connect path is mocked at module level in other tests; here
            # patch it directly so flatten uses the fake
            monkeypatch.setattr(ks.rpyc, "connect",
                                lambda h, p, config=None: FakeConn(root))
            results = bridge.flatten()
        assert len(results) == 1 and results[0]["success"] is True
        assert root.initialize_calls == 0
        assert root.shutdown_calls == 0
        assert root.closed_tickets == [11]

    def test_full_cycle_with_kill_never_touches_handle(
            self, monkeypatch, tmp_path):
        db = seed_engine_db(tmp_path, "B", equity=800.0)
        config = make_config(monkeypatch, tmp_path, KILL_SWITCH_DRY_RUN="0",
                             KILL_DB_PATH_B=db)
        state = seeded_state(
            tmp_path, "B",
            peak_equity=1000.0,
            day={"date": "2026-01-01", "start_equity": 1000.0},
        )
        root = FakeRoot(equity=800.0, mid=4000.0, positions=[
            {"ticket": 7, "symbol": "XAUUSD", "volume": 0.01, "type": 0},
        ])
        install_bridge_mock(monkeypatch, {("mt5b", 8001): root})
        install_tcp_probe(monkeypatch)
        docker = FakeDocker()
        sup = KillSwitchSupervisor(config, state, docker=docker)
        alerts = sup.cycle(now=datetime(2026, 1, 1, 12, tzinfo=timezone.utc))
        assert alerts, "20% DD should have fired"
        assert root.shutdown_calls == 0
        assert root.initialize_calls == 0
        assert root.account_info_calls == 0


# ─── Triggers ────────────────────────────────────────────────────────────


def _base_config(monkeypatch, tmp_path):
    return make_config(monkeypatch, tmp_path)


class TestDailyLossTrigger:
    def test_fires_at_threshold(self, monkeypatch, tmp_path):
        config = _base_config(monkeypatch, tmp_path)
        state = seeded_state(
            tmp_path, "B", day={"date": "2026-01-01", "start_equity": 1000.0}
        )
        now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        obs_map = {"B": obs(config, "B", equity=970.0)}  # exactly 3.0%
        fired = evaluate_triggers(config, state, obs_map, now)
        assert [t.type for _, t in fired] == ["daily_loss"]

    def test_silent_below_threshold(self, monkeypatch, tmp_path):
        config = _base_config(monkeypatch, tmp_path)
        state = seeded_state(
            tmp_path, "B", day={"date": "2026-01-01", "start_equity": 1000.0}
        )
        now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        obs_map = {"B": obs(config, "B", equity=971.0)}  # 2.9%
        assert evaluate_triggers(config, state, obs_map, now) == []

    def test_no_data_never_fires(self, monkeypatch, tmp_path):
        config = _base_config(monkeypatch, tmp_path)
        state = seeded_state(
            tmp_path, "B", day={"date": "2026-01-01", "start_equity": 1000.0}
        )
        now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        obs_map = {"B": obs(config, "B", equity=None, data_ok=False)}
        fired = [t.type for _, t in
                 evaluate_triggers(config, state, obs_map, now)]
        assert "daily_loss" not in fired


class TestDrawdownTrigger:
    def test_fires_at_10pct_from_peak(self, monkeypatch, tmp_path):
        config = _base_config(monkeypatch, tmp_path)
        state = seeded_state(
            tmp_path, "B", peak_equity=1000.0,
            day={"date": "2026-01-01", "start_equity": 1000.0},
        )
        now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        obs_map = {"B": obs(config, "B", equity=900.0)}
        fired = evaluate_triggers(config, state, obs_map, now)
        types = [t.type for _, t in fired]
        assert "dd_from_peak" in types
        assert "daily_loss" in types  # 10% day loss too — independent

    def test_peak_only_ratchets_up(self, monkeypatch, tmp_path):
        config = _base_config(monkeypatch, tmp_path)
        state = seeded_state(tmp_path, "B", peak_equity=1200.0)
        now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        evaluate_triggers(
            config, state,
            {"B": obs(config, "B", equity=1100.0)}, now,
        )
        assert state.account("B")["peak_equity"] == 1200.0  # high-water mark


class TestOrderRateTrigger:
    @staticmethod
    def _db_with_trades(tmp_path, n: int) -> str:
        path = tmp_path / "oracle_train.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE IF NOT EXISTS accounts "
                     "(id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO accounts (id, name) VALUES (2, 'B')")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS live_trades ("
            "id INTEGER PRIMARY KEY, account_id INTEGER, timestamp TEXT,"
            "is_open INTEGER DEFAULT 1)"
        )
        for i in range(n):
            conn.execute(
                "INSERT INTO live_trades (account_id, timestamp) "
                "VALUES (2, datetime('now', '-10 minutes'))"
            )
        conn.commit()
        conn.close()
        return str(path)

    def test_fires_above_20_per_hour(self, monkeypatch, tmp_path):
        config = _base_config(monkeypatch, tmp_path)
        db = self._db_with_trades(tmp_path, 21)
        state = seeded_state(tmp_path, "B", peak_equity=0)
        now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        obs_map = {"B": obs(config, "B", equity=1000.0, db_path=db)}
        fired = [t.type for _, t in
                evaluate_triggers(config, state, obs_map, now)]
        assert "order_rate" in fired

    def test_silent_at_exactly_20(self, monkeypatch, tmp_path):
        config = _base_config(monkeypatch, tmp_path)
        db = self._db_with_trades(tmp_path, 20)
        state = seeded_state(tmp_path, "B", peak_equity=0)
        now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        obs_map = {"B": obs(config, "B", equity=1000.0, db_path=db)}
        fired = [t.type for _, t in
                 evaluate_triggers(config, state, obs_map, now)]
        assert "order_rate" not in fired

    def test_unreadable_db_skips_trigger(self, monkeypatch, tmp_path):
        config = _base_config(monkeypatch, tmp_path)
        state = seeded_state(tmp_path, "B")
        now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        missing = str(tmp_path / "nope.db")
        obs_map = {"B": obs(config, "B", equity=1000.0, db_path=missing)}
        fired = [t.type for _, t in
                 evaluate_triggers(config, state, obs_map, now)]
        assert "order_rate" not in fired
        assert ks.count_trades_last_hour(missing, "B") is None


class TestPriceDeviationTrigger:
    def test_outlier_account_fires(self, monkeypatch, tmp_path):
        config = _base_config(monkeypatch, tmp_path)
        state = KillState(str(tmp_path / "state.json")).load()
        now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        obs_map = {
            "B": obs(config, "B", equity=1000.0, mid=4000.0),
            "C": obs(config, "C", equity=1000.0, mid=4000.0),
            "D": obs(config, "D", equity=1000.0, mid=4050.0),  # +1.25% vs median
        }
        fired = evaluate_triggers(config, state, obs_map, now)
        fired_map = {o.target.name: t.type for o, t in fired}
        assert fired_map.get("D") == "price_dev"
        assert "B" not in fired_map and "C" not in fired_map

    def test_needs_three_reporting_accounts(self, monkeypatch, tmp_path):
        config = _base_config(monkeypatch, tmp_path)
        state = seeded_state(tmp_path, "B", peak_equity=0)
        now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        obs_map = {
            "B": obs(config, "B", equity=1000.0, mid=4000.0),
            "D": obs(config, "D", equity=1000.0, mid=4050.0),
        }
        fired = [t.type for _, t in
                 evaluate_triggers(config, state, obs_map, now)]
        assert "price_dev" not in fired  # 2 mids = no robust reference


class TestConnectivityTrigger:
    def test_fires_after_silence_window(self, monkeypatch, tmp_path):
        config = _base_config(monkeypatch, tmp_path)
        eleven_min_ago = (datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
                          - timedelta(minutes=11)).isoformat()
        state = seeded_state(tmp_path, "B", last_good_data=eleven_min_ago)
        now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        obs_map = {"B": obs(config, "B", equity=None, data_ok=False)}
        fired = [t.type for _, t in
                 evaluate_triggers(config, state, obs_map, now)]
        assert "connectivity" in fired

    def test_silent_inside_window(self, monkeypatch, tmp_path):
        config = _base_config(monkeypatch, tmp_path)
        nine_min_ago = (datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
                        - timedelta(minutes=9)).isoformat()
        state = seeded_state(tmp_path, "B", last_good_data=nine_min_ago)
        now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        obs_map = {"B": obs(config, "B", equity=None, data_ok=False)}
        fired = [t.type for _, t in
                 evaluate_triggers(config, state, obs_map, now)]
        assert "connectivity" not in fired

    def test_fresh_state_anchors_at_boot_not_now(self, monkeypatch, tmp_path):
        """Fresh boot + no data ever → the window counts from boot, so the
        very first cycle must NOT fire (a supervisor that kills on its own
        startup lag would be broken)."""
        config = _base_config(monkeypatch, tmp_path)
        state = KillState(str(tmp_path / "state.json")).load()
        now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        state._boot_time = now
        obs_map = {"B": obs(config, "B", equity=None, data_ok=False)}
        fired = [t.type for _, t in
                 evaluate_triggers(config, state, obs_map, now)]
        assert "connectivity" not in fired


# ─── State persistence / dedup ───────────────────────────────────────────


class TestStatePersistence:
    def _armed(self, monkeypatch, tmp_path, **env):
        config = make_config(monkeypatch, tmp_path, KILL_SWITCH_DRY_RUN="0",
                             **env)
        state = seeded_state(
            tmp_path, "B",
            peak_equity=1000.0,
            day={"date": "2026-01-01", "start_equity": 1000.0},
        )
        return config, state

    def test_trigger_fires_once_across_restart(self, monkeypatch, tmp_path):
        db = seed_engine_db(tmp_path, "B", equity=800.0)
        config, state = self._armed(monkeypatch, tmp_path, KILL_DB_PATH_B=db)
        root = FakeRoot(equity=800.0, mid=4000.0)
        install_bridge_mock(monkeypatch, {("mt5b", 8001): root})
        install_tcp_probe(monkeypatch)
        sent = install_tg(monkeypatch)
        docker = FakeDocker()

        now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        first = KillSwitchSupervisor(config, state, docker=docker)
        alerts = first.cycle(now=now)
        # 20% daily + 20% dd fire together → ONE alert, ONE policy pass
        assert len(alerts) == 1
        assert sorted(alerts[0]["triggers"]) == ["daily_loss", "dd_from_peak"]
        assert len(sent) == 1
        assert docker.stops == ["oracle-engine-train"]

        # supervisor restart, same conditions → nothing re-fires
        state2 = KillState(str(tmp_path / "state.json")).load()
        second = KillSwitchSupervisor(config, state2, docker=docker)
        second.cycle(now=now + timedelta(minutes=1))
        assert len(sent) == 1  # no duplicate alerts
        assert docker.stops == ["oracle-engine-train"]  # no duplicate stop

        # the trigger record itself survived the restart
        triggers = state2.account("B")["triggers"]
        assert "daily_loss" in triggers and "dd_from_peak" in triggers
        assert triggers["daily_loss"]["details"]["equity"] == 800.0

    def test_peak_baseline_recovers_from_state(self, monkeypatch, tmp_path):
        """CAUSAL: equity 895 only trips dd_from_peak if the high-water
        peak=1000 was recovered from the state file. A fresh supervisor
        (lost state) would set peak=895 and stay silent — silence here is
        the smell that persistence broke."""
        db = seed_engine_db(tmp_path, "B", equity=895.0)
        config, state = self._armed(monkeypatch, tmp_path, KILL_DB_PATH_B=db)
        root = FakeRoot(equity=895.0, mid=4000.0)  # 10.5% below persisted peak
        install_bridge_mock(monkeypatch, {("mt5b", 8001): root})
        install_tcp_probe(monkeypatch)
        sent = install_tg(monkeypatch)
        docker = FakeDocker()
        sup = KillSwitchSupervisor(config, state, docker=docker)
        alerts = sup.cycle(now=datetime(2026, 1, 1, 12, tzinfo=timezone.utc))
        assert "dd_from_peak" in alerts[0]["triggers"]
        assert any("dd_from_peak" in m for m in sent)
        # and the peak is still the high-water mark, not pulled down
        assert state.account("B")["peak_equity"] == 1000.0

    def test_reset_clears_account_state(self, monkeypatch, tmp_path):
        config, _ = self._armed(monkeypatch, tmp_path)
        state = seeded_state(tmp_path, "B", peak_equity=900.0)
        state.record_trigger("B", "dd_from_peak", "test", {})
        state.save()
        state.clear_account("B")
        state.save()
        reloaded = KillState(str(tmp_path / "state.json")).load()
        assert reloaded.account("B") == {}

    def test_corrupt_state_starts_fresh_not_dead(self, monkeypatch, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("NOT JSON AT ALL {", encoding="utf-8")
        state = KillState(str(path)).load()
        assert state.data["accounts"] == {}
        # and it still works as a supervisor state
        state.account("B")["peak_equity"] = 1.0
        state.save()
        assert "B" in json.loads(path.read_text())["accounts"]


# ─── Dry run ─────────────────────────────────────────────────────────────


class TestDryRun:
    def test_dry_run_alerts_but_never_acts(self, monkeypatch, tmp_path):
        db = seed_engine_db(tmp_path, "B", equity=800.0)
        config = make_config(monkeypatch, tmp_path, KILL_SWITCH_DRY_RUN="1",
                             KILL_DB_PATH_B=db)
        state = seeded_state(
            tmp_path, "B",
            peak_equity=1000.0, day={"date": "2026-01-01", "start_equity": 1000.0},
        )
        root = FakeRoot(equity=800.0, mid=4000.0, positions=[
            {"ticket": 5, "symbol": "XAUUSD", "volume": 0.01, "type": 0},
        ])
        install_bridge_mock(monkeypatch, {("mt5b", 8001): root})
        install_tcp_probe(monkeypatch)
        sent = install_tg(monkeypatch)
        docker = FakeDocker()

        sup = KillSwitchSupervisor(config, state, docker=docker)
        alerts = sup.cycle(now=datetime(2026, 1, 1, 12, tzinfo=timezone.utc))

        assert alerts, "triggers must still EVALUATE in dry-run"
        assert root.order_calls == []          # no flatten
        assert root.closed_tickets == []
        assert docker.stops == []              # no container stop
        assert sent, "dry-run must still alert"
        assert all("[DRY-RUN]" in m for m in sent)
        # state is still recorded — the VPS dry-run period must show what
        # WOULD have killed
        assert "daily_loss" in state.account("B")["triggers"]

    def test_armed_mode_flattens_and_stops(self, monkeypatch, tmp_path):
        db = seed_engine_db(tmp_path, "B", equity=800.0)
        config = make_config(monkeypatch, tmp_path, KILL_SWITCH_DRY_RUN="0",
                             KILL_DB_PATH_B=db)
        state = seeded_state(
            tmp_path, "B",
            peak_equity=1000.0, day={"date": "2026-01-01", "start_equity": 1000.0},
        )
        root = FakeRoot(equity=800.0, mid=4000.0, positions=[
            {"ticket": 5, "symbol": "XAUUSD", "volume": 0.01, "type": 0},
            {"ticket": 6, "symbol": "XAUUSD", "volume": 0.02, "type": 1},
        ])
        install_bridge_mock(monkeypatch, {("mt5b", 8001): root})
        install_tcp_probe(monkeypatch)
        sent = install_tg(monkeypatch)
        docker = FakeDocker()

        sup = KillSwitchSupervisor(config, state, docker=docker)
        sup.cycle(now=datetime(2026, 1, 1, 12, tzinfo=timezone.utc))

        assert sorted(root.closed_tickets) == [5, 6]
        assert docker.stops == ["oracle-engine-train"]
        assert not any("[DRY-RUN]" in m for m in sent)


# ─── Group policies ──────────────────────────────────────────────────────


class TestGroupPolicy:
    def _armed(self, monkeypatch, tmp_path, equity=800.0, **env):
        # every account's ENGINE DB carries the equity row the supervisor
        # now reads instead of the bridge (ISSUE-010 phase 2)
        dbs = {t: seed_engine_db(tmp_path, t, equity=equity)
               for t in ("A", "B", "C", "D", "P1", "P2", "P3")}
        config = make_config(monkeypatch, tmp_path,
                             KILL_SWITCH_DRY_RUN="0",
                             **ks_db_env(dbs), **env)
        state = KillState(config.state_path).load()
        for t in config.targets:
            acct = state.account(t.name)
            acct["peak_equity"] = 1000.0
            acct["day"] = {"date": "2026-01-01", "start_equity": 1000.0}
        state.save()
        return config, state

    def _roots_for(self, config):
        return {
            (t.bridge_host, t.bridge_port):
                FakeRoot(equity=800.0, mid=4000.0, positions=[
                    {"ticket": 1, "symbol": "XAUUSD", "volume": 0.01, "type": 0},
                ])
            for t in config.targets
        }

    def test_demo_group_flatten_stop_tg(self, monkeypatch, tmp_path):
        config, state = self._armed(monkeypatch, tmp_path)
        install_bridge_mock(monkeypatch, self._roots_for(config))
        install_tcp_probe(monkeypatch)
        sent = install_tg(monkeypatch)
        docker = FakeDocker()
        alerts = KillSwitchSupervisor(config, state, docker=docker).cycle(
            now=datetime(2026, 1, 1, 12, tzinfo=timezone.utc))
        # all six non-real accounts fired, each stopped its engine ONCE
        assert sorted(docker.stops) == [
            "oracle-engine-p1", "oracle-engine-p2", "oracle-engine-p3",
            "oracle-engine-train", "oracle-engine-train", "oracle-engine-train",
        ]
        # demo/cent alerts are the normal kind — URGENT is real-group only
        non_real = [m for m in sent if "Account: A " not in m
                    and "(group real)" not in m]
        assert non_real and all("URGENT" not in m for m in non_real)
        assert len(alerts) == 7  # all 7 accounts fire (A included) — this
        # test pins the demo/cent SIDE of that firing

    def test_real_group_flatten_block_but_never_stop(self, monkeypatch, tmp_path):
        config, state = self._armed(monkeypatch, tmp_path)
        roots = self._roots_for(config)
        real_root = roots[("mt5a", 8001)]
        install_bridge_mock(monkeypatch, roots)
        install_tcp_probe(monkeypatch)
        sent = install_tg(monkeypatch)
        docker = FakeDocker()
        KillSwitchSupervisor(config, state, docker=docker).cycle(
            now=datetime(2026, 1, 1, 12, tzinfo=timezone.utc))

        # Real-A WAS flattened (bridge policy)...
        assert real_root.closed_tickets == [1]
        # ...but its container is never stopped — the stop list contains
        # ONLY the demo/cent engines
        assert sorted(docker.stops) == [
            "oracle-engine-p1", "oracle-engine-p2", "oracle-engine-p3",
            "oracle-engine-train", "oracle-engine-train", "oracle-engine-train",
        ]
        # the alert is the URGENT one and says the container is untouched
        real_alerts = [m for m in sent if "Account: A" in m]
        assert len(real_alerts) == 1
        assert "URGENT" in real_alerts[0]
        assert "iron rule" in real_alerts[0].lower()

    def test_real_engine_container_env_refused_at_boot(self, monkeypatch, tmp_path):
        with pytest.raises(ValueError, match="REAL-group"):
            make_config(monkeypatch, tmp_path,
                        KILL_ENGINE_CONTAINER_A="oracle-engine")

    def test_blocked_real_account_is_enforced_every_cycle(
            self, monkeypatch, tmp_path):
        """Armed + REAL already killed: the engine keeps running (iron rule)
        and may re-enter — the supervisor silently re-flattens new positions
        without re-alarming (the block, not the alarm)."""
        config, state = self._armed(monkeypatch, tmp_path)
        state.record_trigger("A", "dd_from_peak", "already killed", {})
        state.save()
        roots = self._roots_for(config)
        real_root = roots[("mt5a", 8001)]
        for t in config.targets:  # equity back at peak → no NEW trigger fires
            seed_engine_db(tmp_path, t.name, equity=1000.0)
        real_root.positions = [
            {"ticket": 99, "symbol": "XAUUSDm", "volume": 0.01, "type": 0},
        ]
        install_bridge_mock(monkeypatch, roots)
        install_tcp_probe(monkeypatch)
        sent = install_tg(monkeypatch)
        docker = FakeDocker()
        sup = KillSwitchSupervisor(config, state, docker=docker)
        sup.cycle(now=datetime(2026, 1, 1, 12, tzinfo=timezone.utc))
        assert real_root.closed_tickets == [99]   # new position flattened
        assert sent == []                          # no re-alarm
        assert docker.stops == []                   # still never stopped

    def test_blocked_demo_account_enforcement_after_stop(
            self, monkeypatch, tmp_path):
        """Demo/cent enforcement also holds — a stopped engine whose bridge
        still serves positions (leftovers/strays) gets them flattened."""
        config, state = self._armed(monkeypatch, tmp_path)
        state.record_trigger("B", "daily_loss", "killed earlier", {})
        state.save()
        roots = self._roots_for(config)
        for t in config.targets:
            seed_engine_db(tmp_path, t.name, equity=1000.0)
        roots[("mt5b", 8001)].positions = [
            {"ticket": 42, "symbol": "XAUUSD", "volume": 0.01, "type": 0},
        ]
        install_bridge_mock(monkeypatch, roots)
        install_tcp_probe(monkeypatch)
        install_tg(monkeypatch)
        docker = FakeDocker()
        KillSwitchSupervisor(config, state, docker=docker).cycle(
            now=datetime(2026, 1, 1, 12, tzinfo=timezone.utc))
        assert roots[("mt5b", 8001)].closed_tickets == [42]


# ─── Config parsing (fail-closed) ────────────────────────────────────────


class TestConfigParsing:
    def test_defaults(self, monkeypatch, tmp_path):
        config = make_config(monkeypatch, tmp_path)
        assert config.dry_run is True  # bare env must never arm a kill switch
        assert [t.name for t in config.targets if t.group == GROUP_DEMO] \
            == ["B", "C", "D"]
        assert [t.name for t in config.targets if t.group == GROUP_CENT] \
            == ["P1", "P2", "P3"]
        assert [t.name for t in config.targets if t.group == GROUP_REAL] == ["A"]
        assert config.daily_loss_pct == 3.0
        assert config.dd_pct == 10.0
        assert config.order_rate_per_hour == 20
        assert config.price_dev_pct == 1.0
        assert config.connectivity_minutes == 10
        assert config.poll_seconds == 30

    def test_bridge_defaults_follow_compose_network(self, monkeypatch, tmp_path):
        config = make_config(monkeypatch, tmp_path)
        t = target_by_name(config, "P2")
        assert (t.bridge_host, t.bridge_port) == ("mt5p2", 8001)

    def test_malformed_number_fails_boot(self, monkeypatch, tmp_path):
        with pytest.raises(ValueError, match="KILL_DAILY_LOSS_PCT"):
            make_config(monkeypatch, tmp_path, KILL_DAILY_LOSS_PCT="three")

    def test_malformed_dry_run_value_fails_boot(self, monkeypatch, tmp_path):
        with pytest.raises(ValueError, match="KILL_SWITCH_DRY_RUN"):
            make_config(monkeypatch, tmp_path, KILL_SWITCH_DRY_RUN="maybe")

    def test_duplicate_account_across_groups_fails_boot(
            self, monkeypatch, tmp_path):
        with pytest.raises(ValueError, match="more than one kill group"):
            make_config(monkeypatch, tmp_path, KILL_REAL_ACCOUNTS="B,A")

    def test_empty_group_is_allowed(self, monkeypatch, tmp_path):
        config = make_config(monkeypatch, tmp_path, KILL_REAL_ACCOUNTS="")
        assert [t.name for t in config.targets if t.group == GROUP_REAL] == []


# ─── CLI ──────────────────────────────────────────────────────────────────


class TestCli:
    def test_once_runs_single_cycle(self, monkeypatch, tmp_path, caplog):
        make_config(monkeypatch, tmp_path)
        root = FakeRoot(equity=1000.0, mid=4000.0)
        install_bridge_mock(monkeypatch, {("mt5b", 8001): root})
        install_tcp_probe(monkeypatch)
        install_tg(monkeypatch)
        argv = ["--once", "--state", str(tmp_path / "state.json")]
        assert ks.main(argv) == 0
        assert (tmp_path / "state.json").exists()

    def test_reset_clears_account(self, monkeypatch, tmp_path):
        make_config(monkeypatch, tmp_path)
        state = seeded_state(tmp_path, "B", peak_equity=123.0)
        state.record_trigger("B", "dd_from_peak", "x", {})
        state.save()
        assert ks.main(["--reset", "b", "--state",
                        str(tmp_path / "state.json")]) == 0
        fresh = KillState(str(tmp_path / "state.json")).load()
        assert fresh.account("B") == {}

    def test_dry_run_flag_overrides_env(self, monkeypatch, tmp_path):
        """--no-dry-run on the CLI must arm even when env says dry-run
        (operator's explicit one-shot arming for a controlled test).
        Since the phase-2 boot guard, arming requires a readable engine
        DB — the real compose wires KILL_DB_PATH_* for every account."""
        db = seed_engine_db(tmp_path, "B", equity=1000.0)
        make_config(monkeypatch, tmp_path, KILL_SWITCH_DRY_RUN="1",
                    KILL_DB_PATH_B=db)
        argv = ["--once", "--no-dry-run", "--state",
                str(tmp_path / "state.json")]
        monkeypatch.setattr(
            ks.KillSwitchSupervisor, "cycle",
            lambda self, now=None: [],
        )
        assert ks.main(argv) == 0


# ─── Flatten fail-loud (retro งาน 4 follow-up, คุณหมอผ่าน Hermes 17/11) ───


class DeadRoot(FakeRoot):
    """Bridge-side failure: positions_get raises — the read path is broken,
    but account_info still answers (transient/partial bridge failures are
    exactly how this looks in production)."""

    def positions_get(self, symbol=None, ticket=None):
        raise RuntimeError("bridge read failed")


class TestFlattenFailLoud:
    """positions() returns None on bridge failure, [] when really flat.
    flatten()'s old `if not positions:` treated BOTH as "nothing to close"
    and returned [] — so a kill that fired mid-bridge-failure reported
    'flatten: 0 closed, 0 failed' while real positions may still be open:
    the supervisor BELIEVED the account flat without ever reading it.
    None must be fail-loud (read-failed, NOT confirmed flat); only [] is
    allowed to mean 'already flat'."""

    def test_flatten_returns_none_when_positions_unreadable(self, monkeypatch):
        root = DeadRoot(equity=800.0, mid=4000.0, positions=[
            {"ticket": 5, "symbol": "XAUUSD", "volume": 0.01, "type": 0},
        ])
        install_bridge_mock(monkeypatch, {("mt5b", 8001): root})
        with HandleSafeBridge("mt5b", 8001) as bridge:
            assert bridge.positions() is None      # read-failed contract
            results = bridge.flatten()
        assert results is None, (
            "bridge read-failure must NOT be flattened into a fake "
            f"'all closed' result (got {results!r})"
        )
        assert root.order_calls == []  # nothing was closed — and none claimed

    def test_flatten_empty_positions_still_returns_empty_list(self, monkeypatch):
        """Shape guard against over-blocking: no positions reported AND the
        read succeeded → [] really is 'already flat', not a failure."""
        root = FakeRoot(equity=800.0, mid=4000.0, positions=[])
        install_bridge_mock(monkeypatch, {("mt5b", 8001): root})
        with HandleSafeBridge("mt5b", 8001) as bridge:
            assert bridge.positions() == []
            results = bridge.flatten()
        assert results == []

    def test_supervisor_reports_read_failed_not_silent_flat(
            self, monkeypatch, tmp_path):
        """Armed kill fires while the bridge cannot report positions → the
        action/alert must say READ-FAILED (manual check required), never
        the old silent '0 closed, 0 failed'."""
        db = seed_engine_db(tmp_path, "B", equity=800.0)
        config = make_config(monkeypatch, tmp_path, KILL_SWITCH_DRY_RUN="0",
                             KILL_DB_PATH_B=db)
        state = seeded_state(
            tmp_path, "B",
            peak_equity=1000.0, day={"date": "2026-01-01", "start_equity": 1000.0},
        )
        root = DeadRoot(equity=800.0, mid=4000.0)  # daily-loss fires
        install_bridge_mock(monkeypatch, {("mt5b", 8001): root})
        install_tcp_probe(monkeypatch)
        sent = install_tg(monkeypatch)
        docker = FakeDocker()

        sup = KillSwitchSupervisor(config, state, docker=docker)
        sup.cycle(now=datetime(2026, 1, 1, 12, tzinfo=timezone.utc))

        joined = "\n".join(sent)
        assert "READ-FAILED" in joined, (
            "the alert must carry the read-failure, not a flat claim"
        )
        assert "0 closed" not in joined, (
            "a bridge read-failure must never be reported as a clean "
            "'0 closed, 0 failed' flatten"
        )
        assert "flat" in joined.lower()
        # stopping the engine container is independent of the read — it
        # must still happen (the engine is the position SOURCE)
        assert docker.stops == ["oracle-engine-train"]

    def test_real_group_read_failed_still_says_manual_intervention(
            self, monkeypatch, tmp_path):
        """REAL group: flatten read-failed → the URGENT alert must demand
        MANUAL intervention (the block is enforced, but 'is it flat?' is
        UNKNOWN — the operator must look at the terminal)."""
        db = seed_engine_db(tmp_path, "A", equity=800.0)
        config = make_config(monkeypatch, tmp_path, KILL_SWITCH_DRY_RUN="0",
                             KILL_DB_PATH_A=db)
        state = seeded_state(
            tmp_path, "A",
            peak_equity=1000.0, day={"date": "2026-01-01", "start_equity": 1000.0},
        )
        root = DeadRoot(equity=800.0, mid=4000.0)
        install_bridge_mock(monkeypatch, {("mt5a", 8001): root})
        install_tcp_probe(monkeypatch)
        sent = install_tg(monkeypatch)
        docker = FakeDocker()

        KillSwitchSupervisor(config, state, docker=docker).cycle(
            now=datetime(2026, 1, 1, 12, tzinfo=timezone.utc))

        joined = "\n".join(sent)
        assert "URGENT" in joined                    # real group stays urgent
        assert "READ-FAILED" in joined
        assert "manual" in joined.lower()           # demands human eyes
        assert docker.stops == []                   # iron rule: never stopped
        assert root.order_calls == []               # nothing was claimed closed

    def test_blocked_enforcement_read_failed_logs_not_claims_flat(
            self, monkeypatch, tmp_path, caplog):
        """BLOCK-enforcement on a read-failed bridge: no 'flattened' claim,
        a warning that verification failed — silence here would re-create
        the same 'believed flat' failure on every later cycle."""
        import logging

        db = seed_engine_db(tmp_path, "B", equity=1000.0)
        config = make_config(monkeypatch, tmp_path, KILL_SWITCH_DRY_RUN="0",
                             KILL_DB_PATH_B=db)
        state = seeded_state(tmp_path, "B", peak_equity=1000.0,
                            day={"date": "2026-01-01", "start_equity": 1000.0})
        state.record_trigger("B", "daily_loss", "killed earlier", {})
        state.save()
        root = DeadRoot(equity=1000.0, mid=4000.0)  # no NEW trigger fires
        install_bridge_mock(monkeypatch, {("mt5b", 8001): root})
        install_tcp_probe(monkeypatch)
        install_tg(monkeypatch)
        docker = FakeDocker()

        with caplog.at_level(logging.WARNING, logger="scripts.kill_switch"):
            KillSwitchSupervisor(config, state, docker=docker).cycle(
                now=datetime(2026, 1, 1, 12, tzinfo=timezone.utc))

        joined = caplog.text
        assert "read-failed" in joined.lower(), (
            "enforcement on a read-failed bridge must log the failed "
            "verification, not pass silently"
        )
        assert "flattened" not in joined.lower()
        assert root.order_calls == []
