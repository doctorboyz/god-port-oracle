"""Cell engine — one bet at a time per cell, shared by backtest and live.

The backtest loop and the live container both drive the SAME primitives
(``entries.family_signal_series`` / ``entries.signal_on_last_bar`` +
``contract.open_bet`` + ``contract.BetResolver``), so a backtest bar and a
live cycle seeing the same closed bar make identical decisions. That is the
"engine เดียวกับ live" requirement of the task — no second implementation
to drift.

Backtest semantics (no lookahead, locked by tests):
- signal at closed bar i → fill at the OPEN of bar i+1 (the next bar — in
  live the fill is the current tick, the same instant after bar close)
- while a bet is open the cell takes no new signal (1 ไม้/cell)
- a resolved bet re-arms immediately: the resolution bar itself may signal
- compounding: each bet risks ``risk_fraction`` of the CURRENT balance
- data ending before resolution leaves the bet OPEN (reported, excluded
  from WR/expectancy)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

import pandas as pd

from broky.bet import entries
from broky.bet.contract import (
    BetGeometry, OpenBet, POLICIES, POLICY_SL_FIRST, BetResolver, open_bet,
)

STATUS_OPEN = "open"
STATUS_RESOLVED = "resolved"


@dataclass(frozen=True)
class CellSpec:
    """One true-bet cell: entry family + fixed contract geometry."""
    cell_id: str
    family: str
    geometry: BetGeometry
    seed_balance: float
    risk_fraction: float
    boll_threshold: float = entries.MR_BOLL_THRESHOLD_DEFAULT
    spread_cost_per_oz: float = 0.20


@dataclass
class BetRecord:
    """One bet as persisted/reported (mirrors the store's bet_bets row)."""
    cell_id: str
    direction: str
    signal_ts: datetime
    entry_ts: datetime
    entry_price: float
    qty_oz: float
    tp_price: float
    sl_price: float
    risk_amount: float
    status: str                       # open / resolved
    outcome: Optional[str] = None
    exit_ts: Optional[datetime] = None
    exit_price: Optional[float] = None
    pnl: Optional[float] = None
    mfe_price: float = 0.0
    mae_price: float = 0.0
    bars_to_finish: int = 0
    ambiguous: bool = False
    events: list[dict[str, Any]] = field(default_factory=list)
    resolution_method: str = "bars"
    balance_after: Optional[float] = None


def _ts(x: Any) -> datetime:
    return x.to_pydatetime() if hasattr(x, "to_pydatetime") else x


def run_backtest_cell(
    df: pd.DataFrame,
    spec: CellSpec,
    policy: str = POLICY_SL_FIRST,
) -> list[BetRecord]:
    """Replay one cell over the full closed-bar history.

    `df` must be strictly time-ascending closed M5 bars with
    open/high/low/close columns. Returns every bet in order (resolved +
    still-open).
    """
    if policy not in POLICIES:
        raise ValueError(f"policy must be one of {POLICIES}, got {policy!r}")
    if not {"open", "high", "low", "close"}.issubset(df.columns):
        raise ValueError("df must have open/high/low/close columns")

    signals = entries.family_signal_series(
        spec.family, df, boll_threshold=spec.boll_threshold,
    )
    balance = spec.seed_balance
    records: list[BetRecord] = []
    resolver: Optional[BetResolver] = None
    record: Optional[BetRecord] = None

    i = entries.MIN_HISTORY_BARS
    n = len(df)
    while i < n:
        sig = str(signals.iloc[i]) if i < n else ""

        # 1) advance an open bet with this bar (the entry bar included:
        #    its open is the fill, its high/low belong to the first step)
        if resolver is not None and record is not None:
            state = resolver.step_bar(
                _ts(df.index[i]), float(df["high"].iloc[i]), float(df["low"].iloc[i]),
            )
            if state.outcome is not None:
                _apply_resolution(record, state)
                balance += record.pnl or 0.0
                record.balance_after = balance
                records.append(record)
                resolver = None
                record = None
                # fall through: this same bar may arm the next bet below

        # 2) no open bet + signal on this closed bar → fill next bar's open
        if resolver is None and i + 1 < n and sig in (entries.BUY, entries.SELL):
            entry_idx = i + 1
            entry_price = float(df["open"].iloc[entry_idx])
            bet = open_bet(
                cell_id=spec.cell_id,
                direction=sig,
                signal_ts=_ts(df.index[i]),
                entry_ts=_ts(df.index[entry_idx]),
                entry_price=entry_price,
                geometry=spec.geometry,
                risk_amount=balance * spec.risk_fraction,
                spread_cost_per_oz=spec.spread_cost_per_oz,
            )
            record = BetRecord(
                cell_id=spec.cell_id, direction=bet.direction,
                signal_ts=bet.signal_ts, entry_ts=bet.entry_ts,
                entry_price=bet.entry_price, qty_oz=bet.qty_oz,
                tp_price=bet.tp_price, sl_price=bet.sl_price,
                risk_amount=bet.risk_amount, status=STATUS_OPEN,
            )
            resolver = BetResolver(bet, policy)
            # step the entry bar immediately, then continue from the next bar
            state = resolver.step_bar(
                _ts(df.index[entry_idx]),
                float(df["high"].iloc[entry_idx]),
                float(df["low"].iloc[entry_idx]),
            )
            if state.outcome is not None:
                _apply_resolution(record, state)
                balance += record.pnl or 0.0
                record.balance_after = balance
                records.append(record)
                resolver = None
                record = None
                # re-evaluate the entry bar's own signal for the next bet
                i = entry_idx
            else:
                i = entry_idx + 1
            continue

        i += 1

    if resolver is not None and record is not None:
        records.append(record)  # still open at end of data
    return records


def _apply_resolution(record: BetRecord, state) -> None:
    record.status = STATUS_RESOLVED
    record.outcome = state.outcome
    record.exit_ts = state.exit_ts
    record.exit_price = state.exit_price
    record.pnl = state.pnl
    record.mfe_price = state.mfe_price
    record.mae_price = state.mae_price
    record.bars_to_finish = state.bars_to_finish
    record.ambiguous = state.ambiguous
    record.events = list(state.events)
    record.resolution_method = state.resolution_method


def longest_losing_streak(pnls: list[float]) -> int:
    """Longest run of consecutive resolved losses (pnl < 0)."""
    best = cur = 0
    for pnl in pnls:
        if pnl < 0:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def cell_stats(records: list[BetRecord], geometry: BetGeometry,
               spread_cost_per_oz: float = 0.20,
               final_balance: Optional[float] = None) -> dict[str, Any]:
    """Per-cell report row: n / WR / expectancy / breakeven / streaks.

    Open (unresolved) bets are counted separately and never enter WR or
    expectancy — the data simply ended first. `breakeven_wr_with_spread`
    is the WR at which NET expectancy (spread booked on every bet) is 0:
    win nets (tp−s), loss costs (sl+s) → BE = (sl+s)/(tp+sl), per ounce.
    """
    resolved = [r for r in records if r.status == STATUS_RESOLVED]
    open_bets = [r for r in records if r.status == STATUS_OPEN]
    wins = [r for r in resolved if r.outcome == "tp"]
    n = len(resolved)
    pnls = [r.pnl for r in resolved if r.pnl is not None]
    bars = [r.bars_to_finish for r in resolved]
    ambiguous = [r for r in resolved if r.ambiguous]

    wr = (len(wins) / n) if n else 0.0
    expectancy = (sum(pnls) / n) if n else 0.0
    qty = resolved[0].qty_oz if resolved else 0.0
    gross_expectancy = (
        wr * geometry.tp_dist * qty - (1 - wr) * geometry.sl_dist * qty
    ) if n else 0.0

    return {
        "n_bets": len(records),
        "n_resolved": n,
        "n_open": len(open_bets),
        "wins": len(wins),
        "losses": n - len(wins),
        "wr": wr,
        "expectancy": expectancy,                    # net of spread cost
        "gross_expectancy": gross_expectancy,        # pure geometry, no spread
        "breakeven_wr": geometry.breakeven_wr,
        "breakeven_wr_with_spread": (
            (geometry.sl_dist + spread_cost_per_oz)
            / (geometry.tp_dist + geometry.sl_dist)
        ),
        "avg_bars_to_finish": (sum(bars) / len(bars)) if bars else 0.0,
        "longest_losing_streak": longest_losing_streak(pnls),
        "n_ambiguous": len(ambiguous),
        "avg_mfe": (sum(r.mfe_price for r in resolved) / n) if n else 0.0,
        "avg_mae": (sum(r.mae_price for r in resolved) / n) if n else 0.0,
        "total_pnl": sum(pnls),
        "final_balance": final_balance,
    }
