"""Causal proof test: COPY . . must not bake data/.git/.env/ψ into image layers (ISSUE-103).

Hypothesis
----------
Bug: the repo-root Dockerfile runs `COPY . .` (L22) with no .dockerignore, so
every build bakes data/ (1.6GB — oracle.db 1.4GB), .git/ (49M), ψ/ (15M),
tests/ (3.2M) and — worst — the host `.env` (REAL MT5 credentials) into the
image layer. On 2026-10-02 the disk hit 100% (117MB free) mid-deploy because
rebuilds re-accumulate multi-GB layers. Runtime never reads the baked data/
(every compose file shadows /app/data with a volume), and removing .env from
the image turns oracle_runner's load_dotenv() into a no-op — so docker-compose.yml's
oracle-engine block must pin the keys that previously leaked in via the baked
.env (RANGING_HARD_BLOCK=1, LEARNING_MODE=0 per the host .env lines 54/80).

Causal proof
------------
(1) The .dockerignore file must exist and exclude the heavy/secret/host-only
paths, while NOT excluding anything the runtime needs baked (scripts/, broky/,
metty/, shared/, docker/, pyproject.toml).
(2) docker-compose.yml oracle-engine must pin RANGING_HARD_BLOCK so behavior
survives the .env removal.

RED before fix (file absent, pin absent), GREEN after. The runtime-paths
control must pass both sides.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DOCKERIGNORE = ROOT / ".dockerignore"

# Paths that are heavy (disk exhaustion), secret (.env leak class, ISSUE-099),
# or host-only (agent vault) — must be excluded from the build context.
MUST_EXCLUDE = [
    ".git",
    ".env",
    ".env.*",
    "data",
    "*.db",
    "ψ",
    "tests",
    "graphify-out",
    "__pycache__",
    "*.pkl",
    "*.joblib",
    ".claude",
    ".synapse",
]

# Paths the runtime genuinely needs baked — excluding any of these breaks
# oracle_runner / signal generation / the healthcheck inside the image.
MUST_KEEP = [
    "scripts",
    "broky",
    "metty",
    "shared",
    "docker",
    "pyproject.toml",
]


def _dockerignore_lines() -> list[str]:
    assert DOCKERIGNORE.exists(), (
        ".dockerignore does not exist — COPY . . bakes data/ 1.6GB + host .env "
        "(MT5 credentials) into every image layer (ISSUE-103)"
    )
    lines = DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
    # Strip comments and whitespace-only lines; keep the raw pattern text.
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


class TestDockerignoreContractCausal:
    """Causal proof: build context must exclude heavy/secret paths, keep runtime paths."""

    def test_dockerignore_exists(self):
        """RED before fix: no .dockerignore exists."""
        assert DOCKERIGNORE.exists()

    def test_critical_exclusions_present(self):
        """Each heavy/secret/host-only pattern appears as its own non-comment line."""
        patterns = set(_dockerignore_lines())
        missing = [p for p in MUST_EXCLUDE if p not in patterns]
        assert not missing, f".dockerignore missing required exclusions: {missing}"

    def test_runtime_paths_not_excluded(self):
        """Control: nothing the runtime needs baked may be excluded (passes both sides)."""
        patterns = {p.rstrip("/") for p in _dockerignore_lines()}
        excluded_runtime = [p for p in MUST_KEEP if p in patterns]
        assert not excluded_runtime, (
            f".dockerignore excludes runtime-required paths: {excluded_runtime}"
        )

    def test_local_compose_pins_ranging_block(self):
        """RED before fix: docker-compose.yml oracle-engine never pinned RANGING_HARD_BLOCK.

        Today the value (1) leaks in via the baked /app/.env line 80; once
        .env is excluded from the image, load_dotenv() becomes a no-op and the
        flag falls to the generator default 0 — silently disabling Real-A's
        ranging hard-block in the local compose. The pin preserves behavior.
        """
        compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
        env = compose["services"]["oracle-engine"]["environment"]
        pins = {str(e).split("=", 1)[0]: e for e in env if "=" in str(e)}
        assert "RANGING_HARD_BLOCK" in pins, (
            "docker-compose.yml oracle-engine must pin RANGING_HARD_BLOCK "
            "(was supplied by the baked .env — ISSUE-103)"
        )
        assert "LEARNING_MODE" in pins, (
            "docker-compose.yml oracle-engine must pin LEARNING_MODE "
            "(mirror the vps compose C3 guard)"
        )