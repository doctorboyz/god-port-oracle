"""Core integrity lock — the kill-switch task's IRON scope, made permanent.

doctorboyz order 2026-11-16 (kill switch supervisor): the supervisor was
delivered as NEW files only. The paper farm on the Mac mini builds from
Dockerfile + metty/ + broky/ + shared/ — the same tree as the VPS engines —
and farm1's P22 winner was mid-validation, so this task had to not move a
single byte of the shared core. That constraint does not end with the task:
any future change to these files changes the farm image too, so it must be
a CONSCIOUS act.

This test pins a sha256 manifest of every file in the IRON scope:

    metty/**, broky/**, shared/**          (engine + farm runtime code)
    Dockerfile, docker/mt5/**              (image + bridge server)
    docker-compose.paper-farm.yml          (farm topology)
    farm_variants.yml                      (farm variant contract)

docker-compose.vps.yml is deliberately NOT locked (it is the file this
supervisor lives in; its own additive contract is pinned in
tests/test_kill_switch_infra.py).

If this test fails you changed a locked file. Either revert, or if the
change is genuinely intended (and the farm impact is considered), update
the manifest with:

    python3 tests/test_core_integrity.py --update

The manifest is committed alongside this test so the lock covers every
checkout, not just the machine that made it.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = Path(__file__).parent / "core_integrity_manifest.json"

IRON_DIRS = ("metty", "broky", "shared")
IRON_FILES = (
    "Dockerfile",
    "docker-compose.paper-farm.yml",
    "farm_variants.yml",
)


def _iron_paths() -> list[Path]:
    paths: list[Path] = []
    for sub in IRON_DIRS:
        paths += sorted(p for p in (ROOT / sub).rglob("*") if p.is_file())
    for rel in IRON_FILES:
        p = ROOT / rel
        if p.exists():
            paths.append(p)
    paths += sorted(p for p in (ROOT / "docker").rglob("*") if p.is_file())
    # machine-local artifacts are not source — they regenerate per machine
    return sorted({
        p for p in set(paths)
        if "__pycache__" not in p.parts
        and p.suffix not in (".pyc", ".pyo")
        and p.name != ".DS_Store"
    })


def build_manifest() -> dict[str, str]:
    manifest: dict[str, str] = {}
    for p in _iron_paths():
        rel = p.relative_to(ROOT).as_posix()
        manifest[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return manifest


def write_manifest() -> int:
    manifest = build_manifest()
    MANIFEST_PATH.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {MANIFEST_PATH} ({len(manifest)} files)")
    return 0


class TestCoreIntegrity:
    def test_iron_scope_matches_manifest(self):
        """Every IRON-scope file hashes to its committed manifest entry.

        Failure means a locked file changed. Revert it, or — if the change
        is intended AND its paper-farm impact is considered — regenerate:
            python3 tests/test_core_integrity.py --update
        """
        if not MANIFEST_PATH.exists():
            # manifest must exist — a checkout without it has no lock
            self.fail(
                f"{MANIFEST_PATH.name} missing — regenerate with "
                "`python3 tests/test_core_integrity.py --update` and commit it"
            )
        committed = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        current = build_manifest()
        changed = sorted(
            k for k in committed
            if k in current and committed[k] != current[k]
        )
        added = sorted(k for k in current if k not in committed)
        removed = sorted(k for k in committed if k not in current)
        assert not changed, (
            "IRON-scope files were modified (they build the paper-farm "
            "image too — see docstring): " + ", ".join(changed)
        )
        assert not added, (
            "new files inside the IRON scope must be added to the manifest "
            "deliberately (python3 tests/test_core_integrity.py --update): "
            + ", ".join(added)
        )
        assert not removed, (
            "IRON-scope files deleted: " + ", ".join(removed)
        )

    def test_manifest_is_nontrivial(self):
        """Sanity: the lock actually covers the tree, not an empty file."""
        committed = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        assert len(committed) > 50, (
            f"manifest covers only {len(committed)} files — the core tree "
            "alone is far bigger; the lock is broken"
        )
        assert any(k.startswith("metty/") for k in committed)
        assert any(k.startswith("broky/") for k in committed)
        assert "Dockerfile" in committed
        assert "docker/mt5/mt5_bridge_server.py" in committed


if __name__ == "__main__":
    if "--update" in sys.argv:
        raise SystemExit(write_manifest())
    raise SystemExit(
        "locked-file test module — run via pytest, or pass --update to "
        "regenerate the manifest"
    )
