"""Causal proof: account A → "P0" repurpose (P22 champion on Exness Standard USD).

คุณหมอสั่งผ่าน Hermes 17/11 ค่ำ — account A (Exness-MT5Real15, STANDARD USD,
$100 deposited) is repurposed to run the P22 champion LIVE (same strategy as
P1-P3). This file proves the three danger points from REAL artifacts
(compose YAML + LiveTrader / DrawdownProtector / TradeBlocker / registry
objects), never from echoing os.environ:

1. USD baselines (จุดอันตรายที่สุดของ deploy นี้): this is a STANDARD account
   — the bridge reports USD (production proof 15:02 UTC 17/11:
   balance=$100.00), NOT USC like the cent ladder P1-P3. INITIAL_EQUITY /
   INITIAL_BALANCE must be 100. A cent-style 10000 baseline against USD
   equity is 100× off → the 20% account DD layer never fires. Proven dead
   both ways in TestUsdBaselines.
2. The repurpose actually happened: DRY_RUN=0 hard-coded, RANGING_HARD_BLOCK
   absent (P22 = MR — entries live in ranging; the old Real-A config's =1
   starves every entry), scalp path off, ML veto off, champion knobs
   hard-coded on the _A suffix, and no ${...} on safety-critical keys (a
   stale host .env must win NEITHER arm NOR disarm).
3. The rename is LABEL-ONLY: display_name "P0" via DISPLAY_NAME_A while
   the internal key stays "A" everywhere (registry, DB, kill switch,
   KILL_DB_PATH_A). mt5a and every other service stay untouched.

The same P0_EXPECT spec drives BOTH the compose contract and the
LiveTrader object construction, so file and runtime cannot drift apart.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

COMPOSE = ROOT / "docker-compose.vps.yml"

# ── The P0 contract (single source of truth) ────────────────────────────
# Values mirror the P22 champion (same as P1-P3) but in USD: account A is
# a STANDARD account, so the bridge reports USD directly — the baselines
# are 100, NOT the 10000 a cent-style copy-paste would produce.
P0_EXPECT = {
    "A": {
        "usd": 100,
        "strategy_id": "mr-bet-p22-P0",
        "dry_run": "0",
        "display_name": "P0",
        "mt5_server": "Exness-MT5Real15",
    },
}

# Champion knobs (mirror the P1-P3 services). None = per-account USD value.
P0_KNOBS = {
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
    "INITIAL_BALANCE": None,      # per-account = usd
    "INITIAL_EQUITY": None,       # per-account = usd
    "LEVERAGE": "500",
    "ACCOUNT_TYPE": "real",
    "SIGNAL_GROUP": "portfolio",
    "DRAWDOWN_DAILY_LIMIT": "0.10",
    "DRAWDOWN_WEEKLY_LIMIT": "0.20",
    "DRAWDOWN_ACCOUNT_LIMIT": "0.20",
    "DRAWDOWN_COOLDOWN_HOURS": "4",
}

# Global (non-suffixed) engine env the P22 mirror requires.
# DRY_RUN is NOT here — it is per-account (P0_EXPECT dry_run).
P0_GLOBALS = {
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
}

# Safety-critical keys that MUST be hard-coded in the service env — a
# ${VAR:-default} here lets a stale host .env value win (repo lesson:
# ML_FILTER_ENABLED / RR_RATIO_* class). Identical rule to the P22-cent
# deploy plus the P0-specific label/baseline keys.
_NO_INTERPOLATION_KEYS = [
    "DRY_RUN", "STRATEGY_ID", "DISPLAY_NAME", "ATR_MULTIPLIER", "RR_RATIO",
    "MIN_CONFIDENCE", "BUY_MIN_CONFIDENCE", "MR_BOLL_THRESHOLD",
    "TIME_STOP_BARS", "TRAILING_TP_ENABLED",
    "CIRCUIT_BREAKER_COOLDOWN_MINUTES", "RISK_PER_TRADE",
    "SWING_MAX_SPREAD", "MR_COST_MULT", "SL_CAP",
    "MAX_POSITIONS", "MAX_POSITIONS_CAP", "MIN_POSITIONS",
    "EQUITY_PER_POSITION", "INITIAL_BALANCE", "INITIAL_EQUITY", "LEVERAGE",
    "TRADE_BLOCKER_HARD_MAX_LOTS", "MT5_SERVER", "MT5_SYMBOL",
    "ML_FILTER_ENABLED", "ACCOUNT_TYPE", "SIGNAL_GROUP",
    "DRAWDOWN_DAILY_LIMIT", "DRAWDOWN_WEEKLY_LIMIT",
    "DRAWDOWN_ACCOUNT_LIMIT", "DRAWDOWN_COOLDOWN_HOURS",
]

# Stale Real-A-era knobs that must NOT survive the repurpose. Their presence
# would either starve P22 entries (RANGING_HARD_BLOCK=1 via a stale host
# .env win) or imply a scalp/ML path that must be dead.
_ABSENT_KEYS = [
    "RANGING_HARD_BLOCK",                    # MR enters in ranging
    "M5_SCALP_INTERVAL", "M5_SCALP_SPREAD_MAX",
    "M5_SCALP_RISK_PER_TRADE", "M5_MAX_SPREAD_A",
    "ML_LOSS_THRESHOLD", "ML_MODEL_DIR", "ML_MODEL_DIR_A",
    "TRAILING_ACTIVATION_PCT_A", "TRAILING_TRAIL_PCT_A",  # champion = defaults
    "SWING_DISABLED_ACCOUNTS", "TRADE_BLOCKER_MARGIN_SAFETY",  # default 0.8
    "ML_FILTER_ENABLED_A",
]

# Realistic XAUUSD numbers for the sizing math: ATR(M5) 2.0 → SL distance
# = ATR×2.0 + 2.0 spread buffer = $6.0 price distance.
_SL_DISTANCE = 6.0
_ENTRY_PRICE = 2700.0


def _compose_doc():
    return yaml.safe_load(COMPOSE.read_text())


def _service_env(doc, name):
    return doc["services"][name]["environment"]


def _expected_env(acct, spec):
    """Full expected env for the oracle-engine service (P0 = account A)."""
    usd = str(spec["usd"])
    env = {
        **P0_GLOBALS,
        "DRY_RUN": spec["dry_run"],
        **{k: (usd if v is None else v) for k, v in P0_KNOBS.items()},
        f"STRATEGY_ID_{acct}": spec["strategy_id"],
        f"DISPLAY_NAME_{acct}": spec["display_name"],
        f"MT5_SERVER_{acct}": spec["mt5_server"],
    }
    # Per-account keys get the _{acct} suffix; global engine keys stay bare.
    return {
        f"{k}_{acct}" if k in P0_KNOBS else k: v for k, v in env.items()
    }


# ═══ Part 1 — compose-file contract (artifact-level) ═══════════════════


class TestComposeContract:
    """docker-compose.vps.yml oracle-engine (account A → P0) must carry the
    P22 champion env in USD, hard-coded, with the old Real-A config gone."""

    @pytest.fixture(scope="class")
    def doc(self):
        return _compose_doc()

    def test_p0_knob_matrix(self, doc):
        env = dict(
            e.split("=", 1) if "=" in e else (e, "")
            for e in _service_env(doc, "oracle-engine")
        )
        for key, want in _expected_env("A", P0_EXPECT["A"]).items():
            assert env.get(key) == want, (
                f"P0: {key}={env.get(key)!r} expected {want!r}"
            )

    def test_critical_keys_hard_coded(self, doc):
        """${...} on a critical key = stale host .env wins = the exact bug
        class this deploy must not repeat (arm AND disarm directions)."""
        for e in _service_env(doc, "oracle-engine"):
            key = e.split("=", 1)[0]
            base = key.rsplit("_A", 1)[0] if key.endswith("A") else key
            if base in _NO_INTERPOLATION_KEYS:
                assert "${" not in e, (
                    f"P0: critical key {key} uses interpolation: {e!r}"
                )

    def test_credentials_stay_env_sourced(self, doc):
        """The inverse guard: login/password must NEVER be hard-coded in
        the repo (secrets live only in the VPS .env via interpolation)."""
        for key in ("MT5_LOGIN_A", "MT5_PASSWORD_A"):
            line = next(
                (e for e in _service_env(doc, "oracle-engine")
                 if e.startswith(f"{key}=")), None,
            )
            assert line is not None and "${" in line, (
                f"P0: {key} must stay ${key}:-interpolated — a literal "
                "credential in compose is a secrets leak"
            )

    def test_ranging_hard_block_absent(self, doc):
        """P22 is mean reversion — entries live in RANGING regimes. The old
        Real-A config's RANGING_HARD_BLOCK=1 starves every MR entry."""
        for e in _service_env(doc, "oracle-engine"):
            assert not e.startswith("RANGING_HARD_BLOCK="), (
                "P0 must not set RANGING_HARD_BLOCK (MR starves)"
            )

    def test_stale_real_a_knobs_gone(self, doc):
        """Old Real-A era knobs must not survive the repurpose — leaving
        them invites a stale-.env win or a dead-path resurrection."""
        env_keys = {e.split("=", 1)[0] for e in _service_env(doc, "oracle-engine")}
        for key in _ABSENT_KEYS:
            assert key not in env_keys, f"P0: stale knob {key} must be removed"

    def test_dry_run_hard_coded_live(self, doc):
        assert "DRY_RUN=0" in _service_env(doc, "oracle-engine"), (
            "P0: requires DRY_RUN=0 hard-coded (no ${...} — a stale host "
            ".env must not disarm the live order)"
        )

    def test_baseline_in_usd(self, doc):
        """STANDARD account: the bridge reports USD. INITIAL_*_A=100 — a
        cent-style 10000 here would silently kill the account DD layer
        (see TestUsdBaselines for the causal proof)."""
        env = dict(e.split("=", 1) for e in _service_env(doc, "oracle-engine"))
        assert env["INITIAL_EQUITY_A"] == "100"
        assert env["INITIAL_BALANCE_A"] == "100"
        assert env["ACCOUNT_TYPE_A"] == "real", (
            "P0 is a standard real account — cent_real would poison the "
            "registry's real-money defaults"
        )

    def test_display_label_p0_internal_key_a(self, doc):
        """Label-only rename: DISPLAY_NAME_A=P0, but ACCOUNTS/DB/bridge keep
        the internal key/volume 'A' — kill switch wiring reads those."""
        env = dict(e.split("=", 1) for e in _service_env(doc, "oracle-engine"))
        assert env["DISPLAY_NAME_A"] == "P0"
        assert env["ACCOUNTS"] == "A"
        assert env["DB_PATH"] == "/app/data/oracle.db"
        assert env["MT5_BRIDGE_A_HOST"] == "mt5a"
        assert env["MT5_BRIDGE_A_PORT"] == "8001"

    def test_a_suffixed_key_set_exact(self, doc):
        """Future-drift lock (review round 1): the per-account surface
        must be EXACTLY the expected keys — a new stray *_A knob (not in
        _ABSENT_KEYS, not matching _P[123]) must fail loudly here, not
        slip through the value matrix."""
        env_keys = {e.split("=", 1)[0] for e in _service_env(doc, "oracle-engine")}
        a_keys = {k for k in env_keys if k.endswith("_A")}
        expected_a = {
            f"{k}_A" for k in P0_KNOBS
        } | {
            "STRATEGY_ID_A", "DISPLAY_NAME_A", "MT5_SERVER_A",
            "MT5_LOGIN_A", "MT5_PASSWORD_A",
        }
        assert a_keys == expected_a, (
            f"unexpected *_A keys: {sorted(a_keys - expected_a)} / "
            f"missing: {sorted(expected_a - a_keys)}"
        )

    def test_no_p_account_leak(self, doc):
        """Existing contract (portfolio compose): the A service must not
        gain P-account env."""
        import re
        p_env = re.compile(r"_P[123](=|:|\$)")
        for e in _service_env(doc, "oracle-engine"):
            assert not p_env.search(e), f"oracle-engine leaked portfolio env: {e}"

    def test_mt5a_untouched(self, doc):
        """IRON: mt5a keeps its exact contract — credentials via
        MT5_CMD_OPTIONS interpolation, ports, volume, restart."""
        mt5a = doc["services"]["mt5a"]
        assert mt5a["container_name"] == "mt5a"
        assert any(
            e.startswith("MT5_CMD_OPTIONS=/login:${MT5_LOGIN_A}")
            for e in mt5a["environment"]
        ), "mt5a MT5_CMD_OPTIONS changed — mt5a is untouchable"
        assert "5900:3000" in mt5a["ports"]
        assert "5005:8001" in mt5a["ports"]
        assert mt5a["volumes"] == ["mt5-config-a:/config"]
        assert mt5a["restart"] == "unless-stopped"

    def test_engine_identity_lines_unchanged(self, doc):
        """The service identity the kill-switch infra contract locks:
        container, volume, dependency, restart (test_kill_switch_infra
        test_real_a_service_untouched mirror)."""
        svc = doc["services"]["oracle-engine"]
        assert svc["container_name"] == "oracle-engine"
        assert svc["volumes"] == ["oracle-data:/app/data"]
        assert list(svc["depends_on"]) == ["mt5a"]
        assert svc["restart"] == "unless-stopped"

    def test_kill_switch_db_wiring_still_matches(self, doc):
        """c18fc52: kill-switch reads A's equity from the engine DB. The
        repurpose must not break that path — same volume, same DB file."""
        ks_env = {
            e.split("=", 1)[0]: e.split("=", 1)[1]
            for e in _service_env(doc, "kill-switch") if "=" in e
        }
        assert ks_env["KILL_DB_PATH_A"] == "/ksdata/a/oracle.db"
        assert "oracle-data:/ksdata/a" in doc["services"]["kill-switch"]["volumes"]
        assert "oracle-data:/app/data" in doc["services"]["oracle-engine"]["volumes"]


# ═══ Part 2 — object-level causal: env → LiveTrader attributes ═════════

_GLOBAL_ENV_TO_CLEAR = list(P0_GLOBALS) + _ABSENT_KEYS + [
    "TRADING_PHASE", "ML_FILTER_ENABLED_A",
]

_ACCOUNT_ENV_TO_CLEAR = list(P0_KNOBS) + [
    "STRATEGY_ID", "DISPLAY_NAME", "MT5_SERVER", "MT5_SYMBOL",
]


def _clear_env(monkeypatch):
    for k in _GLOBAL_ENV_TO_CLEAR:
        monkeypatch.delenv(k, raising=False)
    for k in _ACCOUNT_ENV_TO_CLEAR:
        monkeypatch.delenv(f"{k}_A", raising=False)
        if k in P0_KNOBS:
            monkeypatch.delenv(k, raising=False)


def _apply_env(monkeypatch):
    _clear_env(monkeypatch)
    for key, want in _expected_env("A", P0_EXPECT["A"]).items():
        monkeypatch.setenv(key, want)


@pytest.fixture()
def p_db(tmp_path):
    from metty.core.db import init_db, insert_account
    db = tmp_path / "oracle_p0.db"
    init_db(db)
    insert_account(
        name="A", balance=100.0, leverage=500,
        bridge_host="mt5a", bridge_port=8001,
        signal_group="portfolio", db_path=db,
    )
    return db


class TestP22KnobsOnLiveObject:
    """Set the compose env → construct the REAL LiveTrader → the trading
    loop's own attributes must carry the P22 values (ISSUE-089 convention:
    never trust os.environ echo)."""

    def test_knobs_land_on_trader(self, monkeypatch, p_db):
        import broky.signals.generator as gen_mod
        # Generator flags are read at module import — set the module attrs
        # the way a fresh container would get them from env.
        monkeypatch.setattr(gen_mod, "TRENDING_HARD_BLOCK", True)
        monkeypatch.setattr(gen_mod, "RANGING_HARD_BLOCK", False)
        monkeypatch.setattr(gen_mod, "SESSION_CONFIDENCE_MULT_DISABLED", True)
        monkeypatch.setattr(gen_mod, "REGIME_VOLATILE_SKIP", True)

        _apply_env(monkeypatch)
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account="A", db_path=p_db, dry_run=True)

        spec = P0_EXPECT["A"]
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
        assert t._mr_mode is True, "P22 is MR-only (TRENDING_HARD_BLOCK=1)"
        assert t._ml_enabled is False, "ML veto must be OFF (mirror champion)"
        assert t.learning_mode is False
        assert t.max_positions == 1
        assert t._max_positions_cap == 1
        assert t._equity_per_position == 100.0

    def test_display_name_is_p0(self, monkeypatch, p_db):
        """The label a human sees in every log line is P0; the internal key
        stays 'A' (self.account)."""
        _apply_env(monkeypatch)
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account="A", db_path=p_db, dry_run=True)
        assert t.account == "A", "internal key must stay 'A'"
        assert t.display_name == "P0"

    def test_dd_baseline_usd_on_object(self, monkeypatch, p_db):
        """INITIAL_EQUITY must reach the DrawdownProtector in USD so the
        account-level 20% line is real (100 → blocks at 80)."""
        _apply_env(monkeypatch)
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account="A", db_path=p_db, dry_run=True)
        initial = t._drawdown_protector._state.initial_equity
        assert initial == 100.0, (
            f"P0: DD baseline {initial} is not the USD balance — a "
            "cent-style 10000 here silently kills the account DD layer"
        )

    def test_trade_blocker_gates_from_env(self, monkeypatch, p_db):
        """SL cap / cost coverage / hard-max-lots must reach TradeBlocker
        with the champion values."""
        _apply_env(monkeypatch)
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account="A", db_path=p_db, dry_run=True)
        assert t._trade_blocker.max_sl_distance_price == 12.0
        assert t._trade_blocker.tp_cost_mult == 3.0
        assert t._trade_blocker.hard_max_lots == 0.05

    @pytest.mark.parametrize("equity", [100.0, 10.0, 1.0, 0.5])
    def test_max_positions_always_one(self, monkeypatch, p_db, equity):
        _apply_env(monkeypatch)
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account="A", db_path=p_db, dry_run=True)
        assert t._calculate_max_positions(equity) == 1, (
            f"P0: dynamic max positions moved off 1 at equity {equity}"
        )


# ═══ Part 3 — USD sizing math (min-lot floor, doctor-accepted) ══════════


class TestUsdSizingMath:
    """$100 standard × min lot 0.01 × SL $6-12 = 6-12% per trade — the
    min-lot floor คุณหมอ already acknowledged (target 0.5% is unreachable
    at this balance; sizing itself stays currency-correct)."""

    def test_min_lot_floor_at_100_usd(self):
        lots = _sizing().risk_per_trade_size(
            100.0, 0.005, _ENTRY_PRICE, _ENTRY_PRICE - _SL_DISTANCE,
        )
        assert lots == 0.01, "floor path must be exactly the broker min lot"

    def test_engine_calculate_lots_real_object(self, monkeypatch, p_db):
        """Same proof through the REAL LiveTrader._calculate_lots (the
        exact method the live loop calls)."""
        _apply_env(monkeypatch)
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account="A", db_path=p_db, dry_run=True)
        lots = t._calculate_lots(
            100.0, _ENTRY_PRICE, _ENTRY_PRICE - _SL_DISTANCE, 2.0,
        )
        assert lots == 0.01

    def test_min_lot_loss_within_documented_band(self):
        """0.01 lots at SL $6 = $6 (6% of $100); at the SL cap $12 = $12
        (12%). Both ends are the band the order acknowledges (6-12%)."""
        lots = 0.01
        assert lots * _SL_DISTANCE * 100.0 == 6.0
        assert lots * 12.0 * 100.0 == 12.0  # SL_CAP_A=12 worst case

    def test_hard_max_lots_clamp_via_trade_blocker(self):
        """Even if ATR collapsed to a tight SL (raw lots > cap), the real
        TradeBlocker hard cap refuses — engine-level proof the 0.05
        ceiling cannot be exceeded."""
        from broky.risk.trade_blocker import TradeBlocker, BlockInput
        tb = TradeBlocker(
            hard_max_lots=0.05, max_sl_distance_price=12.0, tp_cost_mult=3.0,
        )
        blocked = tb.check(BlockInput(
            open_positions=0, max_positions=1, daily_trades_today=0,
            weekly_trades_this_week=0, lots=0.08, risk_pct=0.005,
            sl_distance_pct=_SL_DISTANCE / _ENTRY_PRICE * 100,
            sl_distance_price=_SL_DISTANCE,
            tp_distance_price=_SL_DISTANCE * 1.2,
            spread_price=0.30, equity=100.0,
            margin_required=0.5, free_margin=80.0, learning_mode=False,
        ))
        assert blocked.blocked and blocked.block_name == "hard_max_lots"


# ═══ Part 4 — USD baselines: the DD layer must actually fire ════════════


class TestUsdBaselines:
    """The account-level DD line compares INITIAL_EQUITY against bridge
    equity (USD on a standard account). Proven both ways — this is why
    the compose pins 100 and NOT a cent-style 10000."""

    def test_usd_baseline_blocks_at_20pct(self):
        from broky.risk.drawdown_protection import DrawdownProtector
        dp = DrawdownProtector(initial_equity=100.0, account_limit_pct=0.20)
        ok, _ = dp.check(80.0)  # −20% of $100 = $80
        assert ok is False, "USD baseline: the 20% line must block"

    def test_cent_style_baseline_freezes_at_boot(self):
        """The copy-paste-from-P1 trap: 10000 (USC-style) against a $100
        standard account is −99% from the baseline at the very first cycle
        → the account-level line blocks IMMEDIATELY and P0 never trades
        again (the reverse direction of the P-cent trap, same wrong-
        currency root). Causal proof the USD choice is the live one."""
        from broky.risk.drawdown_protection import DrawdownProtector
        dp = DrawdownProtector(initial_equity=10000.0, account_limit_pct=0.20)
        # Bridge reports $100 on day one; a 10000 baseline reads it as a
        # catastrophic breach — frozen out from the first cycle.
        ok, _ = dp.check(100.0)
        assert ok is False, (
            "cent-style baseline must be caught by this proof — if it "
            "stops blocking, the wrong-currency trap changed shape"
        )
        # And the correct USD baseline lets the SAME equity trade.
        dp_ok = DrawdownProtector(initial_equity=100.0, account_limit_pct=0.20)
        ok2, _ = dp_ok.check(100.0)
        assert ok2 is True, "USD baseline at bridge equity must allow trading"


# ═══ Part 5 — label-only rename: registry contract ═════════════════════


class TestDisplayLabelOnly:
    """DISPLAY_NAME_<NAME> override (env-wins) at the single point
    account_registry; internal key unchanged everywhere else."""

    def test_override_wins(self, monkeypatch):
        _clear_env(monkeypatch)
        monkeypatch.delenv("ACCOUNT_TYPE_A", raising=False)
        monkeypatch.setenv("DISPLAY_NAME_A", "P0")
        from metty.core.account_registry import get_display_name
        assert get_display_name("A") == "P0"

    def test_no_override_back_compat(self, monkeypatch):
        """Deployments that don't set DISPLAY_NAME_A keep 'Real-A' — the
        override is opt-in, so no other surface changes behavior."""
        _clear_env(monkeypatch)
        monkeypatch.delenv("DISPLAY_NAME_A", raising=False)
        monkeypatch.delenv("ACCOUNT_TYPE_A", raising=False)
        from metty.core.account_registry import get_display_name
        assert get_display_name("A") == "Real-A"
        assert get_display_name("B") == "Demo-B"

    def test_override_reaches_config_property(self, monkeypatch):
        """AccountConfigInfo.display_name (the runner + traders log with it)
        must carry the same override."""
        _clear_env(monkeypatch)
        monkeypatch.setenv("ACCOUNTS", "A")
        monkeypatch.setenv("ACCOUNT_TYPE_A", "real")
        monkeypatch.setenv("DISPLAY_NAME_A", "P0")
        from metty.core.account_registry import get_account_config
        cfg = get_account_config("A")
        assert cfg.name == "A", "internal key must stay 'A'"
        assert cfg.account_type == "real"
        assert cfg.display_name == "P0"


def _sizing():
    from broky.risk import sizing
    return sizing
