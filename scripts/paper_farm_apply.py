#!/usr/bin/env python3
"""Generate + deploy the paper trade farm from farm_variants.yml.

farm_variants.yml is the single source of truth. This script turns it into
docker-compose.paper-farm.yml and (with --up) brings the farm up:

    python3 scripts/paper_farm_apply.py          # regenerate compose only
    python3 scripts/paper_farm_apply.py --up     # regenerate + docker compose up -d

Contract (see ψ/memory/learnings/hermes-paper-farm-protocol.md):
- Every variant = one P-account. `common:` knobs merge with per-variant
  overrides, each emitted as `<KNOB>_<ACCOUNT>` env var.
- Existing DBs are kept across redeploys (bind dir data/farm/) — new
  accounts auto-seed via oracle_runner._seed_portfolio_accounts.
- Feed is brokerless: paper-fetcher polls yfinance GC=F into
  data/paper-feed/, engines mount it read-only at /app/feed (DATA_DIR).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
VARIANTS_FILE = ROOT / "farm_variants.yml"
COMPOSE_FILE = ROOT / "docker-compose.paper-farm.yml"

# Process-global farm rules — shared by every variant in the same container.
# TRENDING_HARD_BLOCK is read at import time in broky.signals.generator,
# so one container = one mode for all its accounts.
FARM_MODE_ENV = [
    "TRADING_PHASE=trade",          # no collector — feed comes from CSV
    "DRY_RUN=1",                    # paper: insert_live_trade only, never send_order
    "TRENDING_HARD_BLOCK=1",        # mr-bet MR-only mode (process-global).
    "RANGING_HARD_BLOCK=0",         # PINNED OFF — Real-A's gate, not BCD's.
                                    # The image carries a Real-A-era /app/.env
                                    # (COPY . .) with RANGING_HARD_BLOCK=1 and
                                    # load_dotenv() fills unset keys at boot;
                                    # an explicit =0 in the process env beats
                                    # it. MR trades ARE ranging trades —
                                    # 38h / 0 trades proved both-blocks = no
                                    # trade window ever (ISSUE-099).
    "SESSION_CONFIDENCE_MULT_DISABLED=1",  # BCD contract: MR conf cap 0.65 x
                                    # ASIAN 0.70 = 0.455 < 0.55 locks out golden
                                    # hours UTC 0/1/6 (docker-compose.vps.yml)
    "H4_USE_CLOSED_BAR_ONLY=1",
    "M5_SCALP_ENABLED=0",
    "SCALP_ENABLED=0",
    "LEARNING_MODE=0",
    "COLLECT_INTERVAL=300",
    "TRADE_INTERVAL=300",
    "ML_FILTER_ENABLED=0",
    "MT5_AUTO_LOGIN=0",             # no mt5 service in this compose — skip boot retries
    "MT5_BRIDGE_MAX_RETRIES=0",     # brokerless: bridge intentionally OFF —
                                    # no connect attempt, no ERROR spam per cycle
    "MT5_BRIDGE_RETRY_DELAY=0",
    "MOCK_SPREAD_POINTS=20",        # brokerless spread (points): farm has no
                                    # MT5, so _get_current_spread() returned
                                    # None and cut EVERY surviving signal with
                                    # "spread unavailable" (P20 canary: 7/7
                                    # skipped, 2026-10-02). 20 pts = $0.20:
                                    # passes SWING_MAX_SPREAD=30 and keeps
                                    # MR_COST_MULT=3.0 meaningful (TP >= $0.60
                                    # — real ATR-scaled TPs clear it).
    "MAX_CANDLE_AGE_SECONDS=1800",  # stale-feed guard (GC=F delay ~10-15 min;
                                    # 30 min trips only on a real outage)
    "DAILY_SUMMARY_DISABLED=1",
    "BRIDGE_STATUS_DISABLED=1",
    "TG_BOT_TOKEN=",
    "TG_CHAT_ID=",
]

FEED_HOST_DIR = "./data/paper-feed"
DATA_HOST_DIR = "./data/farm"

# The image bakes the repo's Real-A-era .env (COPY . .) — 70 stale keys plus
# MT5 credentials. Every farm service strips it before starting so
# load_dotenv() has nothing to load; per-service env above is the only source.
_STRIP_ENV_CMD = 'sh -c "rm -f /app/.env && exec {entrypoint}"'


def _variant_env(cfg: dict, name: str) -> list[str]:
    """Merge common knobs + variant overrides → `KNOB_<ACCOUNT>` env lines."""
    merged: dict[str, str] = dict(cfg.get("common", {}))
    merged.update(cfg["variants"][name].get("env", {}))
    lines = [f"STRATEGY_ID_{name}=s-{name}"]
    for knob, value in merged.items():
        lines.append(f"{knob}_{name}={value}")
    return lines


def build_compose(cfg: dict) -> dict:
    services: dict = {
        "paper-fetcher": {
            "build": {"context": ".", "dockerfile": "Dockerfile"},
            "container_name": "paper-fetcher",
            "command": _STRIP_ENV_CMD.format(
                entrypoint="python3 scripts/paper_feed_fetcher.py --feed-dir /app/feed"
            ),
            "volumes": [f"{FEED_HOST_DIR}:/app/feed"],
            "restart": "unless-stopped",
        }
    }
    for group in cfg.get("groups", []):
        accounts = ",".join(group["variants"])
        env = [
            f"ACCOUNTS={accounts}",
            f"DB_PATH={group['db_path']}",
            "DATA_DIR=/app/feed",
            *FARM_MODE_ENV,
        ]
        for name in group["variants"]:
            env.extend(_variant_env(cfg, name))

        services[f"oracle-engine-{group['name']}"] = {
            "build": {"context": ".", "dockerfile": "Dockerfile"},
            "container_name": f"oracle-engine-{group['name']}",
            "environment": env,
            "command": _STRIP_ENV_CMD.format(
                entrypoint="python3 scripts/oracle_runner.py"
            ),
            # feed read-only (fetcher owns writes); DB dir per group on host
            "volumes": [
                f"{FEED_HOST_DIR}:/app/feed:ro",
                f"{DATA_HOST_DIR}/{group['name']}:/app/data",
            ],
            "restart": "unless-stopped",
        }
    return {"services": services}


def main() -> None:
    cfg = yaml.safe_load(VARIANTS_FILE.read_text())
    compose = build_compose(cfg)

    header = (
        "# GENERATED by scripts/paper_farm_apply.py from farm_variants.yml — do not edit.\n"
        "# Source of truth: farm_variants.yml (see hermes-paper-farm-protocol.md)\n"
    )
    COMPOSE_FILE.write_text(header + yaml.safe_dump(compose, sort_keys=False))
    n = sum(len(g["variants"]) for g in cfg.get("groups", []))
    print(f"Wrote {COMPOSE_FILE.name} — {n} variants in {len(cfg.get('groups', []))} group(s)")

    if "--up" in sys.argv:
        cmd = ["docker", "compose", "-f", str(COMPOSE_FILE), "up", "-d", "--build"]
        print(f"Deploying: {' '.join(cmd)}")
        result = subprocess.run(cmd, cwd=ROOT)
        sys.exit(result.returncode)


if __name__ == "__main__":
    main()