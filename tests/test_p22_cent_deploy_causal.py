"""Causal proof: P22-cent stage-1 deploy (engine-p1/p2/p3, Exness Standard Cent).

คุณหมอสั่งผ่าน Hermes 16/11 — engine-p1/p2/p3 run the P22 champion on REAL
cent accounts (P1 $50 / P2 $20 / P3 $10 = 5000/2000/1000 USC). Stage 1
(16/11) was DRY_RUN on all three; stage 2 (17/11) ARMS P1 live
(DRY_RUN=0) while P2/P3 stay dry — arming state is per-account in
P22_EXPECT. This file proves the three danger
points of that deploy from REAL artifacts (compose YAML + LiveTrader /
DrawdownProtector / TradeBlocker objects), never from echo os.environ:

1. USC sizing (จุดอันตรายที่สุด per the order): the bridge returns equity in
   the ACCOUNT currency = USC (metty/bridge/client.py get_account_info —
   no conversion anywhere). Sizing is currency-agnostic
   (lots = equity×risk / SL_dist×100; broker PnL for XAUUSDc is also USC
   with contract_size 100), so lots must land in the doctor-approved
   ranges: P1 0.01–0.05 / P2 0.01–0.02 / P3 0.01 — and the identity
   lots × SL_dist × 100 ≈ equity × risk must hold in USC.
2. Baselines must be USC: INITIAL_EQUITY feeds the in-engine ACCOUNT-level
   drawdown line (live_trader.py → DrawdownProtector), compared directly
   against bridge equity. A USD-unit baseline (50) against USC equity
   (5000) is 100× off → the 20% account DD layer never fires. Proven both
   ways below: USC baseline blocks at the line, USD baseline does not.
3. EQUITY_PER_POSITION cannot block the swing/MR path while
   MAX_POSITIONS_CAP=1: min(cap, max(min, floor(eq/epp))) == 1 for ANY
   positive equity — so the demo-era default 200 could never have starved
   a $10–50 account on this path. (It IS read by live_trader, not only by
   m5_scalp_trader — value kept account-currency-correct anyway.)

The same P22_EXPECT spec drives BOTH the compose-file contract and the
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

# ── The P22-cent contract (single source of truth) ──────────────────────
# Values mirror demo C (mr-bet-p22-C) + the cent-account ladder capital.
# usc = the account-currency balance the bridge reports (1 USD = 100 USC).
# dry_run: stage 2 ARMED 17/11 — P1 live (kill switch deployed + sizing
# proven at runtime + คุณหมอ approved), P2/P3 still dry (one at a time).
P22_EXPECT = {
    "P1": {"usc": 5000, "strategy_id": "mr-bet-p22-P1", "dry_run": "0"},
    "P2": {"usc": 2000, "strategy_id": "mr-bet-p22-P2", "dry_run": "1"},
    "P3": {"usc": 1000, "strategy_id": "mr-bet-p22-P3", "dry_run": "1"},
}

# Knobs identical on all three engines (mirror mr-bet-p22-C).
P22_KNOBS = {
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
    "EQUITY_PER_POSITION": None,  # per-account = usc (set below)
    "INITIAL_BALANCE": None,      # per-account = usc
    "INITIAL_EQUITY": None,      # per-account = usc
    "LEVERAGE": "2000",
    "ACCOUNT_TYPE": "cent_real",
}

# Global (non-suffixed) engine env the P22 mirror requires.
# DRY_RUN is NOT here — it is per-account (P22_EXPECT dry_run).
P22_GLOBALS = {
    "SCALP_ENABLED": "0",
    "M5_SCALP_ENABLED": "0",
    "TRENDING_HARD_BLOCK": "1",
    "SESSION_CONFIDENCE_MULT_DISABLED": "1",
    "REGIME_VOLATILE_SKIP": "1",
    "ML_FILTER_ENABLED": "0",
    "LEARNING_MODE": "0",
    "TRADE_BLOCKER_DAILY_LIMIT": "10",
    "TRADE_BLOCKER_WEEKLY_LIMIT": "30",
    "TRADE_BLOCKER_HARD_MAX_LOTS": "0.05",
}

# Safety-critical keys that MUST be hard-coded in the service env — a
# ${VAR:-default} here lets a stale host .env value win (repo lesson:
# ML_FILTER_ENABLED / RR_RATIO_* class, see train service comments).
_NO_INTERPOLATION_KEYS = [
    "DRY_RUN", "STRATEGY_ID", "ATR_MULTIPLIER", "RR_RATIO",
    "MIN_CONFIDENCE", "BUY_MIN_CONFIDENCE", "MR_BOLL_THRESHOLD",
    "TIME_STOP_BARS", "TRAILING_TP_ENABLED",
    "CIRCUIT_BREAKER_COOLDOWN_MINUTES", "RISK_PER_TRADE",
    "SWING_MAX_SPREAD", "MR_COST_MULT", "SL_CAP",
    "MAX_POSITIONS", "MAX_POSITIONS_CAP", "EQUITY_PER_POSITION",
    "INITIAL_BALANCE", "INITIAL_EQUITY", "LEVERAGE",
    "TRADE_BLOCKER_HARD_MAX_LOTS", "MT5_SERVER", "MT5_SYMBOL",
    "ML_FILTER_ENABLED", "ACCOUNT_TYPE",
]

# Realistic XAUUSD numbers for the sizing math: ATR(M5) 2.0 → SL distance
# = ATR×2.0 (P22 multiplier) + 2.0 spread buffer = $6.0 price distance.
_SL_DISTANCE = 6.0
_ENTRY_PRICE = 2700.0


def _compose_doc():
    return yaml.safe_load(COMPOSE.read_text())


def _service_env(doc, n):
    return doc["services"][f"oracle-engine-p{n}"]["environment"]


def _expected_env(acct, spec):
    """Full expected env list for one P service (compose-contract source)."""
    usc = str(spec["usc"])
    env = {
        **{k: v for k, v in P22_GLOBALS.items()},
        # Per-account arming state (stage 2: P1 live, P2/P3 dry).
        "DRY_RUN": spec["dry_run"],
        **{k: (usc if v is None else v) for k, v in P22_KNOBS.items()},
        f"STRATEGY_ID_{acct}": spec["strategy_id"],
        f"MT5_SERVER_{acct}": "Exness-MT5Real25",
        f"MT5_SYMBOL_{acct}": "XAUUSDc",
    }
    # Per-account keys get the _{acct} suffix; global engine keys stay bare.
    suffixed = {
        f"{k}_{acct}" if k in P22_KNOBS else k: v for k, v in env.items()
    }
    return suffixed


# ═══ Part 1 — compose-file contract (artifact-level) ═══════════════════


class TestComposeContract:
    """docker-compose.vps.yml engine-p1/p2/p3 must carry the P22-cent
    env exactly (per-account dry_run in P22_EXPECT) — hard-coded, no
    interpolation on critical keys."""

    @pytest.fixture(scope="class")
    def doc(self):
        return _compose_doc()

    def test_p22_knob_matrix(self, doc):
        for acct, spec in P22_EXPECT.items():
            n = acct[1]
            env = dict(
                e.split("=", 1) if "=" in e else (e, "")
                for e in _service_env(doc, n)
            )
            for key, want in _expected_env(acct, spec).items():
                assert env.get(key) == want, (
                    f"{acct}: {key}={env.get(key)!r} expected {want!r}"
                )

    def test_critical_keys_hard_coded(self, doc):
        """${...} on a critical key = stale host .env wins = the exact bug
        class this deploy must not repeat."""
        import re
        for acct in P22_EXPECT:
            n = acct[1]
            for e in _service_env(doc, n):
                key = e.split("=", 1)[0]
                base = key.rsplit(f"_{acct}", 1)[0] if key.endswith(acct) else key
                if base in _NO_INTERPOLATION_KEYS:
                    assert "${" not in e, (
                        f"{acct}: critical key {key} uses interpolation: {e!r}"
                    )

    def test_ranging_hard_block_absent(self, doc):
        """P22 is mean reversion — entries live in RANGING regimes. The old
        P configs set RANGING_HARD_BLOCK=1 which starves every MR entry."""
        for acct in P22_EXPECT:
            for e in _service_env(doc, acct[1]):
                assert not e.startswith("RANGING_HARD_BLOCK="), (
                    f"{acct} must not set RANGING_HARD_BLOCK (MR starves)"
                )

    def test_dry_run_state_per_account(self, doc):
        """Stage 2 (17/11): P1 ARMED (DRY_RUN=0), P2/P3 stay dry (1).
        The hard-code rule itself (no ${...} on DRY_RUN) is locked by
        test_critical_keys_hard_coded via _NO_INTERPOLATION_KEYS."""
        for acct, spec in P22_EXPECT.items():
            env = _service_env(doc, acct[1])
            want = f"DRY_RUN={spec['dry_run']}"
            assert any(e == want for e in env), (
                f"{acct}: requires {want} hard-coded"
            )

    def test_baseline_in_account_currency(self, doc):
        """USC baselines: INITIAL_EQUITY is compared against bridge equity
        (USC). USD-unit values kill the account-level DD layer — see
        TestCentBaselines for the causal proof."""
        for acct, spec in P22_EXPECT.items():
            n = acct[1]
            env = dict(e.split("=", 1) for e in _service_env(doc, n))
            assert env[f"INITIAL_EQUITY_{acct}"] == str(spec["usc"])
            assert env[f"INITIAL_BALANCE_{acct}"] == str(spec["usc"])

    def test_real_a_untouched(self, doc):
        """Real-A env must not gain any P-account leak (existing contract)."""
        import re
        p_env = re.compile(r"_P[123](=|:|\$)")
        for svc in ("oracle-engine", "oracle-engine-train"):
            for e in doc["services"][svc]["environment"]:
                assert not p_env.search(e), f"{svc} leaked portfolio env: {e}"

    def test_mt5_bridges_not_recreated(self, doc):
        """Iron scope: mt5p1/p2/p3 keep their login-via-MT5_CMD_OPTIONS
        contract untouched (the engine reads its own env at start)."""
        for n in (1, 2, 3):
            env = doc["services"][f"mt5p{n}"]["environment"]
            assert any(e.startswith("MT5_CMD_OPTIONS=/login:") for e in env)


# ═══ Part 2 — object-level causal: env → LiveTrader attributes ═════════

_GLOBAL_ENV_TO_CLEAR = list(P22_GLOBALS) + [
    "RANGING_HARD_BLOCK", "H4_USE_CLOSED_BAR_ONLY",
    "ML_LOSS_THRESHOLD", "ML_FILTER_ENABLED_P",
]


def _clear_env(monkeypatch, acct):
    for k in _GLOBAL_ENV_TO_CLEAR:
        monkeypatch.delenv(k, raising=False)
    for k in list(P22_KNOBS) + ["STRATEGY_ID", "MT5_SERVER", "MT5_SYMBOL"]:
        monkeypatch.delenv(f"{k}_{acct}", raising=False)
        if k in P22_KNOBS:
            monkeypatch.delenv(k, raising=False)


def _apply_env(monkeypatch, acct, spec):
    _clear_env(monkeypatch, acct)
    for key, want in _expected_env(acct, spec).items():
        monkeypatch.setenv(key, want)


@pytest.fixture()
def p_db(tmp_path):
    from metty.core.db import init_db, insert_account
    db = tmp_path / "oracle_p.db"
    init_db(db)
    for acct, spec in P22_EXPECT.items():
        insert_account(
            name=acct, balance=float(spec["usc"]), leverage=2000,
            bridge_host=f"mt5{acct.lower()}", bridge_port=8001,
            signal_group="portfolio", db_path=db,
        )
    return db


class TestP22KnobsOnLiveObject:
    """Set the compose env → construct the REAL LiveTrader → the trading
    loop's own attributes must carry the P22 values (ISSUE-089 convention:
    never trust os.environ echo)."""

    @pytest.mark.parametrize("acct", list(P22_EXPECT))
    def test_knobs_land_on_trader(self, monkeypatch, p_db, acct):
        import broky.signals.generator as gen_mod
        # Generator flags are read at module import — set the module attrs
        # the way a fresh container would get them from env.
        monkeypatch.setattr(gen_mod, "TRENDING_HARD_BLOCK", True)
        monkeypatch.setattr(gen_mod, "RANGING_HARD_BLOCK", False)
        monkeypatch.setattr(gen_mod, "SESSION_CONFIDENCE_MULT_DISABLED", True)
        monkeypatch.setattr(gen_mod, "REGIME_VOLATILE_SKIP", True)

        _apply_env(monkeypatch, acct, P22_EXPECT[acct])
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account=acct, db_path=p_db, dry_run=True)

        spec = P22_EXPECT[acct]
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
        assert t._ml_enabled is False, "ML veto must be OFF (mirror demo C)"
        assert t.learning_mode is False
        assert t.max_positions == 1
        assert t._max_positions_cap == 1
        assert t._equity_per_position == float(spec["usc"])

    @pytest.mark.parametrize("acct", list(P22_EXPECT))
    def test_dd_baseline_usc_on_object(self, monkeypatch, p_db, acct):
        """INITIAL_EQUITY must reach the DrawdownProtector in USC so the
        account-level 20% line is real (5000 → blocks at 4000)."""
        _apply_env(monkeypatch, acct, P22_EXPECT[acct])
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account=acct, db_path=p_db, dry_run=True)
        initial = t._drawdown_protector._state.initial_equity
        assert initial == float(P22_EXPECT[acct]["usc"]), (
            f"{acct}: DD baseline {initial} is not the USC balance — "
            "a USD-unit value here silently kills the account DD layer"
        )

    @pytest.mark.parametrize("acct", list(P22_EXPECT))
    def test_trade_blocker_gates_from_env(self, monkeypatch, p_db, acct):
        """SL cap / cost coverage / hard-max-lots must reach TradeBlocker
        with the P22 values."""
        _apply_env(monkeypatch, acct, P22_EXPECT[acct])
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account=acct, db_path=p_db, dry_run=True)
        assert t._trade_blocker.max_sl_distance_price == 12.0
        assert t._trade_blocker.tp_cost_mult == 3.0
        assert t._trade_blocker.hard_max_lots == 0.05


# ═══ Part 3 — USC sizing math (จุดอันตรายที่สุด) ════════════════════════


class TestUscSizingMath:
    """Bridge equity is USC. lots = equity×risk / (SL_dist×100) — the same
    formula the broker uses for XAUUSDc PnL in USC — so the risk identity
    holds in ACCOUNT currency and the lots land in the approved ranges.
    Sizing math exercised through the REAL library call the live loop uses
    (LiveTrader._calculate_lots → risk_per_trade_size)."""

    @pytest.mark.parametrize("acct,lo,hi", [
        ("P1", 0.01, 0.05), ("P2", 0.01, 0.02), ("P3", 0.01, 0.01),
    ])
    def test_lots_in_doctor_approved_range(self, acct, lo, hi):
        """equity=USC balance, SL $6 → lots must be inside the range the
        order pins (P1 0.01–0.05 / P2 0.01–0.02 / P3 0.01)."""
        spec = P22_EXPECT[acct]
        entry = _ENTRY_PRICE
        sl = entry - _SL_DISTANCE
        lots = _sizing().risk_per_trade_size(
            float(spec["usc"]), 0.005, entry, sl,
        )
        assert lo <= lots <= hi, (
            f"{acct}: lots {lots} outside approved range [{lo}, {hi}] — "
            "USC handling is wrong, STOP and report"
        )

    @pytest.mark.parametrize("acct,lo,hi", [
        ("P1", 0.01, 0.05), ("P2", 0.01, 0.02), ("P3", 0.01, 0.01),
    ])
    def test_engine_calculate_lots_real_object(self, monkeypatch, p_db,
                                                acct, lo, hi):
        """Same range proof but through the REAL LiveTrader._calculate_lots
        (the exact method the live loop calls) — not just the library fn."""
        _apply_env(monkeypatch, acct, P22_EXPECT[acct])
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account=acct, db_path=p_db, dry_run=True)
        lots = t._calculate_lots(
            float(P22_EXPECT[acct]["usc"]), _ENTRY_PRICE,
            _ENTRY_PRICE - _SL_DISTANCE, 2.0,
        )
        assert lo <= lots <= hi, (
            f"{acct}: engine _calculate_lots returned {lots}, "
            f"outside approved range [{lo}, {hi}] — USC handling wrong"
        )

    @pytest.mark.parametrize("acct", list(P22_EXPECT))
    def test_risk_identity_in_account_currency(self, acct):
        """lots × SL_dist × 100 must equal equity×risk in USC (= $ risk /100)
        — the currency-agnostic property that makes cent sizing correct."""
        spec = P22_EXPECT[acct]
        equity_usc = float(spec["usc"])
        entry, sl = _ENTRY_PRICE, _ENTRY_PRICE - _SL_DISTANCE
        lots = _sizing().risk_per_trade_size(equity_usc, 0.005, entry, sl)
        # USC loss at SL. Normal case: floor rounding loses < 1 USC per
        # 0.01-lot step, so loss ≤ intended risk. P3 case: raw lots 0.008
        # floor UP to the 0.01 broker minimum → loss exceeds intended risk
        # by at most one lot-step (documented min-lot floor in the compose).
        loss_usc = lots * _SL_DISTANCE * 100.0
        risk_usc = equity_usc * 0.005
        if lots > 0.01:
            assert loss_usc <= risk_usc, (
                f"{acct}: loss {loss_usc} USC > intended risk {risk_usc} USC "
                "without the min-lot floor — sizing is wrong, STOP"
            )
            assert risk_usc - loss_usc < 6.0
        else:
            assert lots == 0.01, "floor path must be exactly the broker min lot"
            # loss = one min lot at this SL — the floor can only ADD risk
            # up to the min-lot size, never more.
            assert loss_usc <= 0.01 * _SL_DISTANCE * 100.0 + 1e-9
        # In USD: 25 USC = $0.25 = 0.5% of $50 — NOT $25 (the feared 100×)
        usd = risk_usc / 100.0
        assert abs(usd - (spec["usc"] / 100.0) * 0.005) < 1e-9

    def test_hard_max_lots_clamp_via_trade_blocker(self):
        """Tight-SL scenario (raw lots 0.08) must be REFUSED by the real
        TradeBlocker hard cap — engine-level proof the approved 0.05
        ceiling cannot be exceeded even if ATR collapses."""
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
            spread_price=0.30, equity=5000.0,
            margin_required=0.5, free_margin=4000.0, learning_mode=False,
        ))
        assert blocked.blocked and blocked.block_name == "hard_max_lots"

    def test_p3_min_lot_floor(self):
        """P3 ($10): raw lots at $6 SL = 5/600 = 0.008 → sizing floors to
        the 0.01 broker minimum. Documented consequence: P3 may risk slightly
        over 0.5% per trade (min-lot floor, same on every broker)."""
        lots = _sizing().risk_per_trade_size(1000.0, 0.005, _ENTRY_PRICE,
                                             _ENTRY_PRICE - _SL_DISTANCE)
        assert lots == 0.01


def _sizing():
    from broky.risk import sizing
    return sizing


# ═══ Part 4 — cent baselines: the DD layer must actually fire ══════════


class TestCentBaselines:
    """The account-level DD line compares INITIAL_EQUITY against bridge
    equity (USC). Proven both ways — this is why the compose pins USC."""

    def test_usc_baseline_blocks_at_20pct(self):
        from broky.risk.drawdown_protection import DrawdownProtector
        dp = DrawdownProtector(initial_equity=5000.0, account_limit_pct=0.20)
        ok, _ = dp.check(4000.0)  # −20% of 5000 USC = $40
        assert ok is False, "USC baseline: 20% line must block"

    def test_usd_unit_baseline_is_dead_layer(self):
        """The literal-USD reading of the order (INITIAL_EQUITY=50 against
        USC equity 5000) never blocks — causal proof the USC choice is the
        one that fulfils 'ให้ CB/DD baseline ถูก'."""
        from broky.risk.drawdown_protection import DrawdownProtector
        dp = DrawdownProtector(initial_equity=50.0, account_limit_pct=0.20)
        # Even a catastrophic −90% real loss (5000 → 500 USC = $50 → $5)
        # stays far above the $40 USD-unit line → never blocks.
        ok, _ = dp.check(500.0)
        assert ok is True, "USD-unit baseline against USC equity: dead layer"


# ═══ Part 5 — EQUITY_PER_POSITION inertness on the swing path ══════════


class TestEquityPerPositionInert:
    """The order asked whether the swing/MR path uses EQUITY_PER_POSITION
    (answer: yes, live_trader.py _calculate_max_positions — not only
    m5_scalp_trader) and whether default 200 would block $10–50 accounts.
    With cap=1 the formula can never return anything but 1."""

    @pytest.mark.parametrize("acct", list(P22_EXPECT))
    @pytest.mark.parametrize("equity", [5000.0, 1000.0, 100.0, 0.5])
    def test_max_positions_always_one(self, monkeypatch, p_db, acct, equity):
        _apply_env(monkeypatch, acct, P22_EXPECT[acct])
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account=acct, db_path=p_db, dry_run=True)
        assert t._calculate_max_positions(equity) == 1, (
            f"{acct}: dynamic max positions moved off 1 at equity {equity} — "
            "EQUITY_PER_POSITION is no longer inert, re-check the cap"
        )

    def test_legacy_default_200_also_inert(self, monkeypatch, p_db):
        """Even the untouched demo-era default (200) cannot block with
        cap=1 — the $10–50 accounts were never at risk from this knob."""
        _apply_env(monkeypatch, "P3", P22_EXPECT["P3"])
        monkeypatch.setenv("EQUITY_PER_POSITION_P3", "200")
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account="P3", db_path=p_db, dry_run=True)
        assert t._calculate_max_positions(1000.0) == 1
        assert t._calculate_max_positions(199.0) == 1


# ═══ Part 6 — USC equity path: dry-run sizing uses bridge equity ═══════

class TestDryRunEquityPath:
    """In DRY_RUN with the bridge up, _get_equity returns the broker's USC
    equity (NOT the DB paper fallback) — that is what feeds _calculate_lots
    in the live loop, so the DRY_RUN log is valid evidence of USC sizing."""

    def test_paper_fallback_only_when_bridge_unavailable(self):
        """Guard the assumption used above: paper equity is a dry_run-only
        fallback for a missing bridge (live_trader._get_equity contract)."""
        import inspect
        from metty.execution import live_trader as lt
        src = inspect.getsource(lt.LiveTrader._get_equity)
        assert "if equity is None and self.dry_run:" in src
        assert "_paper_equity_from_db" in src
