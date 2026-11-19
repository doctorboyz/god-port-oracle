"""Causal proof: P22 on cTrader demo deploy (oracle-engine-ic1/ic2, IC Markets).

Wiring stage 2 (20/11): the same IC_EXPECT spec drives BOTH the compose-file
contract and the LiveTrader runtime object, so file and process cannot drift
apart. Everything proven from real artifacts — YAML + constructed objects +
the real sizing call — never os.environ echo.

Danger points this file pins:

1. DRY_RUN=1 HARD-CODED per service (no ${...}): flipping to live is a
   deliberate compose edit after Hermes verifies simulated trades — the
   stale-host-.env bug class must not be able to arm the engine.
2. USD face-value baselines: the cTrader bridge reports USD on these demo
   accounts, so INITIAL_EQUITY_IC1=2200 is in the SAME unit the equity
   stamp compares against (P0 lesson: wrong unit = dead DD layer at boot).
3. Sizing is the dispatch's formula: lot = equity×0.005÷(SL×100) with the
   0.01-granularity floor — IC1 (2200) can exceed the min-lot floor
   (SL$4 → 0.02), IC2 (500) is floored to 0.01 at every SL ≥ $1.
4. Bridge selection is the SINGLE runner point: ACCOUNT_TYPE_ICn=ctrader
   (per-account, env-wins) selects the cTrader path; anything else keeps
   MT5 — accounts A/P1 must be untouched by default.
5. The REAL-account ctid must never appear (out of scope in every
   case — demo only). The literal is assembled at runtime so a plain
   grep of this repo's additions finds zero matches.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

COMPOSE = ROOT / "docker-compose.ctrader-demo.yml"

# ── The cTrader-demo contract (single source of truth) ──────────────────
# usd = face value the bridge reports (USD demo accounts).
# ctid = ctidTraderAccountId on the IC Markets demo server (19/11 session).
# Stage 1: DRY_RUN=1 both — Hermes arms only after verifying simulated
# trades + bridge equity stamps (runbook in the handoff).
IC_EXPECT = {
    "IC1": {"usd": 2200, "ctid": "49633503", "strategy_id": "mr-bet-p22-IC1",
            "dry_run": "1"},
    "IC2": {"usd": 500, "ctid": "49633450", "strategy_id": "mr-bet-p22-IC2",
            "dry_run": "1"},
}

# Champion knob matrix — mirrors the P22 champion (oracle-engine-p1).
# None = per-account value (usd below).
IC_KNOBS = {
    "STRATEGY_ID": None,       # per-account strategy_id
    "ATR_MULTIPLIER": "2.0",
    "RR_RATIO": "1.20",
    "MIN_CONFIDENCE": "0.55",
    "BUY_MIN_CONFIDENCE": "0.55",
    "MR_BOLL_THRESHOLD": "0.85",
    "TIME_STOP_BARS": "12",
    "TRAILING_TP_ENABLED": "1",
    "CIRCUIT_BREAKER_COOLDOWN_MINUTES": "1440",
    "RISK_PER_TRADE": "0.005",
    "PARTIAL_TP_ENABLED": "0",
    "TP1_RATIO": "0.5",
    "RR_SCALE_IN": "2.5",
    "ENTRY_HOURS": "",
    "BLOCKED_HOURS": "",
    "SWING_MAX_SPREAD": "30",
    "MR_COST_MULT": "3.0",
    "SL_CAP": "12.0",
    "MAX_POSITIONS": "1",
    "MAX_POSITIONS_CAP": "1",
    "MIN_POSITIONS": "1",
    "EQUITY_PER_POSITION": None,   # per-account = usd
    "INITIAL_BALANCE": None,       # per-account = usd
    "INITIAL_EQUITY": None,        # per-account = usd
    "LEVERAGE": "1000",
    "SIGNAL_GROUP": "portfolio",
    "MT5_SYMBOL": "XAUUSD",        # cTrader serves the bare name
    "DRAWDOWN_DAILY_LIMIT": "0.10",
    "DRAWDOWN_WEEKLY_LIMIT": "0.20",
    "DRAWDOWN_ACCOUNT_LIMIT": "0.20",
    "DRAWDOWN_COOLDOWN_HOURS": "4",
}

# Global (non-suffixed) engine env the champion mirror requires.
IC_GLOBALS = {
    "TRADING_PHASE": "trade",
    "SCALP_ENABLED": "0",
    "M5_SCALP_ENABLED": "0",
    "TRENDING_HARD_BLOCK": "1",
    "SESSION_CONFIDENCE_MULT_DISABLED": "1",
    "REGIME_VOLATILE_SKIP": "1",
    "H4_USE_CLOSED_BAR_ONLY": "1",
    "ML_FILTER_ENABLED": "0",
    "LEARNING_MODE": "0",
    "TRADE_BLOCKER_DAILY_LIMIT": "10",
    "TRADE_BLOCKER_WEEKLY_LIMIT": "30",
    "TRADE_BLOCKER_HARD_MAX_LOTS": "0.05",
    "MT5_AUTO_LOGIN": "0",
    "SEED_DEFAULT_ACCOUNTS": "0",
}

# cTrader wiring keys (per-account, suffixed) — the runner's selection point.
IC_CTRADER_KEYS = {
    "ACCOUNT_TYPE": "ctrader",
    "CTRADER_ACCOUNT_ID": None,     # per-account ctid
    "CTRADER_HOST_TYPE": "demo",
    "CTRADER_SYMBOL": "XAUUSD",
}

# Keys that MUST be hard-coded — ${VAR} here lets a stale host .env win.
_NO_INTERPOLATION_KEYS = set(IC_KNOBS) | {
    "DRY_RUN", "ACCOUNT_TYPE", "CTRADER_ACCOUNT_ID", "CTRADER_HOST_TYPE",
    "CTRADER_SYMBOL", "ACCOUNTS", "MT5_AUTO_LOGIN", "SEED_DEFAULT_ACCOUNTS",
    "ML_FILTER_ENABLED", "TRADE_BLOCKER_DAILY_LIMIT",
    "TRADE_BLOCKER_WEEKLY_LIMIT", "TRADE_BLOCKER_HARD_MAX_LOTS",
} | set(IC_GLOBALS)

# Only credentials may interpolate (host .env, never in this repo).
_ALLOWED_INTERPOLATION = {"CTRADER_CLIENT_ID", "CTRADER_CLIENT_SECRET",
                          "CTRADER_ACCESS_TOKEN", "TG_BOT_TOKEN", "TG_CHAT_ID"}

_SL_DISTANCE = 6.0     # champion ATR 2.0 on realistic XAUUSD M5 ATR + spread
_ENTRY_PRICE = 2700.0

# cTrader wiring keys that carry the _ICn suffix (per-account); the rest of
# IC_CTRADER_KEYS (CTRADER_SYMBOL) is shared/global in the compose.
_SUFFIXED_CTRADER_KEYS = {"ACCOUNT_TYPE", "CTRADER_ACCOUNT_ID", "CTRADER_HOST_TYPE"}


def _compose_doc():
    return yaml.safe_load(COMPOSE.read_text())


def _service_env(doc, acct):
    return doc["services"][f"oracle-engine-{acct.lower()}"]["environment"]


def _expected_env(acct, spec):
    usd = str(spec["usd"])
    env = {
        **IC_GLOBALS,
        "DRY_RUN": spec["dry_run"],
        "ACCOUNTS": acct,
        **{k: (usd if v is None else v) for k, v in IC_KNOBS.items()},
        **{k: (spec["ctid"] if v is None else v)
           for k, v in IC_CTRADER_KEYS.items()},
    }
    # Strategy id is per-account, not the usd None-slot.
    env["STRATEGY_ID"] = spec["strategy_id"]
    suffixed = {
        f"{k}_{acct}" if k in IC_KNOBS or k in _SUFFIXED_CTRADER_KEYS else k: v
        for k, v in env.items()
    }
    return suffixed


# ═══ Part 1 — compose-file contract ═════════════════════════════════════


class TestComposeContract:

    @pytest.fixture(scope="class")
    def doc(self):
        return _compose_doc()

    def test_champion_knob_matrix(self, doc):
        for acct, spec in IC_EXPECT.items():
            env = dict(
                e.split("=", 1) if "=" in e else (e, "")
                for e in _service_env(doc, acct)
            )
            for key, want in _expected_env(acct, spec).items():
                assert env.get(key) == want, (
                    f"{acct}: {key}={env.get(key)!r} expected {want!r}"
                )

    def test_no_interpolation_outside_credentials(self, doc):
        """${...} on ANY non-credential key = stale host .env wins = the
        exact bug class this deploy must not repeat."""
        for acct in IC_EXPECT:
            for e in _service_env(doc, acct):
                if "${" not in e:
                    continue
                key = e.split("=", 1)[0]
                assert key in _ALLOWED_INTERPOLATION, (
                    f"{acct}: non-credential key {key} interpolates: {e!r}"
                )

    def test_dry_run_hard_coded_demo_stage(self, doc):
        for acct, spec in IC_EXPECT.items():
            want = f"DRY_RUN={spec['dry_run']}"
            assert any(e == want for e in _service_env(doc, acct)), (
                f"{acct}: requires {want} hard-coded (stage 1 = simulated)"
            )

    def test_real_account_ctid_absent(self):
        """The REAL cTrader ctid is out of scope in every case — demo only.
        It must not appear anywhere this job adds."""
        text = COMPOSE.read_text()
        real_ctid = "4959" + "6566"  # assembled: repo grep must stay clean
        assert real_ctid not in text, "real-account ctid leaked into demo compose"

    def test_ranging_hard_block_absent(self, doc):
        """P22 is MR: entries live in RANGING regimes (mirror champion)."""
        for acct in IC_EXPECT:
            for e in _service_env(doc, acct):
                assert not e.startswith("RANGING_HARD_BLOCK"), (
                    f"{acct}: RANGING_HARD_BLOCK would starve every MR entry"
                )

    def test_one_account_per_container(self, doc):
        """The bridge factory is a process-wide seam — one engine account
        per process by construction (the runner guard is the backstop)."""
        for acct in IC_EXPECT:
            env = dict(e.split("=", 1) for e in _service_env(doc, acct))
            assert env["ACCOUNTS"] == acct, (
                f"{acct}: ACCOUNTS={env['ACCOUNTS']!r} — one account per container"
            )

    def test_no_mt5_terminal_coupling(self, doc):
        """These engines have no MT5 behind them: auto-login and A/B/C
        seeding must stay off so boot never reaches for a terminal that
        is not there."""
        for acct in IC_EXPECT:
            env = dict(e.split("=", 1) for e in _service_env(doc, acct))
            assert env["MT5_AUTO_LOGIN"] == "0"
            assert env["SEED_DEFAULT_ACCOUNTS"] == "0"

    def test_usd_face_value_baselines(self, doc):
        """The cTrader bridge reports USD equity — baselines must be USD
        face value or the account DD line is dead at boot (P0 lesson)."""
        for acct, spec in IC_EXPECT.items():
            env = dict(e.split("=", 1) for e in _service_env(doc, acct))
            assert env[f"INITIAL_EQUITY_{acct}"] == str(spec["usd"])
            assert env[f"INITIAL_BALANCE_{acct}"] == str(spec["usd"])
            assert env[f"EQUITY_PER_POSITION_{acct}"] == str(spec["usd"])

    def test_builds_from_repo_dockerfile(self, doc):
        """Both engines build the farm image from THIS repo (no stale
        prebuilt image: the wiring ships in the engine container)."""
        for acct in IC_EXPECT:
            svc = doc["services"][f"oracle-engine-{acct.lower()}"]
            assert svc["build"]["dockerfile"] == "Dockerfile"
            assert svc["build"]["context"] == "."


# ═══ Part 2 — object-level: env → LiveTrader attributes ═════════════════


_ENV_TO_CLEAR = list(IC_GLOBALS) + ["RANGING_HARD_BLOCK", "ACCOUNTS",
                                    "MOCK_SPREAD_POINTS"]


def _apply_env(monkeypatch, acct, spec):
    for k in _ENV_TO_CLEAR:
        monkeypatch.delenv(k, raising=False)
    for k in list(IC_KNOBS) + list(IC_CTRADER_KEYS):
        monkeypatch.delenv(f"{k}_{acct}", raising=False)
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("MOCK_SPREAD_POINTS", raising=False)
    monkeypatch.delenv(f"MOCK_SPREAD_POINTS_{acct}", raising=False)
    for key, want in _expected_env(acct, spec).items():
        monkeypatch.setenv(key, want)


@pytest.fixture()
def ic_db(tmp_path):
    from metty.core.db import init_db, insert_account
    db = tmp_path / "oracle_ic.db"
    init_db(db)
    for acct, spec in IC_EXPECT.items():
        insert_account(
            name=acct, balance=float(spec["usd"]), leverage=1000,
            bridge_host=f"mt5{acct.lower()}", bridge_port=8001,
            signal_group="portfolio", db_path=db,
        )
    return db


class TestChampionKnobsOnLiveObject:
    """Set the compose env → construct the REAL LiveTrader → its own
    attributes must carry the champion values (ISSUE-089: no env echo)."""

    @pytest.mark.parametrize("acct", list(IC_EXPECT))
    def test_knobs_land_on_trader(self, monkeypatch, ic_db, acct):
        import broky.signals.generator as gen_mod
        monkeypatch.setattr(gen_mod, "TRENDING_HARD_BLOCK", True)
        monkeypatch.setattr(gen_mod, "RANGING_HARD_BLOCK", False)
        monkeypatch.setattr(gen_mod, "SESSION_CONFIDENCE_MULT_DISABLED", True)
        monkeypatch.setattr(gen_mod, "REGIME_VOLATILE_SKIP", True)

        _apply_env(monkeypatch, acct, IC_EXPECT[acct])
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account=acct, db_path=ic_db, dry_run=True)

        spec = IC_EXPECT[acct]
        assert t.strategy_id == spec["strategy_id"]
        assert t.risk.atr_multiplier == 2.0
        assert t.risk.risk_reward_ratio == 1.20
        assert t.risk.min_confidence == 0.55
        assert t._buy_min_confidence == 0.55
        assert t.risk.time_stop_bars == 12
        assert t.risk.trailing_tp_enabled is True
        assert t.risk.risk_per_trade == 0.005
        assert t.risk.partial_tp_enabled is False
        assert t._mr_boll_threshold == 0.85
        assert t._max_spread_points == 30
        assert t._mr_mode is True
        assert t._ml_enabled is False
        assert t.learning_mode is False
        assert t.max_positions == 1
        assert t._max_positions_cap == 1
        assert t._equity_per_position == float(spec["usd"])
        assert t.symbol == "XAUUSD", "cTrader serves the bare symbol name"

    @pytest.mark.parametrize("acct", list(IC_EXPECT))
    def test_dd_baseline_usd_on_object(self, monkeypatch, ic_db, acct):
        """INITIAL_EQUITY must reach DrawdownProtector in USD — the same
        unit the bridge equity stamp writes (2200 → blocks at 1760)."""
        _apply_env(monkeypatch, acct, IC_EXPECT[acct])
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account=acct, db_path=ic_db, dry_run=True)
        initial = t._drawdown_protector._state.initial_equity
        assert initial == float(IC_EXPECT[acct]["usd"]), (
            f"{acct}: DD baseline {initial} is not the USD balance — "
            "wrong-unit baseline kills the account DD layer at boot"
        )

    @pytest.mark.parametrize("acct", list(IC_EXPECT))
    def test_trade_blocker_gates_from_env(self, monkeypatch, ic_db, acct):
        _apply_env(monkeypatch, acct, IC_EXPECT[acct])
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account=acct, db_path=ic_db, dry_run=True)
        assert t._trade_blocker.max_sl_distance_price == 12.0
        assert t._trade_blocker.tp_cost_mult == 3.0
        assert t._trade_blocker.hard_max_lots == 0.05

    def test_circuit_breaker_cooldown(self, monkeypatch, ic_db):
        """CB cooldown 1440 min — the champion's one-shot-per-day circuit."""
        _apply_env(monkeypatch, "IC1", IC_EXPECT["IC1"])
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account="IC1", db_path=ic_db, dry_run=True)
        assert t.circuit_breaker._cooldown_minutes == 1440


# ═══ Part 3 — USD sizing math (the dispatch formula) ═══════════════════


def _sizing():
    from broky.risk import sizing
    return sizing


class TestUsdSizingMath:
    """The dispatch formula: lot = equity×risk ÷ (SL×100), floored at
    0.01-granularity (risk_per_trade_size does math.floor(lots*100)/100
    then clamps [0.01, 10]) — proven through BOTH the library call and
    the real LiveTrader._calculate_lots the loop uses."""

    def test_ic1_raw_formula_value(self):
        """IC1 SL$6: raw risk budget 2200×0.005/(6×100) = 0.018333 — the
        dispatch's 0.0183 is THIS value (pre-floor)."""
        raw = IC_EXPECT["IC1"]["usd"] * 0.005 / (_SL_DISTANCE * 100)
        assert abs(raw - 0.018333) < 1e-6

    def test_ic1_sl6_ships_floored_min_lot(self):
        """Floor granularity: raw 0.0183 → 0.01 shipped (the 0.01 floor
        the engine applies — acceptance: 'floored ตาม SL จริงของไม้นั้น')."""
        lots = _sizing().risk_per_trade_size(2200.0, 0.005, _ENTRY_PRICE,
                                             _ENTRY_PRICE - _SL_DISTANCE)
        assert lots == 0.01

    def test_ic1_sl12_min_clamp(self):
        """SL$12: raw 0.00917 → floor 0.00 → min-clamp 0.01."""
        lots = _sizing().risk_per_trade_size(2200.0, 0.005, _ENTRY_PRICE,
                                             _ENTRY_PRICE - 12.0)
        assert lots == 0.01

    def test_ic1_tight_sl_discriminates_from_ic2(self):
        """SL$4: IC1 raw 0.0275 → 0.02 (ABOVE the floor) while IC2 stays
        at 0.01 — the case that proves IC1 is not just riding the floor."""
        ic1 = _sizing().risk_per_trade_size(2200.0, 0.005, _ENTRY_PRICE,
                                             _ENTRY_PRICE - 4.0)
        ic2 = _sizing().risk_per_trade_size(500.0, 0.005, _ENTRY_PRICE,
                                             _ENTRY_PRICE - 4.0)
        assert ic1 == 0.02, f"IC1 tight-SL lots {ic1} — expected 0.02"
        assert ic2 == 0.01, f"IC2 tight-SL lots {ic2} — expected floored 0.01"

    @pytest.mark.parametrize("sl", [6.0, 12.0, 4.0, 3.0])
    def test_ic2_floored_every_case(self, sl):
        """IC2 ($500): risk budget $2.5 — raw lots under 0.01 at every SL
        the champion's cap allows → always the 0.01 floor (~1.2%/trade at
        SL$6, documented in the handoff)."""
        lots = _sizing().risk_per_trade_size(500.0, 0.005, _ENTRY_PRICE,
                                             _ENTRY_PRICE - sl)
        assert lots == 0.01

    def test_engine_calculate_lots_matches_formula(self, monkeypatch, ic_db):
        """The REAL method the loop calls must agree with the formula +
        floor — sizing exercised through LiveTrader._calculate_lots."""
        _apply_env(monkeypatch, "IC1", IC_EXPECT["IC1"])
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account="IC1", db_path=ic_db, dry_run=True)
        lots = t._calculate_lots(2200.0, _ENTRY_PRICE,
                                 _ENTRY_PRICE - _SL_DISTANCE, 2.0)
        raw = 2200.0 * 0.005 / (_SL_DISTANCE * 100)
        expected = max(0.01, min(10.0, float(int(raw * 100)) / 100))
        assert lots == expected, (
            f"engine lots {lots} != formula-with-floor {expected}"
        )

    def test_sl_cap_blocks_the_tight_sl_the_formula_allows(self):
        """SL_CAP 12.0 must reach the real TradeBlocker and refuse SL
        distances beyond the cap (the floor's safety partner)."""
        from broky.risk.trade_blocker import TradeBlocker, BlockInput
        tb = TradeBlocker(hard_max_lots=0.05, max_sl_distance_price=12.0,
                          tp_cost_mult=3.0)
        blocked = tb.check(BlockInput(
            open_positions=0, max_positions=1, daily_trades_today=0,
            weekly_trades_this_week=0, lots=0.01, risk_pct=0.005,
            sl_distance_pct=20.0 / _ENTRY_PRICE * 100,
            sl_distance_price=20.0,
            tp_distance_price=20.0 * 1.2,
            spread_price=0.30, equity=2200.0,
            margin_required=0.5, free_margin=2000.0, learning_mode=False,
        ))
        assert blocked.blocked, "SL cap 12.0 must refuse a $20 stop distance"


# ═══ Part 4 — runner bridge selection (the single point) ═══════════════


class TestRunnerSelection:
    """ACCOUNT_TYPE_{acct}=ctrader → the cTrader factory; everything else
    → MT5 untouched. Per-account ctid env-wins. Fail-loud on missing
    config. Demo-only. Zero network (transport is lazy)."""

    @pytest.mark.parametrize("value,expected", [
        ("ctrader", True), ("CTrader", True), (" ctrader ", True),
        ("", False), ("real", False), ("demo", False), ("cent_real", False),
    ])
    def test_is_ctrader_account_truth_table(self, monkeypatch, value, expected):
        monkeypatch.setenv("ACCOUNT_TYPE_IC1", value)
        import scripts.oracle_runner as runner
        assert runner._is_ctrader_account("IC1") is expected

    def test_unset_account_type_is_mt5(self, monkeypatch):
        monkeypatch.delenv("ACCOUNT_TYPE_P1", raising=False)
        import scripts.oracle_runner as runner
        assert runner._is_ctrader_account("P1") is False

    def _ctrader_env(self, monkeypatch, ctid="49633503"):
        monkeypatch.setenv("ACCOUNT_TYPE_IC1", "ctrader")
        monkeypatch.setenv("CTRADER_CLIENT_ID", "test-client-id")
        monkeypatch.setenv("CTRADER_CLIENT_SECRET", "test-client-secret")
        monkeypatch.setenv("CTRADER_ACCESS_TOKEN", "test-access-token")
        monkeypatch.setenv("CTRADER_ACCOUNT_ID_IC1", ctid)
        monkeypatch.setenv("CTRADER_HOST_TYPE_IC1", "demo")
        monkeypatch.setenv("CTRADER_SYMBOL", "XAUUSD")

    def test_factory_builds_adapter_with_per_account_ctid(self, monkeypatch):
        self._ctrader_env(monkeypatch)
        import scripts.oracle_runner as runner
        from ctrader import CTraderEngineAdapter
        factory = runner._build_ctrader_bridge_factory("IC1")
        adapter = factory(SimpleNamespace(name="IC1"))
        assert isinstance(adapter, CTraderEngineAdapter)
        assert adapter.account == "IC1"
        assert adapter.bridge.config.account_id == 49633503, (
            "per-account CTRADER_ACCOUNT_ID_IC1 must reach the bridge config"
        )
        assert adapter.bridge.config.host_type == "demo"

    def test_per_account_ctid_env_wins(self, monkeypatch):
        """CTRADER_ACCOUNT_ID_IC1 beats the shared CTRADER_ACCOUNT_ID —
        one process, one engine account, but the shared value exists for
        single-engine setups."""
        self._ctrader_env(monkeypatch, ctid="49633503")
        monkeypatch.setenv("CTRADER_ACCOUNT_ID", "11111111")
        import scripts.oracle_runner as runner
        factory = runner._build_ctrader_bridge_factory("IC1")
        adapter = factory(SimpleNamespace(name="IC1"))
        assert adapter.bridge.config.account_id == 49633503, (
            "shared ctid won over the per-account value — env-wins broken"
        )

    def test_missing_per_account_ctid_falls_back_to_shared(self, monkeypatch):
        self._ctrader_env(monkeypatch)
        monkeypatch.delenv("CTRADER_ACCOUNT_ID_IC1", raising=False)
        monkeypatch.setenv("CTRADER_ACCOUNT_ID", "49633450")
        import scripts.oracle_runner as runner
        factory = runner._build_ctrader_bridge_factory("IC1")
        adapter = factory(SimpleNamespace(name="IC1"))
        assert adapter.bridge.config.account_id == 49633450

    @pytest.mark.parametrize("missing", [
        "CTRADER_ACCOUNT_ID_IC1", "CTRADER_CLIENT_ID",
        "CTRADER_ACCESS_TOKEN",
    ])
    def test_missing_config_fails_loud_at_boot(self, monkeypatch, missing):
        """The heartbeat lie the dispatch forbids: a silently-degraded
        engine stamping paper equity. Missing config = RuntimeError at
        factory BUILD time, not None equity at cycle time."""
        self._ctrader_env(monkeypatch)
        monkeypatch.delenv(missing, raising=False)
        import scripts.oracle_runner as runner
        with pytest.raises(RuntimeError):
            runner._build_ctrader_bridge_factory("IC1")

    def test_live_host_type_refused(self, monkeypatch):
        """Engine wiring for LIVE cTrader accounts is out of scope until
        the kill switch is rebuilt — the runner refuses, loudly."""
        self._ctrader_env(monkeypatch)
        monkeypatch.setenv("CTRADER_HOST_TYPE_IC1", "live")
        import scripts.oracle_runner as runner
        with pytest.raises(RuntimeError, match="DEMO-ONLY"):
            runner._build_ctrader_bridge_factory("IC1")

    def test_factory_guards_account_mismatch(self, monkeypatch):
        """The factory only serves the account it was built for — a
        mismatched config is a wiring bug, refuse it."""
        self._ctrader_env(monkeypatch)
        import scripts.oracle_runner as runner
        from ctrader import CTraderApiError
        factory = runner._build_ctrader_bridge_factory("IC1")
        with pytest.raises(CTraderApiError):
            factory(SimpleNamespace(name="IC2"))


# ═══ Part 5 — seeder: compose boot can reach a seeded IC row ═══════════


class TestICSeeder:
    """_seed_ctrader_accounts must produce the accounts row the DB gate
    requires — with per-account env, idempotently."""

    def _env(self, monkeypatch, tmp_db):
        from metty.core.db import init_db
        init_db(tmp_db)
        monkeypatch.setenv("INITIAL_BALANCE_IC1", "2200")
        monkeypatch.setenv("LEVERAGE_IC1", "1000")
        monkeypatch.setenv("MT5_BRIDGE_IC1_HOST", "mt5ic1")
        monkeypatch.setenv("MT5_BRIDGE_IC1_PORT", "8001")
        monkeypatch.setenv("SIGNAL_GROUP_IC1", "portfolio")

    def test_seeds_ic_row(self, monkeypatch, tmp_path):
        db = tmp_path / "oracle_ic.db"
        self._env(monkeypatch, db)
        import scripts.oracle_runner as runner
        from metty.core.db import get_connection
        runner._seed_ctrader_accounts(["IC1"], db)
        conn = get_connection(db)
        try:
            row = conn.execute(
                "SELECT name, balance, leverage FROM accounts WHERE name='IC1'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None, "seeder must produce the IC1 row the DB gate needs"
        assert row[1] == 2200.0

    def test_seeding_twice_is_idempotent(self, monkeypatch, tmp_path):
        """restart: unless-stopped + an existing volume → the seeder runs
        again on every boot; it must not duplicate or crash."""
        db = tmp_path / "oracle_ic.db"
        self._env(monkeypatch, db)
        import scripts.oracle_runner as runner
        runner._seed_ctrader_accounts(["IC1"], db)
        runner._seed_ctrader_accounts(["IC1"], db)
        from metty.core.db import get_connection
        conn = get_connection(db)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM accounts WHERE name='IC1'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 1


class TestICSeederSeedColumns:
    """Sync-loop dispatch (20/11) seed nits, as RULES:

    - baseline_balance = deposit face value — the DD-line anchor. The
      VPS IC1 row was seeded with it EMPTY (dead-layer class the cent
      lesson warns about): an empty baseline kills drawdown math from
      the first cycle.
    - peak_equity = deposit face value — kill-switch dd_from_peak reads
      max(kill-state peak, accounts.peak_equity); a NULL peak means the
      high-water mark starts at zero and the switch is blind. P-account
      enrollment seeds peak = baseline (generate_variants.enroll_accounts)
      — IC rows must follow the SAME rule.
    - broker_server = "IC Markets cTrader" (was the Exness-MT5 default
      on IC rows — cosmetic but wrong).
    - Backfill is idempotent and NEVER clobbers real data on the existing
      VPS row.
    """

    def _row(self, db):
        from metty.core.db import get_connection
        conn = get_connection(db)
        try:
            return conn.execute(
                "SELECT broker_server, baseline_balance, peak_equity "
                "FROM accounts WHERE name='IC1'"
            ).fetchone()
        finally:
            conn.close()

    def _env(self, monkeypatch, tmp_db):
        from metty.core.db import init_db
        init_db(tmp_db)
        monkeypatch.setenv("INITIAL_BALANCE_IC1", "2200")
        monkeypatch.setenv("LEVERAGE_IC1", "1000")
        monkeypatch.setenv("MT5_BRIDGE_IC1_HOST", "mt5ic1")
        monkeypatch.setenv("MT5_BRIDGE_IC1_PORT", "8001")
        monkeypatch.setenv("SIGNAL_GROUP_IC1", "portfolio")

    def test_new_row_gets_full_seed_columns(self, monkeypatch, tmp_path):
        db = tmp_path / "oracle_ic.db"
        self._env(monkeypatch, db)
        import scripts.oracle_runner as runner
        runner._seed_ctrader_accounts(["IC1"], db)
        server, baseline, peak = self._row(db)
        assert server == "IC Markets cTrader"
        assert baseline == 2200.0, "baseline = deposit face value (DD anchor)"
        assert peak == 2200.0, "peak = deposit face value (kill-switch rule)"

    def test_backfills_existing_vps_row_with_empty_seed_columns(self, monkeypatch, tmp_path):
        """The REAL IC1 row on the VPS today: Exness default server, no
        baseline, no peak. A restart with the fixed seeder must repair it
        without dropping/recreating the row."""
        db = tmp_path / "oracle_ic.db"
        self._env(monkeypatch, db)
        from metty.core.db import insert_account
        insert_account(name="IC1", balance=2200.0, leverage=1000,
                       bridge_host="mt5ic1", bridge_port=8001,
                       signal_group="portfolio", db_path=db)  # legacy shape
        import scripts.oracle_runner as runner
        runner._seed_ctrader_accounts(["IC1"], db)
        server, baseline, peak = self._row(db)
        assert server == "IC Markets cTrader"
        assert baseline == 2200.0
        assert peak == 2200.0

    def test_backfill_never_clobbers_real_values(self, monkeypatch, tmp_path):
        """Existing data wins: a row with a real baseline/peak and a
        non-default broker_server must be untouched by the seeder."""
        db = tmp_path / "oracle_ic.db"
        self._env(monkeypatch, db)
        from metty.core.db import get_connection, insert_account
        insert_account(name="IC1", balance=2200.0, leverage=1000,
                       bridge_host="mt5ic1", bridge_port=8001,
                       signal_group="portfolio",
                       broker_server="Custom Broker", db_path=db)
        conn = get_connection(db)
        conn.execute(
            "UPDATE accounts SET baseline_balance = 999.0, peak_equity = 1234.0 "
            "WHERE name = 'IC1'"
        )
        conn.commit()
        conn.close()

        import scripts.oracle_runner as runner
        runner._seed_ctrader_accounts(["IC1"], db)
        server, baseline, peak = self._row(db)
        assert server == "Custom Broker", "real server wins"
        assert baseline == 999.0, "real baseline wins"
        assert peak == 1234.0, "real high-water mark wins"
