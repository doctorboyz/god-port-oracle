"""Causal proof test: farm env must honor the mr-bet BCD contract.

Hypothesis
----------
The paper farm clones mr-bet B/C/D variants, so its env must match the
mr-bet contract on docker-compose.vps.yml (oracle-engine-train), NOT Real-A:

- TRENDING_HARD_BLOCK=1  — MR-only mode: ADX>=20 → HOLD, entries are
  Bollinger extremes inside ranging chop. A ranging MR signal IS the trade.
- RANGING_HARD_BLOCK must NOT be set — generator.py's own comment says
  "B/C/D (oracle-engine-train) leave it off". Setting both blocks closes
  EVERY regime: ranging → ranging_hard_block, ADX>=20 → trending_hard_block,
  so the farm can never trade (live proof: 38h up, 0 trades, 990 ranging
  cycles blocked 2026-09-30/10-01).
- SESSION_CONFIDENCE_MULT_DISABLED=1 — VPS comment: "MR conf cap 0.65 x
  ASIAN 0.70 = 0.455 < 0.55 เข้าไม่ได้เลยใน golden hours UTC 0/1/6".
  Without it the farm's BCD clones are mathematically locked out of the
  golden hours, unlike the real BCD they are supposed to mirror.

Causal proof
------------
Assert on the generated compose env (build_compose output, i.e. exactly what
--up deploys): RANGING_HARD_BLOCK absent, TRENDING_HARD_BLOCK=1 present,
SESSION_CONFIDENCE_MULT_DISABLED=1 present. FAILS (RED) before the fix —
current FARM_MODE_ENV contains RANGING_HARD_BLOCK=1 and omits
SESSION_CONFIDENCE_MULT_DISABLED — PASSES (GREEN) after.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.paper_farm_apply import build_compose  # noqa: E402


def _engine_env(cfg: dict) -> list[str]:
    services = build_compose(cfg)["services"]
    engines = [s for name, s in services.items() if name.startswith("oracle-engine")]
    assert engines, "no oracle-engine service generated"
    return engines[0]["environment"]


def _load_cfg() -> dict:
    return yaml.safe_load((ROOT / "farm_variants.yml").read_text())


class TestFarmEnvBcdContractCausal:
    """The farm's engine env must clone the VPS mr-bet BCD contract."""

    def test_ranging_hard_block_must_not_be_set(self):
        env = _engine_env(_load_cfg())
        offenders = [e for e in env if e.startswith("RANGING_HARD_BLOCK=")]
        assert not offenders, (
            "farm sets RANGING_HARD_BLOCK — this closes the ONLY regime MR "
            "trades live in (generator.py: B/C/D leave it off). 38h / 0 trades "
            f"proved it: {offenders}"
        )

    def test_trending_hard_block_still_set(self):
        # Control: the MR-only gate itself must remain (it IS the mr-bet mode)
        env = _engine_env(_load_cfg())
        assert "TRENDING_HARD_BLOCK=1" in env, (
            "farm lost TRENDING_HARD_BLOCK — variants would no longer be "
            "MR-only BCD clones"
        )

    def test_session_confidence_mult_disabled(self):
        env = _engine_env(_load_cfg())
        assert "SESSION_CONFIDENCE_MULT_DISABLED=1" in env, (
            "farm omits SESSION_CONFIDENCE_MULT_DISABLED — golden hours "
            "UTC 0/1/6 are unreachable (0.65 x 0.70 = 0.455 < 0.55), "
            "unlike the real BCD this farm mirrors"
        )