#!/usr/bin/env python3
"""Backup paper-farm SQLite DBs (data/farm/*/*.db → data/farm/backup/).

Runs daily via crontab. Uses sqlite3's online-backup API so the live engine
(WAL mode, writers active) is captured consistently. One stamped copy per DB
per day; the league-table cron keeps nothing, this is the only safety net for
the experiment's history (bind-mount SQLite is the whole payload — a corrupt
farm1.db loses every variant's record).

Also prunes: keeps the newest 30 daily copies per DB.
"""

from __future__ import annotations

import glob
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKUP_DIR = ROOT / "data" / "farm" / "backup"
KEEP_COPIES = 30


def backup_all() -> list[Path]:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    written: list[Path] = []
    for db_path in sorted(glob.glob(str(ROOT / "data" / "farm" / "*" / "*.db"))):
        if Path(db_path).parent == BACKUP_DIR:
            continue  # never back up the backups (glob would snowball names daily)
        src_conn = sqlite3.connect(db_path)
        try:
            name = Path(db_path).name.replace(".", "_")
            dest = BACKUP_DIR / f"{name}-{stamp}.db"
            dst_conn = sqlite3.connect(str(dest))
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
            written.append(dest)
        finally:
            src_conn.close()
    return written


def prune() -> None:
    """Keep only the newest KEEP_COPIES stamped copies per DB name."""
    by_name: dict[str, list[Path]] = {}
    for f in BACKUP_DIR.glob("*.db"):
        parts = f.name.rsplit("-", 1)
        if len(parts) == 2 and parts[1].removesuffix(".db").isdigit():
            by_name.setdefault(parts[0], []).append(f)
    for name, files in by_name.items():
        files.sort()
        for old in files[:-KEEP_COPIES]:
            old.unlink()


def main() -> None:
    written = backup_all()
    prune()
    for f in written:
        print(f"backed up -> {f.name} ({f.stat().st_size} bytes)")


if __name__ == "__main__":
    main()