"""Causal test — graceful close folds the WAL into the main bet db.

Live incident 2026-11-11 15:20-15:33 UTC: three committed bets lived only in
bet.db-wal on the container's virtiofs bind mount; when `docker compose up -d
--build` recreated the container the WAL was gone by the time the new process
opened the db, while the main file kept the older (checkpointed) rows. The
engine now installs a SIGTERM/SIGINT handler that calls conn.close() so
SQLite checkpoints every committed frame into the main file before the
process exits — a redeploy never depends on WAL survival.

The causal claim: with the handler, a SIGTERM'd process leaves a main db
file that contains the committed row WITHOUT its -wal sidecar. Without the
handler (control), the same kill leaves the row only in the -wal sidecar —
the main file alone does not contain it. That difference is exactly the
failure mode that orphaned the three live bets.
"""

from __future__ import annotations

import shutil
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

_CHILD = r"""
import sys, time
sys.path.insert(0, {root!r})
from pathlib import Path
from broky.bet import store

conn = store.connect(Path({db!r}))
{install}
conn.execute("CREATE TABLE probe (v TEXT)")
conn.commit()
conn.execute("INSERT INTO probe VALUES ('committed-before-sigterm')")
conn.commit()
print("ready", flush=True)
while True:
    time.sleep(1)
"""


def _spawn_and_sigterm(tmp_path: Path, install_handler: bool) -> Path:
    db = tmp_path / "bet.db"
    install = (
        "from scripts.bet_engine import _install_graceful_close; "
        "_install_graceful_close(conn)"
        if install_handler else "pass"
    )
    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", _CHILD.format(
            root=str(ROOT), db=str(db), install=install,
        )],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "ready"
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
    finally:
        proc.kill()
    return db


def _row_in_main_file_only(db: Path, tmp_path: Path) -> bool:
    """Open a COPY of the main db file with NO -wal sidecar (what survives a
    redeploy that loses the WAL) and check whether the committed row is there."""
    lone = tmp_path / "lone.db"
    shutil.copy(db, lone)
    conn = sqlite3.connect(lone)
    try:
        return conn.execute(
            "SELECT 1 FROM probe WHERE v = 'committed-before-sigterm'"
        ).fetchone() is not None
    except sqlite3.OperationalError:
        # not even the table made it into the main file — the row is
        # certainly not there either (control case)
        return False
    finally:
        conn.close()


class TestGracefulClose:
    def test_sigterm_handler_folds_wal_into_main_file(self, tmp_path):
        db = _spawn_and_sigterm(tmp_path, install_handler=True)
        assert _row_in_main_file_only(db, tmp_path), (
            "SIGTERM with the graceful-close handler must leave the committed "
            "row readable from the MAIN db file without the WAL sidecar"
        )
        # the checkpoint on close also removes the sidecars
        assert not (tmp_path / "bet.db-wal").exists()

    def test_without_handler_row_stays_only_in_wal(self, tmp_path):
        """Control: the same kill WITHOUT the handler leaves the main file
        without the row (it lives only in the -wal sidecar) — the exact
        window that orphaned the live bets on 2026-11-11."""
        db = _spawn_and_sigterm(tmp_path, install_handler=False)
        assert not _row_in_main_file_only(db, tmp_path)

    @pytest.mark.skipif(not Path("/proc").is_dir(), reason="container-only check")
    def test_handler_installed_for_sigint_and_sigterm(self):
        """The engine registers both stop signals docker sends."""
        import scripts.bet_engine as be

        conn = sqlite3.connect(":memory:")
        be._install_graceful_close(conn)
        assert signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL
        assert signal.getsignal(signal.SIGINT) is not signal.SIG_DFL
