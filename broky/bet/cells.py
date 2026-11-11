"""Cell definitions from farm_variants.yml — the single source of truth.

The bet group in farm_variants.yml carries the 9 cells
(BET-{A|B|C}{6|10|8}) with their geometry; this loader turns them into
``CellSpec`` objects. The live container, the backtest, and the tests all
read the SAME file, so a backtest row and a live cell can never disagree
about what BET-A6 means (Hermes spot-checks the report against the yml).

Group schema (see farm_variants.yml):

    - name: bet
      engine: bet
      variants: [BET-A6, ...]
      db_path: /app/data/bet.db
      common:                      # group-level defaults (NOT the global
        INITIAL_BALANCE: "1000"    # common — that one belongs to farm1/2)
        BET_RISK_FRACTION: "0.015"
        BET_SPREAD_COST_PER_OZ: "0.20"
    variants:
      BET-A6:
        env:
          BET_FAMILY: "A"
          BET_TP_DIST: "6.0"
          BET_SL_DIST: "12.0"
          BET_BOLL_THRESHOLD: "0.85"   # family A only
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from broky.bet.contract import BetGeometry
from broky.bet.engine import CellSpec

ROOT = Path(__file__).resolve().parents[2]
VARIANTS_FILE = ROOT / "farm_variants.yml"

BET_ENGINE = "bet"

# Approved geometry axis (คุณหมอ 2026-11-07): TP/SL dollars, long/short alike.
GEOMETRIES = {
    "6": BetGeometry(tp_dist=6.0, sl_dist=12.0),   # TP6:SL12
    "10": BetGeometry(tp_dist=10.0, sl_dist=20.0),  # TP10:SL20
    "8": BetGeometry(tp_dist=8.0, sl_dist=8.0),     # TP8:SL8
}

SEED_BALANCE_DEFAULT = 1000.0   # $1,000 per cell (9 cells = $9,000 เสมือน)
RISK_FRACTION_DEFAULT = 0.015   # 1.5% — mid-point of the 1.2–2% Cent profile
SPREAD_COST_DEFAULT = 0.20      # $/oz round trip (farm parity: 20 points)


def load_bet_cells(yml_path: Path | str = VARIANTS_FILE) -> list[CellSpec]:
    """All bet cells defined by the `engine: bet` group(s) in the yml."""
    cfg = yaml.safe_load(Path(yml_path).read_text())
    specs: list[CellSpec] = []
    for group in cfg.get("groups", []):
        if group.get("engine") != BET_ENGINE:
            continue
        common: dict[str, Any] = group.get("common") or {}
        for name in group.get("variants", []):
            variant = (cfg.get("variants") or {}).get(name) or {}
            merged = {**common, **(variant.get("env") or {})}
            specs.append(_spec_from_env(name, merged))
    return specs


def _spec_from_env(cell_id: str, env: dict[str, Any]) -> CellSpec:
    """One CellSpec from merged env (group common ← variant overrides)."""
    try:
        family = str(env["BET_FAMILY"]).strip().upper()
        geometry = BetGeometry(
            tp_dist=float(env["BET_TP_DIST"]),
            sl_dist=float(env["BET_SL_DIST"]),
        )
    except KeyError as exc:
        raise ValueError(
            f"bet cell {cell_id} is missing required env {exc} — check "
            f"farm_variants.yml (needs BET_FAMILY / BET_TP_DIST / BET_SL_DIST)"
        ) from exc
    if geometry.tp_dist <= 0 or geometry.sl_dist <= 0:
        raise ValueError(f"bet cell {cell_id}: TP/SL distances must be > 0")
    if geometry not in GEOMETRIES.values():
        raise ValueError(
            f"bet cell {cell_id}: geometry TP{geometry.tp_dist}:SL{geometry.sl_dist} "
            f"is not one of the approved axis {sorted(GEOMETRIES)}"
        )
    return CellSpec(
        cell_id=cell_id,
        family=family,
        geometry=geometry,
        seed_balance=float(env.get("INITIAL_BALANCE", SEED_BALANCE_DEFAULT)),
        risk_fraction=float(env.get("BET_RISK_FRACTION", RISK_FRACTION_DEFAULT)),
        boll_threshold=float(env.get("BET_BOLL_THRESHOLD", 0.85)),
        spread_cost_per_oz=float(
            env.get("BET_SPREAD_COST_PER_OZ", SPREAD_COST_DEFAULT)),
    )
