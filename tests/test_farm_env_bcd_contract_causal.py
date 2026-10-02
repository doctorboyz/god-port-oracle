"""Causal proof test: farm env must honor the mr-bet BCD contract.

Hypothesis
----------
The paper farm clones mr-bet B/C/D variants, so its env must match the
mr-bet contract on docker-compose.vps.yml (oracle-engine-train), NOT Real-A:

- TRENDING_HARD_BLOCK=1  — MR-only mode: ADX>=20 → HOLD, entries are
  Bollinger extremes inside ranging chop. A ranging MR signal IS the trade.
- RANGING_HARD_BLOCK must be pinned OFF (=0), not merely absent — the Docker
  image carries a Real-A-era /app/.env (COPY . ., 2026-07-12) with
  RANGING_HARD_BLOCK=1 at line 80, and oracle_runner's load_dotenv() fills
  UNSET keys from it at startup (live proof 2026-10-02: load_dotenv() →
  generator.RANGING_HARD_BLOCK == True even with clean compose env). An
  explicit =0 in the process env wins over .env (load_dotenv override=False),
  so the pin is the durable fix; the farm command also deletes /app/.env
  before starting the runner (belt and braces — .env also carries 70 stale
  Real-A keys and MT5 credentials that must not leak into farm behavior).
- SESSION_CONFIDENCE_MULT_DISABLED=1 — VPS comment: "MR conf cap 0.65 x
  ASIAN 0.70 = 0.455 < 0.55 เข้าไม่ได้เลยใน golden hours UTC 0/1/6".
  Without it the farm's BCD clones are mathematically locked out of the
  golden hours, unlike the real BCD they are supposed to mirror.

Causal proof
------------
Assert on the generated compose (build_compose output, i.e. exactly what
--up deploys): RANGING_HARD_BLOCK=0 pinned, TRENDING_HARD_BLOCK=1 present,
SESSION_CONFIDENCE_MULT_DISABLED=1 present, and every service's command
removes /app/.env before exec'ing the real entrypoint.
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


class TestFarmEnvBcdContractCausal:
    """The farm's engine env must clone the VPS mr-bet BCD contract."""

    def test_ranging_hard_block_pinned_off(self):
        env = _services()["oracle-engine-farm1"]["environment"]
        assert "RANGING_HARD_BLOCK=0" in env, (
            "farm must PIN RANGING_HARD_BLOCK=0 — the image carries a Real-A "
            ".env with RANGING_HARD_BLOCK=1 and load_dotenv() fills unset keys "
            "at startup (38h / 0 trades proved it). Only an explicit =0 in the "
            "process env beats .env"
        )

    def test_trending_hard_block_still_set(self):
        # Control: the MR-only gate itself must remain (it IS the mr-bet mode)
        env = _services()["oracle-engine-farm1"]["environment"]
        assert "TRENDING_HARD_BLOCK=1" in env, (
            "farm lost TRENDING_HARD_BLOCK — variants would no longer be "
            "MR-only BCD clones"
        )

    def test_session_confidence_mult_disabled(self):
        env = _services()["oracle-engine-farm1"]["environment"]
        assert "SESSION_CONFIDENCE_MULT_DISABLED=1" in env, (
            "farm omits SESSION_CONFIDENCE_MULT_DISABLED — golden hours "
            "UTC 0/1/6 are unreachable (0.65 x 0.70 = 0.455 < 0.55), "
            "unlike the real BCD this farm mirrors"
        )

    def test_mock_spread_points_set(self):
        # Brokerless farm has no MT5: without a mock spread,
        # _get_current_spread() returns None and run_once cuts every
        # surviving signal with "spread unavailable" (P20 canary: 7/7
        # skipped, 2026-10-02). The value must stay meaningful —
        # <= SWING_MAX_SPREAD (30) so entries pass, > 0 so MR_COST_MULT
        # (TP >= 3x spread) keeps enforcing cost coverage.
        env = _services()["oracle-engine-farm1"]["environment"]
        matches = [e for e in env if e.startswith("MOCK_SPREAD_POINTS=")]
        assert matches, (
            "farm must set MOCK_SPREAD_POINTS — brokerless _get_current_spread() "
            "is always None and every surviving signal dies on "
            "'spread unavailable (MT5 disconnected?)'"
        )
        value = float(matches[0].split("=", 1)[1])
        assert 0 < value <= 30.0, (
            f"MOCK_SPREAD_POINTS={value} is inconsistent with farm gates: "
            "0 blinds MR_COST_MULT, >30 fails SWING_MAX_SPREAD=30"
        )

    def test_default_account_seeding_disabled(self):
        # The boot-time A/B/C demo seeder re-created deleted rows on every
        # restart (zombie accounts id 14-16 on 2026-10-02) — farm DBs must
        # opt out. Unset/1 = legacy seeding (VPS unchanged).
        env = _services()["oracle-engine-farm1"]["environment"]
        assert "SEED_DEFAULT_ACCOUNTS=0" in env, (
            "farm must set SEED_DEFAULT_ACCOUNTS=0 — deleting A/B/C rows is "
            "futile while every restart re-seeds them"
        )

    def test_every_service_strips_app_env(self):
        # /app/.env (Real-A era, 70 keys incl. MT5 credentials) must never
        # reach load_dotenv() in a farm container.
        for name, svc in _services().items():
            cmd = svc.get("command", "")
            assert "rm -f /app/.env" in cmd, (
                f"service {name} does not strip /app/.env before starting — "
                "load_dotenv() would load 70 stale Real-A keys"
            )