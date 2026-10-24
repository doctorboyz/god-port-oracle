"""Causal proof test: per-group mode override + portfolio axis 2 (2026-10-24).

Hypothesis
----------
FARM_MODE_ENV in paper_farm_apply.py is one global list shared by every
group, so a second farm group cannot open the trend path: TRENDING_HARD_BLOCK
is read at import time in broky.signals.generator (process-global), meaning
one container = one mode. With no override mechanism a farm2 group inherits
TRENDING_HARD_BLOCK=1 and every trending cycle returns HOLD at the gate —
the trend-following axis would be deployed but mathematically silent,
indistinguishable from "no signals this week".

Causal proof
------------
build_compose must apply a group's `mode_env:` override on top of the
shared FARM_MODE_ENV:
- farm1 keeps TRENDING_HARD_BLOCK=1 exactly (MR-only axis untouched —
  the mr-bet BCD contract tests still pass on the same env)
- farm2 gets TRENDING_HARD_BLOCK=0 and the rest of the mode env intact
- the override wins by key replacement, not by duplicate env lines

Contract part (same artifact): farm_variants.yml must carry P23 (farm1,
P21+trailing) and the farm2 group P31/P32/P33 (trend axis) with the exact
knobs approved by doctorboyz (ada-task-20261024-portfolio-axis2.md), and
the P11-P22 env lines must be unchanged from the pre-axis-2 compose.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.paper_farm_apply import build_compose  # noqa: E402


def _load_cfg() -> dict:
    return yaml.safe_load((ROOT / "farm_variants.yml").read_text())


def _services() -> dict:
    return build_compose(_load_cfg())["services"]


def _env(services: dict, group: str) -> list[str]:
    return services[f"oracle-engine-{group}"]["environment"]


def _knob(env: list[str], key: str) -> str | None:
    matches = [e for e in env if e.startswith(key + "=")]
    return matches[0].split("=", 1)[1] if matches else None


class TestGroupModeOverrideMechanismCausal:
    """The override mechanism itself, on a synthetic two-group config."""

    @staticmethod
    def _cfg() -> dict:
        return {
            "common": {"RR_RATIO": "1.00"},
            "groups": [
                {"name": "farmA", "variants": ["PA"], "db_path": "/app/data/a.db"},
                {"name": "farmB", "variants": ["PB"], "db_path": "/app/data/b.db",
                 "mode_env": {"TRENDING_HARD_BLOCK": "0"}},
            ],
            "variants": {"PA": {"env": {}}, "PB": {"env": {}}},
        }

    def _group_env(self, group: str) -> list[str]:
        return _env(build_compose(self._cfg())["services"], group)

    def test_override_replaces_not_duplicates(self):
        env = self._group_env("farmB")
        trend = [e for e in env if e.startswith("TRENDING_HARD_BLOCK=")]
        assert trend == ["TRENDING_HARD_BLOCK=0"], (
            "mode_env override must REPLACE the global entry — duplicate env "
            "lines leave the winner up to docker compose's undefined behavior"
        )

    def test_sibling_group_untouched_by_override(self):
        # Control: farmA (no override) must keep the global mode env exactly
        env_a = self._group_env("farmA")
        assert "TRENDING_HARD_BLOCK=1" in env_a, (
            "a mode_env override on farm2 must not leak into farm1 — "
            "farm1's MR-only axis is the proven baseline"
        )
        assert "RANGING_HARD_BLOCK=0" in env_a, (
            "unrelated mode knobs must survive the override merge untouched"
        )

    def test_non_overridden_mode_knobs_kept_in_overriding_group(self):
        env_b = self._group_env("farmB")
        for knob in ("RANGING_HARD_BLOCK=0", "SESSION_CONFIDENCE_MULT_DISABLED=1",
                     "SEED_DEFAULT_ACCOUNTS=0", "MOCK_SPREAD_POINTS=20",
                     "DRY_RUN=1", "TRADING_PHASE=trade"):
            assert knob in env_b, (
                f"farm2 lost shared mode knob {knob} — the override must be "
                "surgical (TRENDING_HARD_BLOCK only), not a mode-env rewrite"
            )


class TestPortfolioAxis2Contract:
    """farm_variants.yml ↔ generated compose for the axis-2 expansion."""

    def test_farm1_still_mr_only(self):
        # farm1 is the proven MR-only axis — must stay exactly as before
        assert _knob(_env(_services(), "farm1"), "TRENDING_HARD_BLOCK") == "1", (
            "farm1 must remain MR-only (TRENDING_HARD_BLOCK=1) — the axis-2 "
            "work must not perturb the winning baseline"
        )

    def test_farm2_trend_axis_open(self):
        assert _knob(_env(_services(), "farm2"), "TRENDING_HARD_BLOCK") == "0", (
            "farm2 must run TRENDING_HARD_BLOCK=0 — with the global =1 baked "
            "in at import, every trending cycle would HOLD and the "
            "trend-following axis would be deployed-but-dead"
        )

    def test_farm2_group_shape(self):
        svc = _services()["oracle-engine-farm2"]
        assert svc["container_name"] == "oracle-engine-farm2", (
            "farm2 must be its own container — TRENDING_HARD_BLOCK is "
            "process-global, an account inside farm1 can never open the trend path"
        )
        assert _knob(svc["environment"], "ACCOUNTS") == "P31,P32,P33", (
            "farm2 must run exactly the three approved trend variants"
        )
        assert _knob(svc["environment"], "DB_PATH") == "/app/data/farm2.db", (
            "farm2 must have its own DB file (data/farm/farm2/farm2.db on host)"
        )

    def test_farm1_has_p23_with_exact_knobs(self):
        env = _env(_services(), "farm1")
        assert _knob(env, "ACCOUNTS") == (
            "P11,P12,P13,P14,P15,P16,P17,P18,P19,P20,P21,P22,P23"), (
            "P23 must join farm1 as the 13th variant — P21's exit-hypothesis "
            "winner combined with P22's trailing"
        )
        assert _knob(env, "RR_RATIO_P23") == "1.20"
        assert _knob(env, "MR_BOLL_THRESHOLD_P23") == "0.85"
        assert _knob(env, "ENTRY_HOURS_P23") == ""
        assert _knob(env, "TIME_STOP_BARS_P23") == "24"
        assert _knob(env, "TRAILING_TP_ENABLED_P23") == "1", (
            "P23 = P21 + trailing TP — both winning exit knobs in one variant"
        )

    def test_farm2_variants_exact_knobs(self):
        env = _env(_services(), "farm2")
        # P31 baseline trend: RR 2.5, time stop 48 bars (4h), no trailing
        assert _knob(env, "RR_RATIO_P31") == "2.5"
        assert _knob(env, "TIME_STOP_BARS_P31") == "48"
        assert _knob(env, "TRAILING_TP_ENABLED_P31") == "0"
        assert _knob(env, "MIN_CONFIDENCE_P31") == "0.55"
        # P32 let-it-run: RR 3.0, time stop 96 bars (8h), no trailing
        assert _knob(env, "RR_RATIO_P32") == "3.0"
        assert _knob(env, "TIME_STOP_BARS_P32") == "96"
        assert _knob(env, "TRAILING_TP_ENABLED_P32") == "0"
        # P33 lock profits: P31 + trailing TP
        assert _knob(env, "RR_RATIO_P33") == "2.5"
        assert _knob(env, "TIME_STOP_BARS_P33") == "48"
        assert _knob(env, "TRAILING_TP_ENABLED_P33") == "1"

    def test_farm2_names_do_not_collide_with_farm1(self):
        # league_table merges by account NAME across DBs — a collision would
        # silently blend two variants' stats into one row
        cfg = _load_cfg()
        names: list[str] = []
        for group in cfg["groups"]:
            names.extend(group["variants"])
        assert len(names) == len(set(names)), "account names must be unique farm-wide"

    def test_p11_to_p22_env_untouched(self):
        # The pre-axis-2 winners must come out byte-identical from the
        # regeneration — axis 2 is additive only.
        expected = {
            "P11": {"RR_RATIO": "0.80", "MR_BOLL_THRESHOLD": "0.70",
                    "TIME_STOP_BARS": "12", "TRAILING_TP_ENABLED": "0",
                    "ATR_MULTIPLIER": "2.0"},
            "P12": {"RR_RATIO": "1.00", "MR_BOLL_THRESHOLD": "0.80",
                    "TIME_STOP_BARS": "12", "TRAILING_TP_ENABLED": "0"},
            "P13": {"RR_RATIO": "1.20", "MR_BOLL_THRESHOLD": "0.85"},
            "P14": {"RR_RATIO": "0.80", "MR_BOLL_THRESHOLD": "0.70",
                    "ATR_MULTIPLIER": "1.5"},
            "P15": {"RR_RATIO": "0.80", "MR_BOLL_THRESHOLD": "0.70",
                    "ATR_MULTIPLIER": "2.5"},
            "P16": {"RR_RATIO": "0.80", "MR_BOLL_THRESHOLD": "0.70",
                    "TIME_STOP_BARS": "24"},
            "P17": {"RR_RATIO": "1.00", "MR_BOLL_THRESHOLD": "0.80",
                    "MIN_CONFIDENCE": "0.50", "BUY_MIN_CONFIDENCE": "0.50"},
            "P18": {"RR_RATIO": "1.00", "MR_BOLL_THRESHOLD": "0.80",
                    "MIN_CONFIDENCE": "0.60", "BUY_MIN_CONFIDENCE": "0.60"},
            "P19": {"RR_RATIO": "1.20", "MR_BOLL_THRESHOLD": "0.85",
                    "ATR_MULTIPLIER": "1.5"},
            "P20": {"RR_RATIO": "1.20", "MR_BOLL_THRESHOLD": "0.85",
                    "ENTRY_HOURS": "", "BLOCKED_HOURS": ""},
            "P21": {"RR_RATIO": "1.20", "MR_BOLL_THRESHOLD": "0.85",
                    "ENTRY_HOURS": "", "BLOCKED_HOURS": "",
                    "TIME_STOP_BARS": "24"},
            "P22": {"RR_RATIO": "1.20", "MR_BOLL_THRESHOLD": "0.85",
                    "ENTRY_HOURS": "", "BLOCKED_HOURS": "",
                    "TRAILING_TP_ENABLED": "1"},
        }
        env = _env(_services(), "farm1")
        for name, knobs in expected.items():
            for knob, value in knobs.items():
                assert _knob(env, f"{knob}_{name}") == value, (
                    f"{knob}_{name} drifted from the pre-axis-2 value "
                    f"{value!r} — axis 2 must be additive only"
                )