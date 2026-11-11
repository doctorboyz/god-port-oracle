"""SQLite persistence for the true-bet farm — data/farm/bet/bet.db.

Schema is bet-farm-specific (cells + bets) and intentionally separate from
the LiveTrader schema in metty.core.db: farm1/farm2 keep their own DBs
untouched (iron constraint — additive only), and bet rows carry contract
fields the swing schema has no place for (mfe/mae/bars_to_finish/event
order/ambiguity/resolution method).

The live container seeds its 9 cells on boot (idempotent, mirroring
oracle_runner._seed_portfolio_accounts) — every cell starts at $1,000
(คุณหมอ-approved seed, bet = 1.2–2% profile).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from broky.bet.contract import (
    BetGeometry, OpenBet, POLICY_SL_FIRST, BetResolver, Resolution,
)
from broky.bet.engine import BetRecord, CellSpec

SCHEMA = """
CREATE TABLE IF NOT EXISTS bet_cells (
    cell_id        TEXT PRIMARY KEY,
    family         TEXT NOT NULL,
    tp_dist        REAL NOT NULL,
    sl_dist        REAL NOT NULL,
    boll_threshold REAL NOT NULL,
    seed_balance   REAL NOT NULL,
    balance        REAL NOT NULL,
    risk_fraction  REAL NOT NULL,
    spread_cost_per_oz REAL NOT NULL,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bet_bets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    cell_id         TEXT NOT NULL,
    direction       TEXT NOT NULL,
    signal_ts       TEXT NOT NULL,
    entry_ts        TEXT NOT NULL,
    entry_price     REAL NOT NULL,
    qty_oz          REAL NOT NULL,
    tp_price        REAL NOT NULL,
    sl_price        REAL NOT NULL,
    risk_amount     REAL NOT NULL,
    status          TEXT NOT NULL,          -- open | resolved
    outcome         TEXT,                   -- tp | sl | NULL
    exit_ts         TEXT,
    exit_price      REAL,
    pnl             REAL,
    mfe_price       REAL NOT NULL DEFAULT 0,
    mae_price       REAL NOT NULL DEFAULT 0,
    bars_to_finish  INTEGER NOT NULL DEFAULT 0,
    last_tick_msc   INTEGER NOT NULL DEFAULT 0,
    ambiguous       INTEGER NOT NULL DEFAULT 0,
    events          TEXT NOT NULL DEFAULT '[]',
    resolution_method TEXT NOT NULL DEFAULT 'bars',
    balance_after   REAL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bet_bets_cell ON bet_bets(cell_id, status);
"""


def _now_iso() -> str:
    # Naive-UTC (matches the engine's datetime convention; avoids mixing
    # tz-aware and naive values in comparisons after a fromisoformat round-trip)
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(
        timespec="seconds")


def connect(db_path: Path) -> sqlite3.Connection:
    """Open + migrate the bet DB (WAL for the live container's hot loop)."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def seed_cells(conn: sqlite3.Connection, specs: list[CellSpec]) -> list[str]:
    """Idempotently seed cells (default $1,000 each). Returns created ids."""
    created: list[str] = []
    now = _now_iso()
    for spec in specs:
        row = conn.execute(
            "SELECT 1 FROM bet_cells WHERE cell_id = ?", (spec.cell_id,)
        ).fetchone()
        if row is not None:
            continue
        conn.execute(
            """INSERT INTO bet_cells
               (cell_id, family, tp_dist, sl_dist, boll_threshold,
                seed_balance, balance, risk_fraction, spread_cost_per_oz,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (spec.cell_id, spec.family, spec.geometry.tp_dist,
             spec.geometry.sl_dist, spec.boll_threshold, spec.seed_balance,
             spec.seed_balance, spec.risk_fraction, spec.spread_cost_per_oz,
             now, now),
        )
        created.append(spec.cell_id)
    conn.commit()
    return created


def load_cell_specs(conn: sqlite3.Connection) -> list[CellSpec]:
    """All seeded cells, ordered by id (from the DB — the container's truth)."""
    specs: list[CellSpec] = []
    for row in conn.execute("SELECT * FROM bet_cells ORDER BY cell_id"):
        specs.append(CellSpec(
            cell_id=row["cell_id"],
            family=row["family"],
            geometry=BetGeometry(tp_dist=row["tp_dist"],
                                 sl_dist=row["sl_dist"]),
            seed_balance=row["seed_balance"],
            risk_fraction=row["risk_fraction"],
            boll_threshold=row["boll_threshold"],
            spread_cost_per_oz=row["spread_cost_per_oz"],
        ))
    return specs


def cell_balance(conn: sqlite3.Connection, cell_id: str) -> float:
    row = conn.execute(
        "SELECT balance FROM bet_cells WHERE cell_id = ?", (cell_id,)
    ).fetchone()
    return float(row["balance"]) if row else 0.0


def insert_bet(conn: sqlite3.Connection, bet: OpenBet) -> int:
    """Persist a freshly opened bet; returns its row id."""
    now = _now_iso()
    cur = conn.execute(
        """INSERT INTO bet_bets
           (cell_id, direction, signal_ts, entry_ts, entry_price, qty_oz,
            tp_price, sl_price, risk_amount, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)""",
        (bet.cell_id, bet.direction, bet.signal_ts.isoformat(),
         bet.entry_ts.isoformat(), bet.entry_price, bet.qty_oz,
         bet.tp_price, bet.sl_price, bet.risk_amount, now, now),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_open_bet(conn: sqlite3.Connection, cell_id: str) -> Optional[sqlite3.Row]:
    """The cell's open bet row, or None (one bet at a time per cell)."""
    return conn.execute(
        "SELECT * FROM bet_bets WHERE cell_id = ? AND status = 'open' "
        "ORDER BY id DESC LIMIT 1",
        (cell_id,),
    ).fetchone()


def save_progress(conn: sqlite3.Connection, bet_id: int,
                   resolver: BetResolver) -> None:
    """Persist a still-open bet's running state (MFE/MAE/bars/tick cursor).

    Called every live cycle so a container restart resumes exactly where the
    resolver left off.
    """
    state = resolver.state
    conn.execute(
        """UPDATE bet_bets
           SET mfe_price = ?, mae_price = ?, bars_to_finish = ?,
               last_tick_msc = ?, events = ?, updated_at = ?
           WHERE id = ?""",
        (state.mfe_price, state.mae_price, state.bars_to_finish,
         resolver.last_tick_msc, json.dumps(state.events), _now_iso(), bet_id),
    )
    conn.commit()


def resolve_bet_row(conn: sqlite3.Connection, row: sqlite3.Row,
                    state: Resolution) -> None:
    """Book a resolution: close the bet row, credit the cell's balance.

    pnl already has the round-trip spread cost booked (contract semantics).
    """
    now = _now_iso()
    conn.execute(
        """UPDATE bet_bets
           SET status = 'resolved', outcome = ?, exit_ts = ?, exit_price = ?,
               pnl = ?, mfe_price = ?, mae_price = ?, bars_to_finish = ?,
               ambiguous = ?, events = ?, resolution_method = ?, updated_at = ?
           WHERE id = ?""",
        (state.outcome,
         state.exit_ts.isoformat() if state.exit_ts else None,
         state.exit_price, state.pnl, state.mfe_price, state.mae_price,
         state.bars_to_finish, int(state.ambiguous),
         json.dumps(state.events), state.resolution_method, now, row["id"]),
    )
    if state.pnl is not None:
        conn.execute(
            "UPDATE bet_cells SET balance = balance + ?, updated_at = ? "
            "WHERE cell_id = ?",
            (state.pnl, now, row["cell_id"]),
        )
    conn.commit()


def build_resolver(row: sqlite3.Row, spread_cost_per_oz: float,
                  policy: str = POLICY_SL_FIRST) -> BetResolver:
    """Rehydrate a BetResolver from a persisted open-bet row."""
    bet = OpenBet(
        cell_id=row["cell_id"],
        direction=row["direction"],
        signal_ts=datetime.fromisoformat(row["signal_ts"]),
        entry_ts=datetime.fromisoformat(row["entry_ts"]),
        entry_price=row["entry_price"],
        qty_oz=row["qty_oz"],
        tp_price=row["tp_price"],
        sl_price=row["sl_price"],
        risk_amount=row["risk_amount"],
        spread_cost=row["qty_oz"] * spread_cost_per_oz,
    )
    return BetResolver(
        bet, policy,
        mfe_price=row["mfe_price"], mae_price=row["mae_price"],
        bars_to_finish=row["bars_to_finish"],
        last_tick_msc=row["last_tick_msc"],
    )


def rows_to_records(rows: list[sqlite3.Row]) -> list[BetRecord]:
    """Adapt DB rows to BetRecord for stats/reports."""
    return [
        BetRecord(
            cell_id=r["cell_id"], direction=r["direction"],
            signal_ts=datetime.fromisoformat(r["signal_ts"]),
            entry_ts=datetime.fromisoformat(r["entry_ts"]),
            entry_price=r["entry_price"], qty_oz=r["qty_oz"],
            tp_price=r["tp_price"], sl_price=r["sl_price"],
            risk_amount=r["risk_amount"], status=r["status"],
            outcome=r["outcome"],
            exit_ts=datetime.fromisoformat(r["exit_ts"]) if r["exit_ts"] else None,
            exit_price=r["exit_price"], pnl=r["pnl"],
            mfe_price=r["mfe_price"], mae_price=r["mae_price"],
            bars_to_finish=r["bars_to_finish"],
            ambiguous=bool(r["ambiguous"]),
            events=json.loads(r["events"] or "[]"),
            resolution_method=r["resolution_method"],
            balance_after=r["balance_after"],
        )
        for r in rows
    ]
