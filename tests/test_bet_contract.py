"""Unit tests — the bet contract + resolution (T3 acceptance).

Happy paths, ambiguous same-bar handling (both edges), tick resolution
ordering, MFE/MAE/bars accounting, sizing, and restart-resume state.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from broky.bet.contract import (  # noqa: E402
    BetGeometry, BetResolver, POLICY_SL_FIRST, POLICY_TP_FIRST,
    OUTCOME_SL, OUTCOME_TP, open_bet, resolve_bet_on_bars, resolve_bet_on_ticks,
)

GEOM = BetGeometry(tp_dist=6.0, sl_dist=12.0)
T0 = datetime(2026, 1, 5, 0, 0)


def bars(path: list[tuple[float, float]]) -> pd.DataFrame:
    """path = [(high, low), ...] starting at the entry bar."""
    idx = pd.date_range(start=T0, periods=len(path), freq="5min")
    return pd.DataFrame(
        {"high": [h for h, _ in path], "low": [l for _, l in path],
         "open": [l for _, l in path], "close": [h for h, _ in path]},
        index=idx,
    )


def long_bet(entry: float = 100.0, risk: float = 15.0) -> "OpenBet":
    return open_bet("BET-A6", "BUY", T0, T0, entry, GEOM, risk,
                    spread_cost_per_oz=0.20)


class TestOpenBet:
    def test_sizing_risk_over_sl(self):
        bet = long_bet()
        assert bet.qty_oz == pytest.approx(15.0 / 12.0)  # $15 risk / $12 SL

    def test_levels_are_fixed_from_entry(self):
        bet = long_bet(entry=4000.0, risk=15.0)
        assert bet.tp_price == pytest.approx(4006.0)
        assert bet.sl_price == pytest.approx(3988.0)

    def test_short_levels_mirror(self):
        bet = open_bet("BET-A6", "SELL", T0, T0, 4000.0, GEOM, 15.0)
        assert bet.tp_price == pytest.approx(3994.0)
        assert bet.sl_price == pytest.approx(4012.0)

    def test_bad_direction_raises(self):
        with pytest.raises(ValueError):
            open_bet("X", "HOLD", T0, T0, 100.0, GEOM, 15.0)

    def test_breakeven_wr(self):
        assert BetGeometry(6, 12).breakeven_wr == pytest.approx(12 / 18)
        assert BetGeometry(8, 8).breakeven_wr == pytest.approx(0.5)


class TestBarResolution:
    def test_tp_happy_path(self):
        bet = long_bet()
        res = resolve_bet_on_bars(
            bet, bars([(100.4, 99.9), (100.8, 100.2), (106.5, 100.3)]),
        )
        assert res.outcome == OUTCOME_TP
        assert res.exit_price == pytest.approx(106.0)
        assert res.bars_to_finish == 3
        assert not res.ambiguous

    def test_sl_happy_path(self):
        bet = long_bet()
        res = resolve_bet_on_bars(
            bet, bars([(99.9, 98.0), (99.5, 87.9)]),
        )
        assert res.outcome == OUTCOME_SL
        assert res.exit_price == pytest.approx(88.0)
        assert res.pnl == pytest.approx(15.0 / 12.0 * (88.0 - 100.0)
                                        - 15.0 / 12.0 * 0.20)

    def test_ambiguous_bar_pessimistic_books_sl(self):
        bet = long_bet()
        path = bars([(100.4, 99.9), (107.0, 88.0)])  # one bar hits BOTH levels
        pes = resolve_bet_on_bars(bet, path, POLICY_SL_FIRST)
        opt = resolve_bet_on_bars(bet, path, POLICY_TP_FIRST)
        assert pes.ambiguous and pes.outcome == OUTCOME_SL
        assert opt.ambiguous and opt.outcome == OUTCOME_TP
        assert any(e["event"] == "ambiguous_both" for e in pes.events)

    def test_mfe_mae_tracked_in_price_distance(self):
        bet = long_bet()
        res = resolve_bet_on_bars(
            bet, bars([(100.4, 99.9), (103.0, 98.5), (106.2, 100.0)]),
        )
        assert res.outcome == OUTCOME_TP
        assert res.mfe_price == pytest.approx(6.2)
        assert res.mae_price == pytest.approx(1.5)

    def test_no_touch_leaves_open(self):
        bet = long_bet()
        res = resolve_bet_on_bars(bet, bars([(100.2, 99.9)] * 10))
        assert res.outcome is None
        assert res.pnl is None

    def test_short_resolution(self):
        bet = open_bet("BET-A6", "SELL", T0, T0, 100.0, GEOM, 15.0)
        res = resolve_bet_on_bars(bet, bars([(99.8, 99.5), (99.4, 94.0)]))
        assert res.outcome == OUTCOME_TP
        assert res.exit_price == pytest.approx(94.0)


class TestTickResolution:
    def _tick(self, seconds: float, bid: float, ask: float) -> dict:
        base = datetime(2026, 1, 5, 0, 0).timestamp()
        return {
            "time": base + seconds,
            "time_msc": int((base + seconds) * 1000),
            "bid": bid, "ask": ask,
        }

    def test_ordered_ticks_tp_before_sl(self):
        bet = long_bet()
        ticks = [
            self._tick(10, 100.1, 100.2),
            self._tick(60, 106.3, 106.4),   # mid 106.35 ≥ TP → tp first
            self._tick(120, 87.0, 87.1),    # later SL — never reached
        ]
        res = resolve_bet_on_ticks(bet, ticks)
        assert res.outcome == OUTCOME_TP
        assert res.resolution_method == "ticks"
        assert not res.ambiguous

    def test_ordered_ticks_sl_first_when_sl_comes_first(self):
        bet = long_bet()
        ticks = [
            self._tick(10, 99.9, 100.0),
            self._tick(30, 87.9, 88.0),    # mid 87.95 ≤ SL → sl
            self._tick(90, 106.0, 106.1),
        ]
        res = resolve_bet_on_ticks(bet, ticks)
        assert res.outcome == OUTCOME_SL

    def test_msc_dedup_skips_already_processed(self):
        bet = long_bet()
        resolver = BetResolver(bet)
        resolver.step_tick(T0 + timedelta(seconds=60), 105.0,
                           tick_msc=int(T0.timestamp() * 1000) + 60_000)
        state = resolver.step_tick(T0 + timedelta(seconds=60), 105.0,
                                   tick_msc=int(T0.timestamp() * 1000) + 60_000)
        assert state.outcome is None  # same tick twice → not a resolution
        assert resolver.state.bars_to_finish == 0

    def test_resume_state_restores_excursions(self):
        bet = long_bet()
        resolver = BetResolver(bet, mfe_price=2.5, mae_price=1.0,
                               bars_to_finish=4)
        resolver.step_bar(T0 + timedelta(minutes=25), 104.0, 99.0)
        assert resolver.state.mfe_price == pytest.approx(4.0)   # kept growing
        assert resolver.state.mae_price == pytest.approx(1.0)   # kept the max
        assert resolver.state.bars_to_finish == 5


class TestPnlSpread:
    def test_win_books_round_trip_spread(self):
        bet = long_bet()  # qty 1.25, spread 1.25 × 0.20 = 0.25
        res = resolve_bet_on_bars(bet, bars([(106.5, 100.1)]))
        assert res.pnl == pytest.approx(1.25 * 6.0 - 1.25 * 0.20)

    def test_loss_books_round_trip_spread(self):
        bet = long_bet()
        res = resolve_bet_on_bars(bet, bars([(99.9, 87.9)]))
        assert res.pnl == pytest.approx(-1.25 * 12.0 - 1.25 * 0.20)
