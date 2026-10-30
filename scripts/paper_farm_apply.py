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
- Feed: paper-fetcher writes data/paper-feed/ from the `feed:` source
  (bridge = Exness XAUUSD realtime via the VPS mt5b bridge over Tailscale,
  since 2026-10-30; yfinance = legacy GC=F), engines mount it read-only
  at /app/feed (DATA_DIR).
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
    "SEED_DEFAULT_ACCOUNTS=0",      # farm DBs hold only P-accounts: the boot-time
                                    # A/B/C demo seeder re-created deleted rows on
                                    # every restart (came back as id 14-16 after the
                                    # 2026-10-02 redeploy). Unset/1 = legacy (VPS).
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

# Feed source for the fetcher — farm_variants.yml `feed:` section overrides.
# bridge (2026-10-30): Exness XAUUSD realtime from the VPS mt5q bridge over
# Tailscale — the SAME prices the demo B/C/D engines trade on, so paper↔demo
# signal timing lines up (คุณหมอ: "แก้ feed timing ใช้ realtime อันเดียว").
# mt5q is the farm's DEDICATED price terminal (login demo B env, port 5012):
# the first cut pointed the fetcher at mt5b, which the demo B engine also
# uses — the engine's per-cycle shutdown blinded the fetcher (heal loop,
# stale farm). Race fix 2026-10-30: one terminal per owner.
# yfinance (legacy): GC=F futures, delayed 5-15 min — kept selectable for
# rollback. The fetcher era-switches history on its own (never merges the
# two feeds — spot vs futures differ by dollars of basis).
FEED_DEFAULTS = {
    "source": "yfinance",
    "bridge_host": "vpsdeluna",   # tailnet MagicDNS name of the VPS
    "bridge_port": "5012",        # VPS host port → mt5q container 8001
    "poll_interval": "300",       # M5 poll seconds (bridge runs at 60)
}


def _fetcher_service(cfg: dict) -> dict:
    """paper-fetcher service — the ONLY service allowed to write the feed."""
    feed = {**FEED_DEFAULTS, **(cfg.get("feed") or {})}
    source = str(feed["source"]).strip().lower()
    env = [f"FEED_SOURCE={source}"]
    if source == "bridge":
        env += [
            f"PAPER_BRIDGE_HOST={feed['bridge_host']}",
            f"PAPER_BRIDGE_PORT={feed['bridge_port']}",
        ]
        if int(feed["poll_interval"]) > 60:
            feed["poll_interval"] = "60"  # realtime — keep bar age << 6 min
    entrypoint = (
        f"python3 scripts/paper_feed_fetcher.py --feed-dir /app/feed "
        f"--interval {feed['poll_interval']}"
    )
    return {
        "build": {"context": ".", "dockerfile": "Dockerfile"},
        "container_name": "paper-fetcher",
        "environment": env,
        "command": _STRIP_ENV_CMD.format(entrypoint=entrypoint),
        "volumes": [f"{FEED_HOST_DIR}:/app/feed"],
        "restart": "unless-stopped",
    }

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


def _group_mode_env(group: dict) -> list[str]:
    """FARM_MODE_ENV with the group's `mode_env:` overrides applied.

    TRENDING_HARD_BLOCK is read at import time in broky.signals.generator
    (process-global), so a group wanting a different mode MUST live in its
    own container — this override is how that container's env is built.
    Keys replace the global entry in place (no duplicate env lines: the
    winner of duplicates is undefined in docker compose), and brand-new
    keys not present in the global list are appended.
    """
    overrides = {
        str(k): str(v) for k, v in (group.get("mode_env") or {}).items()
    }
    lines: list[str] = []
    for entry in FARM_MODE_ENV:
        key = entry.split("=", 1)[0]
        if key in overrides:
            lines.append(f"{key}={overrides.pop(key)}")
        else:
            lines.append(entry)
    lines.extend(f"{k}={v}" for k, v in overrides.items())
    return lines


def build_compose(cfg: dict) -> dict:
    services: dict = {"paper-fetcher": _fetcher_service(cfg)}
    for group in cfg.get("groups", []):
        accounts = ",".join(group["variants"])
        env = [
            f"ACCOUNTS={accounts}",
            f"DB_PATH={group['db_path']}",
            "DATA_DIR=/app/feed",
            *_group_mode_env(group),
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