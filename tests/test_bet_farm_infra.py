"""Contract tests — true-bet infra: yml cells → compose service → store.

Locks (Hermes acceptance criteria for T1):
- farm_variants.yml carries the `engine: bet` group with the EXACT 9 cell
  names and approved geometries (TP6:SL12 / TP10:SL20 / TP8:SL8 per
  family A/B/C), seed $1,000
- build_compose emits oracle-engine-farmbet with the bet engine entrypoint,
  its own DB, the shared feed read-only, and per-cell env knobs
- farm1/farm2 services are untouched (the pre-existing causal tests lock
  their env; here we assert the bet group adds no env into them)
- the SQLite store seeds 9 cells idempotently at $1,000 each
- scripts/bet_engine.py's env parser agrees with the yml loader (the
  container and the backtest see the same cells)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from broky.bet import cells, store  # noqa: E402
from broky.bet.contract import BetGeometry  # noqa: E402
from scripts.bet_engine import cell_spec_from_env  # noqa: E402
from scripts.paper_farm_apply import build_compose  # noqa: E402

EXPECTED_CELLS = [
    "BET-A6", "BET-A10", "BET-A8",
    "BET-B6", "BET-B10", "BET-B8",
    "BET-C6", "BET-C10", "BET-C8",
]
GEOMETRY_BY_DIGIT = {
    "6": BetGeometry(6.0, 12.0),
    "10": BetGeometry(10.0, 20.0),
    "8": BetGeometry(8.0, 8.0),
}


def _cfg() -> dict:
    return yaml.safe_load((ROOT / "farm_variants.yml").read_text())


def _services() -> dict:
    return build_compose(_cfg())["services"]


def _knob(env: list[str], key: str) -> str | None:
    matches = [e for e in env if e.startswith(key + "=")]
    return matches[0].split("=", 1)[1] if matches else None


class TestYmlCells:
    def test_nine_cells_with_exact_names(self):
        specs = cells.load_bet_cells()
        assert sorted(s.cell_id for s in specs) == sorted(EXPECTED_CELLS)

    def test_geometry_axis_matches_approval(self):
        specs = {s.cell_id: s for s in cells.load_bet_cells()}
        for cell_id, spec in specs.items():
            digit = cell_id.split("-")[1][1:]  # BET-A10 → "10"
            assert spec.geometry == GEOMETRY_BY_DIGIT[digit], cell_id
            family = cell_id.split("-")[1][0]
            assert spec.family == family

    def test_seed_is_1000_per_cell(self):
        for spec in cells.load_bet_cells():
            assert spec.seed_balance == 1000.0

    def test_bet_group_declares_engine_and_db(self):
        group = next(g for g in _cfg()["groups"] if g.get("engine") == "bet")
        assert group["db_path"] == "/app/data/bet.db"
        assert group["name"] == "bet"

    def test_unapproved_geometry_is_rejected(self):
        with_root = {"groups": [{
            "name": "bet", "engine": "bet", "variants": ["BET-Z9"],
        }], "variants": {"BET-Z9": {"env": {
            "BET_FAMILY": "A", "BET_TP_DIST": "9.0", "BET_SL_DIST": "9.0",
        }}}}
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as f:
            yaml.safe_dump(with_root, f)
            path = Path(f.name)
        try:
            import pytest
            with pytest.raises(ValueError):
                cells.load_bet_cells(path)
        finally:
            path.unlink()


class TestComposeService:
    def test_farmbet_service_exists_with_bet_entrypoint(self):
        svc = _services()["oracle-engine-farmbet"]
        assert svc["container_name"] == "oracle-engine-farmbet"
        assert "scripts/bet_engine.py" in svc["command"]
        assert "oracle_runner.py" not in svc["command"]

    def test_db_and_feed_mounts(self):
        svc = _services()["oracle-engine-farmbet"]
        env = svc["environment"]
        assert _knob(env, "DB_PATH") == "/app/data/bet.db"
        assert _knob(env, "DATA_DIR") == "/app/feed"
        assert "./data/paper-feed:/app/feed:ro" in svc["volumes"]
        assert "./data/farm/bet:/app/data" in svc["volumes"]

    def test_accounts_line_lists_nine_cells(self):
        env = _services()["oracle-engine-farmbet"]["environment"]
        assert _knob(env, "ACCOUNTS") == ",".join(EXPECTED_CELLS)

    def test_per_cell_knobs_emitted(self):
        env = _services()["oracle-engine-farmbet"]["environment"]
        assert _knob(env, "BET_FAMILY_BET_A6") == "A"
        assert _knob(env, "BET_TP_DIST_BET_A6") == "6.0"
        assert _knob(env, "BET_SL_DIST_BET_A6") == "12.0"
        assert _knob(env, "BET_BOLL_THRESHOLD_BET_A6") == "0.85"
        assert _knob(env, "INITIAL_BALANCE_BET_C8") == "1000"
        assert _knob(env, "BET_RISK_FRACTION_BET_C8") == "0.015"
        assert _knob(env, "BET_TP_DIST_BET_B10") == "10.0"
        assert _knob(env, "BET_SL_DIST_BET_B10") == "20.0"

    def test_env_keys_never_contain_dashes(self):
        """Causal lock (live incident 2026-11-11 15:13 UTC): container
        runtimes DROP env entries whose KEY contains '-' from the PID-1
        environment (docker inspect still lists them, so the failure is
        invisible to config checks). The first farmbet boot silently seeded
        ALL 9 cells as default A/TP6:SL12. Every emitted key must therefore
        match ^[A-Za-z0-9_]+$ — for this service and any future one."""
        import re
        env = _services()["oracle-engine-farmbet"]["environment"]
        for line in env:
            key = line.split("=", 1)[0]
            assert re.fullmatch(r"[A-Za-z0-9_]+", key), \
                f"env key {key!r} contains a runtime-hostile character"

    def test_brokerless_safety_env(self):
        env = _services()["oracle-engine-farmbet"]["environment"]
        assert _knob(env, "MT5_AUTO_LOGIN") == "0"

    def test_farm1_farm2_env_untouched(self):
        """The bet group must not leak a single knob into the old farms."""
        for group in ("farm1", "farm2"):
            env = _services()[f"oracle-engine-{group}"]["environment"]
            assert not [e for e in env if e.startswith("BET_")], group
            assert not [e for e in env if "BET-" in e], group

    def test_old_farm_services_still_present(self):
        services = _services()
        assert "oracle-engine-farm1" in services
        assert "oracle-engine-farm2" in services
        assert services["oracle-engine-farm1"]["container_name"] == "oracle-engine-farm1"


class TestStore:
    def test_seed_nine_cells_idempotent(self, tmp_path):
        conn = store.connect(tmp_path / "bet.db")
        specs = cells.load_bet_cells()
        created = store.seed_cells(conn, specs)
        assert sorted(created) == sorted(EXPECTED_CELLS)
        rows = conn.execute(
            "SELECT cell_id, balance, seed_balance FROM bet_cells"
        ).fetchall()
        assert len(rows) == 9
        for row in rows:
            assert row["balance"] == 1000.0
            assert row["seed_balance"] == 1000.0
        # second boot seeds nothing new
        assert store.seed_cells(conn, specs) == []
        conn.close()

    def test_open_bet_roundtrip(self, tmp_path):
        """Insert a bet, rehydrate the resolver, book a resolution."""
        from datetime import datetime
        from broky.bet.contract import open_bet, OUTCOME_TP

        conn = store.connect(tmp_path / "bet.db")
        store.seed_cells(conn, [cells.load_bet_cells()[0]])
        spec = cells.load_bet_cells()[0]
        bet = open_bet(
            cell_id=spec.cell_id, direction="BUY",
            signal_ts=datetime(2026, 1, 5, 0, 5),
            entry_ts=datetime(2026, 1, 5, 0, 10),
            entry_price=4000.0, geometry=spec.geometry,
            risk_amount=15.0, spread_cost_per_oz=spec.spread_cost_per_oz,
        )
        bet_id = store.insert_bet(conn, bet)
        row = store.get_open_bet(conn, spec.cell_id)
        assert row is not None and row["id"] == bet_id

        resolver = store.build_resolver(row, spec.spread_cost_per_oz)
        resolver.step_bar(datetime(2026, 1, 5, 0, 15), 4001.0, 3999.0)
        store.save_progress(conn, bet_id, resolver)

        # restart: rehydrated resolver keeps the persisted excursions
        row = store.get_open_bet(conn, spec.cell_id)
        resumed = store.build_resolver(row, spec.spread_cost_per_oz)
        assert resumed.state.mae_price > 0.0
        assert resumed.state.bars_to_finish == 1

        # tick resolution books pnl and credits the cell balance
        resolver = store.build_resolver(row, spec.spread_cost_per_oz)
        resolver.step_tick(datetime(2026, 1, 5, 0, 20), bet.tp_price)
        assert resolver.state.outcome == OUTCOME_TP
        store.resolve_bet_row(conn, row, resolver.state)
        balance = store.cell_balance(conn, spec.cell_id)
        assert balance > 1000.0
        assert store.get_open_bet(conn, spec.cell_id) is None
        conn.close()


class TestEnvParserMatchesYml:
    def test_container_env_parses_to_same_spec(self, monkeypatch, tmp_path):
        """bet_engine's env knobs and the yml loader must agree exactly —
        for EVERY cell, not just the first (the first (BET-A6) shares its
        values with the defaults, which is exactly how the dash-key bug hid:
        the original version of this test only checked BET-A6 and passed)."""
        import os

        cfg = _cfg()
        group = next(g for g in cfg["groups"] if g.get("engine") == "bet")
        services = build_compose(cfg)["services"]
        env_lines = services["oracle-engine-farmbet"]["environment"]

        monkeypatch.delenv("ACCOUNTS", raising=False)
        for line in env_lines:
            key, _, value = line.partition("=")
            monkeypatch.setenv(key, value)

        yml_specs = {s.cell_id: s for s in cells.load_bet_cells()}
        for cell_id in group["variants"]:
            env_spec = cell_spec_from_env(cell_id)
            yml_spec = yml_specs[cell_id]
            assert env_spec.family == yml_spec.family, cell_id
            assert env_spec.geometry == yml_spec.geometry, cell_id
            assert env_spec.seed_balance == yml_spec.seed_balance, cell_id
            assert env_spec.risk_fraction == yml_spec.risk_fraction, cell_id
            assert env_spec.boll_threshold == yml_spec.boll_threshold, cell_id
            assert env_spec.spread_cost_per_oz == yml_spec.spread_cost_per_oz, cell_id
