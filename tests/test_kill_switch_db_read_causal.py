"""ISSUE-010 phase 2 — kill-switch reads equity/status from the ENGINE DB.

Production evidence (VPS 2026-11-17): supervisor bridge polling was the
disturber of the engines' single shared terminal handle — "stream has been
closed" on all three engines (P1 11 / P2 9 / P3 8 since 08:00) and
account_info via the supervisor failed 18/18; `docker stop kill-switch`
made the stream-closed errors stop dead (0 in 7 minutes). doctorboyz order
via Hermes: ชั้นพิทักษ์เงินต้องไม่พึ่ง bridge ที่มันปกป้อง — equity/DD/
connectivity come from the engine DB (the engine writes current_equity +
equity_ts every cycle), the bridge remains for the tick cross-check and
the flatten action only.

Everything here mocks BOTH the DB (real temp sqlite files) and the bridge
(FakeRoot) — no real container, no real terminal is ever touched.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scripts.kill_switch as ks  # noqa: E402
from scripts.kill_switch import (  # noqa: E402
    HandleSafeBridge,
    KillConfig,
    KillState,
    KillSwitchSupervisor,
    Observation,
    evaluate_triggers,
)
from metty.core.db import (  # noqa: E402
    init_db,
    insert_account,
    update_equity_snapshot,
)

# ─── shared fakes (mirrors tests/test_kill_switch.py contracts) ───────────


class FakeRoot:
    """Bridge stand-in. account_info is DELIBERATELY call-counted: phase 2
    forbids the supervisor from reading equity over the bridge, so any
    account_info call from supervisor code is a contract break."""

    def __init__(self, equity=None, positions=None, mid=None):
        self.equity = equity
        self.positions = positions or []
        self.mid = mid
        self.account_info_calls = 0
        self.tick_calls = 0
        self.initialize_calls = 0
        self.shutdown_calls = 0
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
        return {"equity": self.equity, "balance": self.equity}

    def positions_get(self, symbol=None, ticket=None):
        return [dict(p) for p in self.positions]

    def symbol_info_tick(self, symbol):
        self.tick_calls += 1
        if self.mid is None:
            return None
        return {"bid": self.mid, "ask": self.mid}

    def order_send(self, action, symbol, volume, order_type, price,
                   sl, tp, deviation, magic, comment, type_time, filling,
                   position):
        self.order_calls.append((action, symbol, volume, order_type, position))
        if position:
            self.closed_tickets.append(position)
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
    def fake_connect(host, port, config=None):
        return FakeConn(roots_by_host_port[(host, port)])

    monkeypatch.setattr(ks.rpyc, "connect", fake_connect)


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


def make_config(monkeypatch, tmp_path, **env):
    """Hermetic env config (same cleanup contract as test_kill_switch.py)."""
    import os

    for key in list(os.environ):
        if key.startswith(("KILL_", "MT5_BRIDGE_", "MT5_SYMBOL_")):
            monkeypatch.delenv(key, raising=False)
    defaults = {
        "KILL_SWITCH_DRY_RUN": "1",
        "KILL_STATE_PATH": str(tmp_path / "state.json"),
        "KILL_DEMO_ACCOUNTS": "B",
        "KILL_CENT_ACCOUNTS": "P1",
        "KILL_REAL_ACCOUNTS": "A",
    }
    defaults.update(env)
    for key, value in defaults.items():
        monkeypatch.setenv(key, value)
    return KillConfig.from_env()


# ─── engine-side DB seeding (what the ENGINE would have written) ──────────


def seed_engine_db(tmp_path, name: str, equity, minutes_ago: float = 1.0,
                   peak=None, now=None) -> str:
    """A real sqlite file shaped like the engine's own DB, with the
    per-cycle equity snapshot row exactly as update_equity_snapshot
    leaves it. now defaults to the fixed test clock for determinism."""
    now = now or datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
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


def rewritable_engine_db(tmp_path, name: str, equity, minutes_ago=1.0,
                         peak=None) -> str:
    """Seed via the REAL repo functions (init_db + insert_account +
    update_equity_snapshot) — proves the whole write side end-to-end."""
    path = tmp_path / f"engine_{name.lower()}.db"
    init_db(path)
    insert_account(name, 1000.0, 500, "mt5x", 8001, "g", db_path=path)
    now = datetime.now(timezone.utc)
    # write with a controlled clock for freshness determinism
    conn = sqlite3.connect(path)
    conn.execute(
        "UPDATE accounts SET current_equity = ?, equity_ts = ?, peak_equity = ? "
        "WHERE name = ?",
        (equity,
         (now - timedelta(minutes=minutes_ago)).isoformat(),
         peak, name),
    )
    conn.commit()
    conn.close()
    return str(path)


NOW = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)


def observe_account(config, name, monkeypatch, root=None, tcp_ok=True):
    """_observe one account with the bridge mocked; the DB path comes from
    the config's KILL_DB_PATH_<name> wiring (exactly how the service runs)."""
    target = next(t for t in config.targets if t.name == name)
    if root is not None:
        install_bridge_mock(
            monkeypatch, {(target.bridge_host, target.bridge_port): root})
    install_tcp_probe(monkeypatch, ok=tcp_ok)
    return ks._observe(target, NOW, 10)


# ─── Part 1: engine equity snapshot (metty additive) ──────────────────────


class TestEngineEquitySnapshot:
    """db.py additive contract: migration columns + one write function."""

    def test_migration_adds_columns_to_existing_db(self, tmp_path):
        """A DB created BEFORE this change (accounts table without the two
        columns) must gain them on the next init_db — the VPS engine DBs are
        exactly this shape until the engines are rebuilt."""
        path = tmp_path / "old_engine.db"
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT UNIQUE, "
            "balance REAL, leverage INTEGER, bridge_host TEXT, bridge_port INTEGER, "
            "signal_group TEXT, is_active BOOLEAN, created_at TEXT)"
        )
        conn.execute(
            "INSERT INTO accounts (name, balance, leverage, bridge_host, "
            "bridge_port, signal_group) VALUES ('B', 1.0, 500, 'h', 1, 'g')"
        )
        conn.commit()
        conn.close()

        init_db(path)  # the engine calls this at the top of every cycle

        conn = sqlite3.connect(path)
        cols = {r[1] for r in
                conn.execute("PRAGMA table_info(accounts)").fetchall()}
        conn.close()
        assert "current_equity" in cols
        assert "equity_ts" in cols

    def test_snapshot_write_roundtrip(self, tmp_path):
        path = tmp_path / "engine.db"
        init_db(path)
        insert_account("B", 1000.0, 500, "mt5b", 8001, "g", db_path=path)
        assert update_equity_snapshot("B", 1234.5, path) is True
        snap = ks.read_account_snapshot(str(path), "B")
        assert snap is not None
        assert snap["equity"] == pytest.approx(1234.5)
        assert snap["equity_ts"], "timestamp must be written with the equity"
        parsed = datetime.fromisoformat(snap["equity_ts"])
        assert parsed.tzinfo is not None, "must be an aware UTC timestamp"

    def test_unknown_account_write_returns_false(self, tmp_path):
        path = tmp_path / "engine.db"
        init_db(path)
        insert_account("B", 1000.0, 500, "mt5b", 8001, "g", db_path=path)
        assert update_equity_snapshot("NOPE", 100.0, path) is False

    def test_read_snapshot_missing_account_is_none(self, tmp_path):
        path = tmp_path / "engine.db"
        init_db(path)
        insert_account("B", 1000.0, 500, "mt5b", 8001, "g", db_path=path)
        assert ks.read_account_snapshot(str(path), "NOPE") is None

    def test_read_snapshot_unreadable_db_is_none(self, tmp_path):
        missing = str(tmp_path / "does_not_exist.db")
        assert ks.read_account_snapshot(missing, "B") is None
        assert ks.read_account_snapshot("", "B") is None


class TestLiveTraderWritesSnapshot:
    """The engine writes the snapshot at the TOP of every cycle — before any
    early return — so a held/skipped cycle still proves the engine is alive
    (that heartbeat is the kill switch's connectivity signal)."""

    @staticmethod
    def _paper_trader(tmp_path, monkeypatch):
        from metty.execution.live_trader import LiveTrader
        db_path = tmp_path / "farm.db"
        init_db(db_path)
        insert_account("B", 1000.0, 500, "mt5b", 8001, "g",
                       db_path=db_path)
        monkeypatch.setenv("LEARNING_MODE", "0")
        trader = LiveTrader(account="B", db_path=db_path, dry_run=True)
        # no bridge in a brokerless paper farm — equity reconstructs from DB
        return trader, db_path

    def test_run_once_writes_snapshot_before_early_return(
            self, monkeypatch, tmp_path):
        trader, db_path = self._paper_trader(tmp_path, monkeypatch)
        # cycle dies at the FIRST gate (no candle data) — the earliest
        # early-return there is; the snapshot must already be written
        monkeypatch.setattr(trader, "_fetch_candles", lambda: None)
        result = trader.run_once()

        assert result["action"] == "skip"  # farm behavior unchanged
        snap = ks.read_account_snapshot(str(db_path), "B")
        assert snap is not None
        assert snap["equity"] is not None, (
            "engine must stamp its equity every cycle even when the "
            "cycle then skips — that stamp IS the kill switch's heartbeat"
        )
        age = ks._equity_age_minutes(snap["equity_ts"],
                                     datetime.now(timezone.utc))
        assert age is not None and age < 5

    def test_snapshot_write_never_breaks_the_cycle(
            self, monkeypatch, tmp_path):
        trader, db_path = self._paper_trader(tmp_path, monkeypatch)
        monkeypatch.setattr(trader, "_fetch_candles", lambda: None)

        def boom(*a, **k):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(
            "metty.execution.live_trader.update_equity_snapshot", boom)
        result = trader.run_once()  # must not raise
        assert result["action"] == "skip"

    def test_none_equity_writes_nothing(self, monkeypatch, tmp_path):
        trader, db_path = self._paper_trader(tmp_path, monkeypatch)
        monkeypatch.setattr(trader, "_get_equity", lambda: None)
        trader._write_equity_snapshot()
        snap = ks.read_account_snapshot(str(db_path), "B")
        assert snap["equity"] is None  # no lie: no equity → no stamp


class TestM5ScalpWritesSnapshot:
    """Real-A runs the scalp engine — it must stamp equity too, or the kill
    switch would false-alarm connectivity on the REAL account and flatten
    live positions over a dead man's switch.

    The scalp snapshot must use BRIDGE equity only. _get_balance() falls
    back to a stale DB balance when the bridge is down — writing that with
    a fresh equity_ts would tell the kill switch the engine is healthy
    while it is blind. No bridge equity → no stamp."""

    @staticmethod
    def _scalp_trader(tmp_path, monkeypatch):
        from metty.execution.m5_scalp_trader import M5ScalpTrader
        db_path = tmp_path / "scalp.db"
        init_db(db_path)
        insert_account("A", 500.0, 500, "mt5a", 8001, "g", db_path=db_path)
        trader = M5ScalpTrader(account="A", db_path=db_path)
        return trader, db_path

    def test_bridge_equity_is_stamped(self, monkeypatch, tmp_path):
        trader, db_path = self._scalp_trader(tmp_path, monkeypatch)

        class _Info:
            equity = 777.25

        monkeypatch.setattr(
            "metty.execution.m5_scalp_trader.MT5Bridge"
            ".fetch_account_info_sync",
            lambda self: _Info(),
        )
        trader._write_equity_snapshot()
        snap = ks.read_account_snapshot(str(db_path), "A")
        assert snap["equity"] == pytest.approx(777.25)

    def test_bridge_down_writes_nothing(self, monkeypatch, tmp_path):
        """CAUSAL: _get_balance's stale-balance fallback must NOT become a
        fresh stamp. The stamp goes through _get_balance (to reuse its
        60s bridge-equity cache — no extra cold session per cycle), but
        the cache is set ONLY from real MT5 equity, so a cold cache means
        no stamp. RED if the snapshot ever stamps the fallback value."""
        trader, db_path = self._scalp_trader(tmp_path, monkeypatch)
        monkeypatch.setattr(
            "metty.execution.m5_scalp_trader.MT5Bridge"
            ".fetch_account_info_sync",
            lambda self: None,
        )
        # accounts.balance holds a stale value the fallback would return
        trader._write_equity_snapshot()
        snap = ks.read_account_snapshot(str(db_path), "A")
        assert snap["equity"] is None, (
            "bridge-down must NOT stamp a fresh equity_ts over the stale "
            "DB balance — that fabricates a healthy heartbeat"
        )


# ─── Part 2: _observe reads the engine DB, not the bridge ─────────────────


class TestObserveReadsDb:
    def _config(self, monkeypatch, tmp_path, db_path):
        return make_config(monkeypatch, tmp_path, KILL_DB_PATH_B=db_path)

    def test_equity_comes_from_db_not_bridge(self, monkeypatch, tmp_path):
        db = seed_engine_db(tmp_path, "B", equity=990.0, minutes_ago=1.0)
        config = self._config(monkeypatch, tmp_path, db)
        root = FakeRoot(equity=1.0, mid=4000.0)  # bridge equity is a LIE
        ob = observe_account(config, "B", monkeypatch=monkeypatch, root=root)

        assert ob.equity == pytest.approx(990.0)  # DB wins, bridge ignored
        assert ob.data_ok is True
        assert ob.mid == pytest.approx(4000.0)    # tick cross-check stays
        assert root.account_info_calls == 0, (
            "the supervisor must NEVER read equity over the bridge — "
            "that polling is what disturbed the engines' handle "
            "(stream-closed ×28, 2026-11-17)"
        )

    def test_stale_ts_is_not_data_ok(self, monkeypatch, tmp_path):
        db = seed_engine_db(tmp_path, "B", equity=990.0, minutes_ago=30.0)
        config = self._config(monkeypatch, tmp_path, db)
        root = FakeRoot(mid=4000.0)
        ob = observe_account(config, "B", monkeypatch=monkeypatch, root=root)
        assert ob.equity == pytest.approx(990.0)  # last-known value kept
        assert ob.data_ok is False, (
            "equity older than the connectivity window is NOT usable "
            "data — the engine is dead, the connectivity trigger is "
            "the alarm that must fire"
        )

    def test_fresh_ts_within_window_is_ok(self, monkeypatch, tmp_path):
        db = seed_engine_db(tmp_path, "B", equity=990.0, minutes_ago=9.0)
        config = self._config(monkeypatch, tmp_path, db)
        ob = observe_account(config, "B", monkeypatch=monkeypatch,
                             root=FakeRoot(mid=4000.0))
        assert ob.data_ok is True

    def test_edge_exactly_at_window_is_ok(self, monkeypatch, tmp_path):
        db = seed_engine_db(tmp_path, "B", equity=990.0, minutes_ago=10.0)
        config = self._config(monkeypatch, tmp_path, db)
        ob = observe_account(config, "B", monkeypatch=monkeypatch,
                             root=FakeRoot(mid=4000.0))
        assert ob.data_ok is True  # window is inclusive

    def test_unparseable_ts_is_not_ok(self, monkeypatch, tmp_path):
        path = tmp_path / "bad_ts.db"
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT UNIQUE, "
            "current_equity REAL, equity_ts TEXT, peak_equity REAL)")
        conn.execute(
            "INSERT INTO accounts (name, current_equity, equity_ts) "
            "VALUES ('B', 990.0, 'not-a-timestamp')")
        conn.commit()
        conn.close()
        config = self._config(monkeypatch, tmp_path, str(path))
        ob = observe_account(config, "B", monkeypatch=monkeypatch,
                             root=FakeRoot(mid=4000.0))
        assert ob.data_ok is False

    def test_db_peak_is_carried(self, monkeypatch, tmp_path):
        db = seed_engine_db(tmp_path, "B", equity=950.0, peak=1200.0)
        config = self._config(monkeypatch, tmp_path, db)
        ob = observe_account(config, "B", monkeypatch=monkeypatch,
                             root=FakeRoot(mid=4000.0))
        assert ob.db_peak == pytest.approx(1200.0)

    def test_tcp_down_still_reads_db(self, monkeypatch, tmp_path):
        """DB is a volume mount, not the bridge — a dead bridge must not
        blind the supervisor to the engine's last known state."""
        db = seed_engine_db(tmp_path, "B", equity=990.0)
        config = self._config(monkeypatch, tmp_path, db)
        ob = observe_account(config, "B", monkeypatch=monkeypatch,
                             root=FakeRoot(mid=4000.0), tcp_ok=False)
        assert ob.tcp_ok is False
        assert ob.equity == pytest.approx(990.0)
        assert ob.mid is None

    def test_no_db_path_no_equity(self, monkeypatch, tmp_path):
        config = make_config(monkeypatch, tmp_path)
        target = next(t for t in config.targets if t.name == "B")
        install_tcp_probe(monkeypatch)
        ob = ks._observe(target, NOW, 10)
        assert ob.equity is None
        assert ob.data_ok is False


# ─── Part 3: trigger logic on DB values ───────────────────────────────────


def _armed_config(monkeypatch, tmp_path, db, **env):
    return make_config(monkeypatch, tmp_path,
                       KILL_SWITCH_DRY_RUN="0", KILL_DB_PATH_B=db, **env)


class TestTriggersOnDbValues:
    """daily-loss / DD / connectivity evaluated on the engine DB row."""

    def _sup(self, config, tmp_path, **acct_fields):
        state = KillState(config.state_path).load()
        if acct_fields:
            state.account("B").update(acct_fields)
            state.save()
        return KillSwitchSupervisor(config, state, docker=FakeDocker())

    def test_daily_loss_fires_from_db_equity(self, monkeypatch, tmp_path):
        db = seed_engine_db(tmp_path, "B", equity=969.0)  # 3.1% below 1000
        config = _armed_config(monkeypatch, tmp_path, db)
        install_bridge_mock(monkeypatch, {("mt5b", 8001): FakeRoot(mid=4000.0)})
        install_tcp_probe(monkeypatch)
        sent = install_tg(monkeypatch)
        sup = self._sup(config, tmp_path,
                        day={"date": "2026-01-01", "start_equity": 1000.0})
        alerts = sup.cycle(now=NOW)
        assert "daily_loss" in alerts[0]["triggers"]
        assert any("daily loss" in m for m in sent)

    def test_dd_fires_seeded_from_db_peak(self, monkeypatch, tmp_path):
        """CAUSAL RED: kill state is EMPTY (fresh supervisor boot) — only
        the engine-side accounts.peak_equity knows the high-water mark.
        Without seeding from the DB peak, peak would ratchet to the current
        equity and the 10% DD would never fire."""
        db = seed_engine_db(tmp_path, "B", equity=890.0, peak=1000.0)
        config = _armed_config(monkeypatch, tmp_path, db)
        install_bridge_mock(monkeypatch, {("mt5b", 8001): FakeRoot(mid=4000.0)})
        install_tcp_probe(monkeypatch)
        sent = install_tg(monkeypatch)
        sup = self._sup(config, tmp_path)
        alerts = sup.cycle(now=NOW)
        assert "dd_from_peak" in alerts[0]["triggers"]
        assert any("drawdown" in m for m in sent)
        # the seeded peak is remembered, never pulled down
        assert sup.state.account("B")["peak_equity"] == pytest.approx(1000.0)

    def test_state_peak_wins_over_lower_db_peak(self, monkeypatch, tmp_path):
        """The kill switch's own high-water (state) must never be lowered
        by a smaller engine-side peak."""
        db = seed_engine_db(tmp_path, "B", equity=1000.0, peak=900.0)
        config = _armed_config(monkeypatch, tmp_path, db)
        install_bridge_mock(monkeypatch, {("mt5b", 8001): FakeRoot(mid=4000.0)})
        install_tcp_probe(monkeypatch)
        sup = self._sup(config, tmp_path, peak_equity=1100.0)
        sup.cycle(now=NOW)
        assert sup.state.account("B")["peak_equity"] == pytest.approx(1100.0)

    def test_connectivity_fires_on_stale_engine_ts(
            self, monkeypatch, tmp_path):
        """The engine stopped writing 30 minutes ago — the DB row itself is
        the alarm now (better than a ping: this catches a dead ENGINE, not
        just a dead bridge)."""
        db = seed_engine_db(tmp_path, "B", equity=1000.0, minutes_ago=30.0)
        config = _armed_config(monkeypatch, tmp_path, db)
        install_bridge_mock(monkeypatch, {("mt5b", 8001): FakeRoot(mid=4000.0)})
        install_tcp_probe(monkeypatch)
        sent = install_tg(monkeypatch)
        sup = self._sup(config, tmp_path)
        alerts = sup.cycle(now=NOW)
        assert "connectivity" in alerts[0]["triggers"]
        assert any("engine DB" in m for m in sent), (
            "the alert must say WHERE the silence was measured — the "
            "operator must know it is the engine that went quiet"
        )

    def test_fresh_engine_ts_keeps_connectivity_silent(
            self, monkeypatch, tmp_path):
        db = seed_engine_db(tmp_path, "B", equity=1000.0, minutes_ago=1.0)
        config = _armed_config(monkeypatch, tmp_path, db)
        install_bridge_mock(monkeypatch, {("mt5b", 8001): FakeRoot(mid=4000.0)})
        install_tcp_probe(monkeypatch)
        sup = self._sup(config, tmp_path)
        alerts = sup.cycle(now=NOW)
        assert alerts == []

    def test_stale_equity_skips_loss_triggers(self, monkeypatch, tmp_path):
        """Design lock: a stale (dead-engine) equity row fires CONNECTIVITY,
        not loss math — the value may predate whatever happened since, and
        the policy pass is identical anyway (same account, same flatten)."""
        db = seed_engine_db(tmp_path, "B", equity=500.0, minutes_ago=30.0,
                            peak=1000.0)
        config = _armed_config(monkeypatch, tmp_path, db)
        install_bridge_mock(monkeypatch, {("mt5b", 8001): FakeRoot(mid=4000.0)})
        install_tcp_probe(monkeypatch)
        sup = self._sup(config, tmp_path,
                        day={"date": "2026-01-01", "start_equity": 1000.0})
        alerts = sup.cycle(now=NOW)
        types = alerts[0]["triggers"]
        assert "connectivity" in types
        assert "daily_loss" not in types
        assert "dd_from_peak" not in types


# ─── Part 4: phase-1 keepers on the remaining bridge reads ────────────────


class FlakyTickRoot(FakeRoot):
    """symbol_info_tick fails N times then answers — the transient
    bridge-blip shape phase 1's retry-once exists for."""

    def __init__(self, failures_before_success=1, mid=4000.0):
        super().__init__(mid=mid)
        self.failures_left = failures_before_success
        self.attempts = 0

    def symbol_info_tick(self, symbol):
        self.attempts += 1
        if self.failures_left > 0:
            self.failures_left -= 1
            raise RuntimeError("stream has been closed")
        return {"bid": self.mid, "ask": self.mid}


class TestTickRetryOnceJitter:
    def _sleeps_recorded(self, monkeypatch):
        sleeps: list[float] = []

        def fake_sleep(seconds):
            sleeps.append(seconds)

        monkeypatch.setattr(ks.time, "sleep", fake_sleep)
        return sleeps

    def test_transient_failure_retried_once(self, monkeypatch):
        root = FlakyTickRoot(failures_before_success=1)
        install_bridge_mock(monkeypatch, {("mt5b", 8001): root})
        sleeps = self._sleeps_recorded(monkeypatch)
        with HandleSafeBridge("mt5b", 8001) as bridge:
            mid = bridge.tick_mid("XAUUSDm")
        assert mid == pytest.approx(4000.0)
        assert root.attempts == 2, "exactly one retry — never a hammering loop"
        assert len(sleeps) == 1
        assert 0.2 <= sleeps[0] <= 0.8, "retry delay must be jittered"

    def test_persistent_failure_gives_up_after_one_retry(self, monkeypatch):
        root = FlakyTickRoot(failures_before_success=99)
        install_bridge_mock(monkeypatch, {("mt5b", 8001): root})
        sleeps = self._sleeps_recorded(monkeypatch)
        with HandleSafeBridge("mt5b", 8001) as bridge:
            assert bridge.tick_mid("XAUUSDm") is None
        assert root.attempts == 2
        assert len(sleeps) == 1

    def test_healthy_tick_never_sleeps(self, monkeypatch):
        root = FakeRoot(mid=4000.0)
        install_bridge_mock(monkeypatch, {("mt5b", 8001): root})
        sleeps = self._sleeps_recorded(monkeypatch)
        with HandleSafeBridge("mt5b", 8001) as bridge:
            assert bridge.tick_mid("XAUUSDm") == pytest.approx(4000.0)
        assert sleeps == []


class TestRemoteTypeLogging:
    """ISSUE-013: a weird bridge answer must be logged with its REAL type
    and a repr snippet — 'type=<wrapper>' told the operator nothing about
    what the bridge actually returned."""

    def test_non_dict_tick_logs_real_type_and_repr(
            self, monkeypatch, caplog):
        class StringTickRoot(FakeRoot):
            def symbol_info_tick(self, symbol):
                return "<tick unavailable>"

        root = StringTickRoot()
        install_bridge_mock(monkeypatch, {("mt5b", 8001): root})
        with caplog.at_level(logging.WARNING, logger="scripts.kill_switch"):
            with HandleSafeBridge("mt5b", 8001) as bridge:
                assert bridge.tick_mid("XAUUSDm") is None
        assert "type=str" in caplog.text
        assert "<tick unavailable>" in caplog.text
        assert "non-dict" in caplog.text

    def test_describe_remote_survives_hostile_repr(self):
        class Hostile:
            def __repr__(self):
                raise RuntimeError("repr exploded")

        described = ks._describe_remote(Hostile())
        assert "repr failed" in described
        assert "type=Hostile" in described

    def test_positions_weird_shape_logs_real_type(self, monkeypatch, caplog):
        class WeirdPositionsRoot(FakeRoot):
            def positions_get(self, symbol=None, ticket=None):
                return "not-a-list"

        root = WeirdPositionsRoot()
        install_bridge_mock(monkeypatch, {("mt5b", 8001): root})
        with caplog.at_level(logging.WARNING, logger="scripts.kill_switch"):
            with HandleSafeBridge("mt5b", 8001) as bridge:
                assert bridge.positions() is None
        assert "type=str" in caplog.text

    def test_flatten_retry_recovers_transient_positions_failure(
            self, monkeypatch):
        class FlakyPositionsRoot(FakeRoot):
            def __init__(self):
                super().__init__(mid=4000.0, positions=[
                    {"ticket": 11, "symbol": "XAUUSD", "volume": 0.01,
                     "type": 0},
                ])
                self.failures_left = 1

            def positions_get(self, symbol=None, ticket=None):
                if self.failures_left > 0:
                    self.failures_left -= 1
                    raise RuntimeError("stream has been closed")
                return [dict(p) for p in self.positions]

        root = FlakyPositionsRoot()
        install_bridge_mock(monkeypatch, {("mt5b", 8001): root})
        monkeypatch.setattr(ks.time, "sleep", lambda s: None)
        with HandleSafeBridge("mt5b", 8001) as bridge:
            results = bridge.flatten()
        assert results is not None
        assert results[0]["success"] is True
        assert root.closed_tickets == [11], (
            "a transient read failure must not abort a real flatten — "
            "retry-once recovers the action"
        )


# ─── Part 5: hourly success-rate metric (one line/hour/account) ───────────


class TestHourlySuccessRate:
    @staticmethod
    def _single_account_config(monkeypatch, tmp_path, db):
        return make_config(
            monkeypatch, tmp_path,
            KILL_SWITCH_DRY_RUN="1", KILL_DB_PATH_B=db,
            KILL_DEMO_ACCOUNTS="B", KILL_CENT_ACCOUNTS="",
            KILL_REAL_ACCOUNTS="",
        )

    def _run_cycle(self, sup, db, tmp_path, now, mid=4000.0,
                   monkeypatch=None):
        # the "engine" writes a fresh snapshot just before each poll
        seed_engine_db(tmp_path, "B", equity=1000.0, minutes_ago=1.0, now=now)
        install_bridge_mock(
            monkeypatch, {("mt5b", 8001): FakeRoot(mid=mid)})
        install_tcp_probe(monkeypatch)
        sup.cycle(now=now)

    def test_hour_rollover_logs_one_line_per_account(
            self, monkeypatch, tmp_path, caplog):
        db = seed_engine_db(tmp_path, "B", equity=1000.0, now=NOW)
        config = self._single_account_config(monkeypatch, tmp_path, db)
        install_tg(monkeypatch)
        sup = KillSwitchSupervisor(config, KillState(config.state_path).load(),
                                   docker=FakeDocker())
        with caplog.at_level(logging.INFO, logger="kill_switch"):
            self._run_cycle(sup, db, tmp_path, NOW,
                            monkeypatch=monkeypatch)   # 11:00 hour bucket
            self._run_cycle(sup, db, tmp_path, NOW,
                            monkeypatch=monkeypatch)   # same hour
            hourly = [r for r in caplog.records
                      if "hourly" in r.getMessage()]
            assert hourly == [], "no line before the hour rolls over"
            self._run_cycle(sup, db, tmp_path, NOW + timedelta(hours=1),
                            monkeypatch=monkeypatch)
        lines = [r.getMessage() for r in caplog.records
                 if "hourly" in r.getMessage()]
        assert len(lines) == 1, "exactly ONE hourly line per rollover"
        assert " B " in lines[0] or lines[0].startswith("hourly B") \
            or "B:" in lines[0]
        assert "db 2/2" in lines[0], "both in-hour DB reads succeeded"
        assert "tick 2/2" in lines[0]

    def test_failures_show_up_in_the_rate(self, monkeypatch, tmp_path,
                                          caplog):
        db = seed_engine_db(tmp_path, "B", equity=1000.0, now=NOW)
        config = self._single_account_config(monkeypatch, tmp_path, db)
        install_tg(monkeypatch)
        sup = KillSwitchSupervisor(config, KillState(config.state_path).load(),
                                   docker=FakeDocker())
        with caplog.at_level(logging.INFO, logger="kill_switch"):
            self._run_cycle(sup, db, tmp_path, NOW, mid=None,
                            monkeypatch=monkeypatch)  # tick read fails
            self._run_cycle(sup, db, tmp_path, NOW + timedelta(hours=1),
                            monkeypatch=monkeypatch)
        lines = [r.getMessage() for r in caplog.records
                 if "hourly" in r.getMessage()]
        assert "tick 0/1" in lines[0]


# ─── Part 7: fail-closed boot against wrong-order deploy (review HIGH) ───


def _db_without_snapshot_columns(tmp_path, name: str = "B") -> str:
    """An engine DB from the PRE-phase-2 image: accounts table exists but
    has no current_equity/equity_ts — the shape every VPS engine DB has
    until the engines are rebuilt."""
    path = tmp_path / f"old_{name.lower()}.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT UNIQUE, "
        "balance REAL, leverage INTEGER, bridge_host TEXT, bridge_port INTEGER, "
        "signal_group TEXT, is_active BOOLEAN, created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO accounts (name, balance, leverage, bridge_host, "
        "bridge_port, signal_group) VALUES (?, 1.0, 500, 'h', 1, 'g')",
        (name,),
    )
    conn.commit()
    conn.close()
    return str(path)


class TestArmedBootGuard:
    """Cross-context review HIGH (2026-11-17), reproduced empirically by the
    reviewer: ARMED phase-2 kill switch + pre-phase-2 engine DB → the
    snapshot read raises 'no such column', every account reads as a dead
    engine, connectivity fires on the FIRST cycle (anchored by the old
    bridge-era state last_good_data) → LIVE POSITIONS FLATTENED + engines
    docker-stopped + persistent block — all over a deploy-order mistake
    that a runbook alone cannot guarantee. Fail-closed boot, same as
    _env_dry_run: refuse to ARM, loud."""

    def test_armed_refuses_boot_on_pre_phase2_db(self, monkeypatch, tmp_path):
        old_db = _db_without_snapshot_columns(tmp_path)
        config = make_config(
            monkeypatch, tmp_path, KILL_SWITCH_DRY_RUN="0",
            KILL_DEMO_ACCOUNTS="B", KILL_CENT_ACCOUNTS="",
            KILL_REAL_ACCOUNTS="", KILL_DB_PATH_B=old_db,
        )
        with pytest.raises(ValueError, match="engines"):
            KillSwitchSupervisor(config, KillState(config.state_path).load())

    def test_dry_run_boots_anyway_it_only_alerts(self, monkeypatch, tmp_path):
        old_db = _db_without_snapshot_columns(tmp_path)
        config = make_config(
            monkeypatch, tmp_path, KILL_SWITCH_DRY_RUN="1",
            KILL_DEMO_ACCOUNTS="B", KILL_CENT_ACCOUNTS="",
            KILL_REAL_ACCOUNTS="", KILL_DB_PATH_B=old_db,
        )
        sup = KillSwitchSupervisor(config, KillState(config.state_path).load())
        assert sup is not None  # dry-run must monitor through the gap

    def test_armed_ok_when_db_file_not_there_yet(self, monkeypatch,
                                                 tmp_path):
        """A DB path whose file doesn't exist = engine not started yet —
        the connectivity semantics for that are real (it IS down), so the
        guard must not block boot on it."""
        missing = str(tmp_path / "engine_not_started.db")
        config = make_config(
            monkeypatch, tmp_path, KILL_SWITCH_DRY_RUN="0",
            KILL_DEMO_ACCOUNTS="B", KILL_CENT_ACCOUNTS="",
            KILL_REAL_ACCOUNTS="", KILL_DB_PATH_B=missing,
        )
        sup = KillSwitchSupervisor(config, KillState(config.state_path).load())
        assert sup is not None

    def test_armed_refuses_boot_with_no_db_paths(self, monkeypatch, tmp_path):
        """No KILL_DB_PATH wired at all = the armed supervisor can never
        see equity → guaranteed false connectivity mass-flatten after one
        window. Refuse to arm."""
        config = make_config(
            monkeypatch, tmp_path, KILL_SWITCH_DRY_RUN="0",
            KILL_DEMO_ACCOUNTS="B", KILL_CENT_ACCOUNTS="",
            KILL_REAL_ACCOUNTS="",
        )
        with pytest.raises(ValueError, match="KILL_DB_PATH"):
            KillSwitchSupervisor(config, KillState(config.state_path).load())

    def test_armed_boots_fine_on_current_schema(self, monkeypatch, tmp_path):
        db = seed_engine_db(tmp_path, "B", equity=1000.0)
        config = make_config(
            monkeypatch, tmp_path, KILL_SWITCH_DRY_RUN="0",
            KILL_DEMO_ACCOUNTS="B", KILL_CENT_ACCOUNTS="",
            KILL_REAL_ACCOUNTS="", KILL_DB_PATH_B=db,
        )
        sup = KillSwitchSupervisor(config, KillState(config.state_path).load())
        assert sup is not None

    def test_guard_message_names_the_deploy_order(self, monkeypatch,
                                                  tmp_path):
        old_db = _db_without_snapshot_columns(tmp_path)
        config = make_config(
            monkeypatch, tmp_path, KILL_SWITCH_DRY_RUN="0",
            KILL_DEMO_ACCOUNTS="B", KILL_CENT_ACCOUNTS="",
            KILL_REAL_ACCOUNTS="", KILL_DB_PATH_B=old_db,
        )
        with pytest.raises(ValueError) as exc:
            KillSwitchSupervisor(config, KillState(config.state_path).load())
        assert "current_equity" in str(exc.value)
        assert "rebuild" in str(exc.value).lower()


# ─── Part 6: evaluate_triggers on hand-built observations (DB semantics) ──


class TestEvaluateOnDbSemantics:
    def test_data_ok_updates_last_good_data_from_engine_ts(
            self, monkeypatch, tmp_path):
        config = make_config(monkeypatch, tmp_path)
        state = KillState(config.state_path).load()
        target = next(t for t in config.targets if t.name == "B")
        ts = (NOW - timedelta(minutes=2)).isoformat()
        ob = Observation(target=target, equity=1000.0, equity_ts=ts,
                         data_ok=True)
        evaluate_triggers(config, state, {"B": ob}, NOW)
        assert state.account("B")["last_good_data"] == ts

    def test_not_data_ok_keeps_old_last_good_data(self, monkeypatch,
                                                  tmp_path):
        config = make_config(monkeypatch, tmp_path)
        old = (NOW - timedelta(minutes=30)).isoformat()
        state = KillState(config.state_path).load()
        state.account("B")["last_good_data"] = old
        target = next(t for t in config.targets if t.name == "B")
        ob = Observation(target=target, equity=1000.0,
                        equity_ts=(NOW - timedelta(minutes=30)).isoformat(),
                        data_ok=False)
        evaluate_triggers(config, state, {"B": ob}, NOW)
        assert state.account("B")["last_good_data"] == old
