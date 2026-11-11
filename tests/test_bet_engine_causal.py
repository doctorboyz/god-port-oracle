"""Causal tests — the cell engine (1 ไม้/cell, next-bar fill, stats).

run_backtest_cell must: fill at the bar AFTER the signal bar's open, hold
exactly one bet per cell at a time, re-arm on the resolution bar, leave
tail bets open, and produce honest stats (WR/expectancy/streaks exclude
unresolved bets).

Paths are hand-built OHLC (not padded closes) so every touch of TP/SL and
every re-arm is deliberate — the re-arm rule (the resolution bar itself
may signal the next bet) makes naive synthetic walks surprising.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from broky.bet import engine, entries  # noqa: E402
from broky.bet.contract import BetGeometry, POLICY_SL_FIRST, POLICY_TP_FIRST  # noqa: E402


def ohlc(rows: list[tuple[float, float, float, float]],
        start: str = "2026-01-05 00:00") -> pd.DataFrame:
    """rows = [(open, high, low, close), ...] — full manual control."""
    idx = pd.date_range(start=start, periods=len(rows), freq="5min")
    return pd.DataFrame(
        {"open": [r[0] for r in rows], "high": [r[1] for r in rows],
         "low": [r[2] for r in rows], "close": [r[3] for r in rows],
         "volume": [100.0] * len(rows)},
        index=idx,
    )


def quiet_bar(c: float, pad: float = 0.3) -> tuple[float, float, float, float]:
    return (c, c + pad, c - pad, c)


def breakout_cell(tp: float = 6.0, sl: float = 12.0, seed: float = 1000.0,
                  risk: float = 0.015, spread: float = 0.0) -> engine.CellSpec:
    return engine.CellSpec(
        cell_id="BET-X", family=entries.FAMILY_BREAKOUT,
        geometry=BetGeometry(tp, sl), seed_balance=seed,
        risk_fraction=risk, spread_cost_per_oz=spread,
    )


def two_bet_path() -> pd.DataFrame:
    """Two resolved breakout bets with a clean gap between them.

    Bet 1: signal bar 70 (close 101 > every prior high ~100.32), entry at
    bar 71's open (101), TP 107 touched by bar 76's high 107.8 while its
    CLOSE (105.5) stays under the prior max high — no immediate re-arm.
    Dip → quiet → bet 2: signal bar 100 (close 104.5 > prior max ~103.8),
    entry bar 101 open (104.5), TP 110.5 touched by bar 108's high 111.
    """
    rows = [quiet_bar(100.0 + (0.02 if i % 2 else -0.02)) for i in range(70)]
    rows += [
        (100.0, 101.3, 99.7, 101.0),    # bar 70 — breakout signal
        (101.0, 102.3, 100.7, 102.0),    # bar 71 — entry @ open 101
        (102.0, 103.3, 101.7, 103.0),
        (103.0, 104.3, 102.7, 104.0),
        (104.0, 105.3, 103.7, 105.0),
        (105.0, 106.3, 104.7, 106.0),
        (106.0, 107.8, 105.0, 105.5),    # bar 76 — TP 107 touched, close stays low
    ]
    rows += [quiet_bar(c) for c in
             (105.0, 104.5, 104.0, 103.5, 103.0, 102.5, 102.0, 101.5, 101.0)]
    rows += [quiet_bar(101.0 + (0.02 if i % 2 else -0.02)) for i in range(14)]
    rows += [
        (101.0, 104.8, 100.7, 104.5),   # bar 100 — second breakout signal
        (104.5, 105.3, 104.2, 105.0),   # bar 101 — entry @ open 104.5
        (105.0, 106.3, 104.7, 106.0),
        (106.0, 107.3, 105.7, 107.0),
        (107.0, 108.3, 106.7, 108.0),
        (108.0, 109.3, 107.7, 109.0),
        (109.0, 111.0, 108.7, 109.5),   # bar 107 — TP 110.5 touched
    ]
    return ohlc(rows)


class TestOneBetAtATime:
    def test_no_overlapping_bets(self):
        df = two_bet_path()
        records = engine.run_backtest_cell(df, breakout_cell())
        assert len(records) == 2
        spans = sorted(
            (r.entry_ts, r.exit_ts or df.index[-1].to_pydatetime())
            for r in records
        )
        for (_, end), (start2, _) in zip(spans, spans[1:]):
            assert start2 > end, "bets must never overlap (1 ไม้/cell)"

    def test_signals_while_open_are_skipped(self):
        # During bet 1 (bars 71-76) closes keep breaking the prior-20 high
        # (e.g. bar 72's 103.0 > 102.3) — the engine must NOT open bet 2
        # until bet 1 resolves at bar 76.
        df = two_bet_path()
        records = engine.run_backtest_cell(df, breakout_cell())
        assert records[0].entry_ts == df.index[71].to_pydatetime()
        assert records[1].entry_ts == df.index[101].to_pydatetime()

    def test_flat_cells_report_nothing(self):
        # Steady strong trend: ADX ≥ 20 → family A's MR gate never opens.
        rows = [quiet_bar(100.0 + 0.5 * i) for i in range(200)]
        df = ohlc(rows)
        records = engine.run_backtest_cell(
            df, engine.CellSpec(
                cell_id="BET-A", family=entries.FAMILY_MR_BOLL,
                geometry=BetGeometry(6, 12), seed_balance=1000.0,
                risk_fraction=0.015))
        assert records == []


class TestFillSemantics:
    def test_entry_fills_at_next_bar_open(self):
        df = two_bet_path()
        records = engine.run_backtest_cell(df, breakout_cell())
        first = records[0]
        assert first.signal_ts == df.index[70].to_pydatetime()
        assert first.entry_ts == df.index[71].to_pydatetime()
        assert first.entry_price == pytest.approx(101.0)  # bar 71's open

    def test_signal_bar_is_closed_before_entry(self):
        df = two_bet_path()
        records = engine.run_backtest_cell(df, breakout_cell())
        for r in records:
            assert r.signal_ts < r.entry_ts


class TestCompounding:
    def test_risk_scales_with_balance(self):
        df = two_bet_path()
        spec = breakout_cell(seed=1000.0, risk=0.015)
        records = engine.run_backtest_cell(df, spec)
        resolved = [r for r in records if r.status == engine.STATUS_RESOLVED]
        assert len(resolved) == 2
        assert resolved[0].risk_amount == pytest.approx(15.0)
        # bet 1 won 1.25oz × $6 = $7.50 (spread disabled in this spec)
        assert resolved[0].pnl == pytest.approx(7.5)
        expected_second = (1000.0 + resolved[0].pnl) * 0.015
        assert resolved[1].risk_amount == pytest.approx(expected_second)

    def test_final_balance_reflects_resolved_pnl(self):
        df = two_bet_path()
        spec = breakout_cell()
        records = engine.run_backtest_cell(df, spec)
        resolved = [r for r in records if r.status == engine.STATUS_RESOLVED]
        assert resolved[-1].balance_after == pytest.approx(
            1000.0 + sum(r.pnl for r in resolved))


def smash_path() -> pd.DataFrame:
    """One bet whose FIRST post-entry bar smashes both levels."""
    rows = [quiet_bar(100.0 + (0.02 if i % 2 else -0.02)) for i in range(70)]
    rows += [
        (100.0, 101.3, 99.7, 101.0),   # bar 70 — breakout signal
        (101.0, 101.3, 100.7, 101.0),  # bar 71 — entry @ open 101
        (101.0, 116.0, 84.0, 85.0),    # bar 72 — high ≥ TP107 AND low ≤ SL89
    ]
    return ohlc(rows)


class TestAmbiguityEdges:
    def test_both_edges_reported(self):
        df = smash_path()
        spec = breakout_cell(spread=0.0)
        pes = engine.run_backtest_cell(df, spec, POLICY_SL_FIRST)
        opt = engine.run_backtest_cell(df, spec, POLICY_TP_FIRST)
        assert [r.ambiguous for r in pes] == [True]
        assert [r.ambiguous for r in opt] == [True]
        assert pes[0].outcome == "sl"
        assert opt[0].outcome == "tp"
        assert pes[0].pnl == pytest.approx(-12.0 * 1.25)
        assert opt[0].pnl == pytest.approx(6.0 * 1.25)


class TestOpenTailBets:
    def test_unresolved_bet_is_reported_open_not_counted(self):
        rows = [quiet_bar(100.0 + (0.02 if i % 2 else -0.02)) for i in range(70)]
        rows += [
            (100.0, 101.3, 99.7, 101.0),    # breakout signal
            (101.0, 101.8, 100.7, 101.5),   # entry @ 101, TP 107 never reached
        ]
        df = ohlc(rows)
        records = engine.run_backtest_cell(df, breakout_cell())
        assert len(records) == 1
        assert records[0].status == engine.STATUS_OPEN
        stats = engine.cell_stats(records, BetGeometry(6, 12),
                                  final_balance=1000.0)
        assert stats["n_open"] == 1
        assert stats["n_resolved"] == 0
        assert stats["wr"] == 0.0
        assert stats["expectancy"] == 0.0


class TestStats:
    def test_wr_expectancy_and_streak(self):
        records = []
        base = datetime(2026, 1, 5)
        for k, (outcome, pnl) in enumerate(
            [("tp", 7.25), ("sl", -15.25), ("sl", -15.25), ("tp", 7.25),
             ("sl", -15.25), ("tp", 7.25)]
        ):
            records.append(engine.BetRecord(
                cell_id="BET-X", direction="BUY",
                signal_ts=base + timedelta(minutes=5 * k),
                entry_ts=base + timedelta(minutes=5 * k + 5),
                entry_price=100.0, qty_oz=1.25, tp_price=106.0,
                sl_price=88.0, risk_amount=15.0,
                status=engine.STATUS_RESOLVED, outcome=outcome,
                exit_ts=base + timedelta(minutes=5 * k + 60),
                exit_price=106.0 if outcome == "tp" else 88.0,
                pnl=pnl, bars_to_finish=5,
            ))
        stats = engine.cell_stats(records, BetGeometry(6, 12))
        assert stats["n_resolved"] == 6
        assert stats["wins"] == 3
        assert stats["wr"] == pytest.approx(0.5)
        assert stats["expectancy"] == pytest.approx((7.25 * 3 - 15.25 * 3) / 6)
        assert stats["longest_losing_streak"] == 2
        assert stats["avg_bars_to_finish"] == pytest.approx(5.0)

    def test_breakeven_line_uses_geometry(self):
        stats = engine.cell_stats([], BetGeometry(8, 8), spread_cost_per_oz=0.20)
        assert stats["breakeven_wr"] == pytest.approx(0.5)
        assert stats["breakeven_wr_with_spread"] == pytest.approx(8.2 / 16.0)


class TestDeterminism:
    def test_same_input_same_output(self):
        df = two_bet_path()
        spec = breakout_cell()
        a = engine.run_backtest_cell(df, spec)
        b = engine.run_backtest_cell(df, spec)
        assert [(r.entry_ts, r.entry_price, r.pnl) for r in a] == \
               [(r.entry_ts, r.entry_price, r.pnl) for r in b]

    def test_matches_contract_walk(self):
        # The engine's bar stepping must equal resolve_bet_on_bars on the
        # same window (engine เดียวกัน — one resolution semantics).
        from broky.bet.contract import open_bet, resolve_bet_on_bars

        df = two_bet_path()
        records = engine.run_backtest_cell(df, breakout_cell())
        first = records[0]
        bet = open_bet(
            cell_id=first.cell_id, direction=first.direction,
            signal_ts=first.signal_ts, entry_ts=first.entry_ts,
            entry_price=first.entry_price,
            geometry=BetGeometry(6.0, 12.0), risk_amount=first.risk_amount,
            spread_cost_per_oz=0.0,
        )
        window = df.loc[df.index >= first.entry_ts]
        walked = resolve_bet_on_bars(bet, window)
        assert walked.outcome == first.outcome
        assert walked.exit_ts == first.exit_ts
        assert walked.bars_to_finish == first.bars_to_finish
        assert walked.pnl == pytest.approx(first.pnl)
