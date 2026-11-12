"""ISSUE-100 class systemic guard: env plumbing must have both ends connected.

The margin-gate bug (fixed 2026-10-10, tests/test_leverage_env_farm_causal.py)
was a silent no-op: docker-compose.paper-farm.yml set LEVERAGE_P11..P20=2000
while live_trader.py read MT5_LEVERAGE_{account} — a name nothing sets — so
every margin check quietly priced at the 1:100 default and margin_safety
blocked ~306 real farm signals. Neither end errored; the pipe was just cut
in the middle by a name mismatch.

This guard pins BOTH directions of that pipe:

A. compose -> code: every env key the farm compose writes into a container
   must have a reader in production code — literally (os.environ.get("KEY"))
   or as an account template (KEY_P11 read via f"KEY_{account}").

B. code -> config: every env name production code reads must be SET by some
   live config source (compose files, farm_variants.yml, .env) or be listed
   in ALLOWED_OPTIONAL with a documented reason. This is the direction that
   caught the bug: MT5_LEVERAGE_* was read by live_trader/m5_scalp_trader
   and set by NOTHING (the pre-fix scan, 2026-10-10, listed it as dead).

Known limitation: the reader regex covers os.environ.get / os.environ[ /
os.getenv with f-string or plain names. Exotic indirection (env name built
at runtime) is out of scope — those should not exist for config plumbing.

Optional knobs (read with sane defaults, intentionally unset today) are
allowlisted WITH their reason — the allowlist is the documentation. A new
unset read that is not allowlisted fails this test until someone either
sets it or documents why it may dangle.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Corpus / sources
# ---------------------------------------------------------------------------

PRODUCTION_DIRS = ("metty", "broky", "shared", "scripts")


def _production_corpus() -> str:
    chunks = []
    for sub in PRODUCTION_DIRS:
        for p in (ROOT / sub).rglob("*.py"):
            chunks.append(p.read_text(errors="replace"))
    for p in ROOT.glob("*.py"):
        chunks.append(p.read_text(errors="replace"))
    return "\n".join(chunks)


def _env_sources() -> dict[str, set[str]]:
    """base env name -> where it is set. Only LIVE config counts:
    docker-compose*.yml, farm_variants.yml, .env, .env.example, shell scripts.
    Stale backups (.bak/.backup/old) are deliberately excluded — a var set
    only in a backup is still dead in every running deployment."""
    sources: dict[str, set[str]] = {}
    candidates = list(ROOT.glob("docker-compose*.yml"))
    candidates += [ROOT / "farm_variants.yml", ROOT / ".env", ROOT / ".env.example"]
    candidates += list(ROOT.glob("Dockerfile*"))
    for sub in ("scripts", "ops"):
        d = ROOT / sub
        if d.is_dir():
            candidates += list(d.rglob("*.sh"))
    for path in candidates:
        if not path.exists() or path.is_dir():
            continue
        name = path.name
        if ".bak" in name or "backup" in name or ".old" in name:
            continue
        text = path.read_text(errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            for m in re.finditer(r"(?:^|[\s{'\"-])([A-Z][A-Z0-9_]+)\s*[:=]", line):
                sources.setdefault(m.group(1), set()).add(f"{name}:{lineno}")
    return sources


# ---------------------------------------------------------------------------
# Direction A: compose -> code
# ---------------------------------------------------------------------------

_KEY_LIST_RE = re.compile(r"^\s+-\s+([A-Z][A-Z0-9_]+)\s*(?:=|$)", re.M)
_KEY_MAP_RE = re.compile(r"^\s+([A-Z][A-Z0-9_]+):\s", re.M)
# Per-account / per-cell suffix families a compose key can carry and still be
# read via a template: _P11 (P-account variants) and _BET_A6-style (true-bet
# cells — scripts/bet_engine.py reads them as f"BET_FAMILY_{safe}" with the
# dash sanitized to an underscore). Cell ids now include letter-only forms
# (BET_RF / BET_SF, the conditional cells 13/11) alongside the original
# letter+digit forms. Anything else dangles unread.
_ACCT_SUFFIX_RE = re.compile(r"(?:_P\d+|_BET_[A-Z]+\d*)$")


def _farm_compose_keys() -> dict[str, set[str]]:
    compose = ROOT / "docker-compose.paper-farm.yml"
    text = compose.read_text()
    keys: dict[str, set[str]] = {}
    current = None
    for line in text.splitlines():
        svc = re.match(r"^  ([\w-]+):\s*$", line)
        if svc:
            current = svc.group(1)
        for m in re.finditer(r"(?:^|\s)([A-Z][A-Z0-9_]+)\s*(?:=|$|:)", line):
            key = m.group(1)
            if re.match(r"^[A-Z][A-Z0-9_]+$", key) and current:
                keys.setdefault(key, set()).add(current)
    return keys


def _has_reader(key: str, corpus: str) -> bool:
    """Compose key is readable if code reads the literal key or the
    account-template form (LEVERAGE_P11 via f"LEVERAGE_{account}")."""
    if key in corpus:
        return True
    m = _ACCT_SUFFIX_RE.search(key)
    if m:
        base = key[: m.start() + 1]  # trailing underscore: LEVERAGE_
        if f"{base}{{" in corpus:
            return True
    return False


# ---------------------------------------------------------------------------
# Direction B: code -> config
# ---------------------------------------------------------------------------

_ENV_READ_RE = re.compile(
    r"""os\.environ\.get\(\s*f?["']([A-Z][A-Z0-9_]*_?)\{?|"""
    r"""os\.environ\[\s*f?["']([A-Z][A-Z0-9_]*_?)\{?|"""
    r"""os\.getenv\(\s*f?["']([A-Z][A-Z0-9_]*_?)\{?"""
)

# Per-account knob FAMILIES that are intentionally optional: no member is
# set in any live config today, and code reads them with a sane fallback.
# Keyed by family stem. Anything not listed here must have some family
# member set in a live config source.
ALLOWED_OPTIONAL: dict[str, str] = {
    "POSITION_SIZING_METHOD": "sizing method override; RiskConfig default used when unset",
}


def _code_env_reads(corpus: str) -> dict[str, set[str]]:
    """base env name read in code -> sample occurrences (for the failure msg).

    Only files that actually read env are attributed; we re-walk to get
    file:line for a helpful failure message."""
    reads: dict[str, set[str]] = {}
    for sub in PRODUCTION_DIRS:
        for p in (ROOT / sub).rglob("*.py"):
            _collect_reads(p, reads)
    for p in ROOT.glob("*.py"):
        _collect_reads(p, reads)
    return reads


def _collect_reads(path: Path, reads: dict[str, set[str]]) -> None:
    text = path.read_text(errors="replace")
    for lineno, line in enumerate(text.splitlines(), 1):
        if "os.environ" not in line and "os.getenv" not in line:
            continue
        for m in _ENV_READ_RE.finditer(line):
            base = next(g for g in m.groups() if g)
            if not base.isupper():
                continue
            reads.setdefault(base, set()).add(f"{path.relative_to(ROOT)}:{lineno}")


def _is_set_anywhere(base: str, sources: dict[str, set[str]]) -> bool:
    """A read base is 'set' if some config key equals it, equals its family
    stem, or extends the family (LEVERAGE_ read via f-string template;
    LEVERAGE_P11=2000 set in compose; PARTIAL_TP_ENABLED_A's family is live
    because PARTIAL_TP_ENABLED_B=1 is set in docker-compose.vps.yml)."""
    stem = _family_stem(base)
    if base in sources or stem in sources:
        return True
    return any(
        key.startswith(stem + "_") or key == stem
        for key in sources
    )


def _family_stem(base: str) -> str:
    """Strip per-account suffix (trailing _ template, _A.._D, _P<digits>)."""
    base = base.rstrip("_")
    base = re.sub(r"(?:_(?:A|B|C|D)|_P\d+)$", "", base)
    return base


class TestComposeKeysHaveReaders:
    """Direction A: nothing in the farm compose may dangle unread."""

    def test_every_farm_compose_env_has_a_reader(self):
        corpus = _production_corpus()
        keys = _farm_compose_keys()
        assert keys, "compose parser broke — farm compose must define env keys"
        dead = {k: sorted(v) for k, v in keys.items() if not _has_reader(k, corpus)}
        assert not dead, (
            "farm compose writes env vars that NO production code reads "
            "(ISSUE-100 class — silent no-op at the config end): "
            f"{dead}"
        )


class TestCodeEnvReadsAreSetOrDocumented:
    """Direction B (scoped): PER-ACCOUNT config reads must be set somewhere.

    This is the direction that caught the margin-gate bug: MT5_LEVERAGE_*
    was read by live_trader.py:2103/:2615 and m5_scalp_trader.py:1145 while
    every compose set LEVERAGE_* — silent 1:100 default, ~306 blocked
    signals. Scope is deliberately per-account reads (f-string templates +
    hardcoded _A/_B/_C/_D/_P<digits> names): that is where a wrong name
    silently misprices an account. Plain global tuning knobs
    (TRADE_BLOCKER_*, AUTO_RETRAIN_*, ...) are the standard
    optional-with-default idiom and are out of scope here.

    If this test fails, either SET the var in a live config source or add
    it to ALLOWED_OPTIONAL with a reason."""

    _ACCT_NAME_RE = re.compile(r"(?:_(?:A|B|C|D)|_P\d+)$")

    def test_per_account_env_reads_are_set_or_allowlisted(self):
        sources = _env_sources()
        reads = _code_env_reads(_production_corpus())
        dangling = {}
        for base, where in sorted(reads.items()):
            is_template = base.endswith("_")
            is_suffixed = bool(self._ACCT_NAME_RE.search(base))
            if not (is_template or is_suffixed):
                continue  # plain global knob — out of scope (see docstring)
            if _is_set_anywhere(base, sources):
                continue
            if _family_stem(base) in ALLOWED_OPTIONAL:
                continue
            dangling[base] = sorted(where)[:3]
        assert not dangling, (
            "production code reads PER-ACCOUNT env names whose whole family "
            "NO live config sets — each silently misprices that account's "
            "config forever (ISSUE-100 class, the margin-gate bug). Set the "
            "var in compose/.env or allowlist the family with a reason:\n"
            + "\n".join(f"  {k}: {v}" for k, v in dangling.items())
        )