"""Causal proof test: brokerless farm fills must cross the spread (ISSUE-098).

Hypothesis
----------
Farm pnl is optimistic vs live because BOTH fill legs happen at mid price:
- entry: insert_live_trade(entry_price=price) — mid close, no ask/bid
- exit:  exit_price = SL / TP / trailing level / current mid — no bid/ask

Live XAUUSD crosses the spread on every round trip (BUY fills at ask,
closes at bid; SELL mirrors) — ~1 spread per trade ($0.20 at the farm's
MOCK_SPREAD_POINTS=20). The 2026-10-03 league table therefore flatters
every variant by that friction on every closed trade, so Marks judges
farm data that live trading cannot reproduce.

Fix: dry_run fills cross half a spread per leg (entry leg ± s/2 from
mid, exit leg ∓ s/2) — exactly one spread per round trip, priced from
the same spread source the gate already uses. Trades recorded with
spread_at_entry NULL/0 (legacy rows) and live mode (dry_run=False) are
untouched.

Causal proof
------------
1. Entry: forced signal + MOCK_SPREAD_POINTS=20 → opened trade's
   entry_price must be mid ± $0.10 (half of 20 points). RED now: mid.
2. Exits: trade with spread_at_entry=20 hitting SL/TP → exit fill must
   be level ∓ $0.10 and pnl must include the cost. RED now: exact level.
3. TP1 partial close pays the same half spread.
4. Controls: no recorded spread → mid fill preserved (legacy rows);
   dry_run=False → no simulation ever (live/VPS unchanged).

This test FAILS (RED) before the fix, PASSES (GREEN) after.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from metty.core.db import (  # noqa: E402
    get_open_trades,
    init_db,
    insert_account,
    insert_live_trade,
)
from metty.execution.live_trader import CONTRACT_SIZE, LiveTrader, RiskConfig  # noqa: E402
from shared.models import Signal, SignalType  # noqa: E402

_M5_CSV = "XAUUSD_M5.csv"
# MOCK_SPREAD_POINTS=20 → $0.20 spread (XAUUSD point = 0.01) → $0.10 per leg.
HALF_SPREAD = 0.10
LOT = 0.05
# Cost per leg in pnl terms: half_spread x lot x CONTRACT_SIZE.
LEG_COST = HALF_SPREAD * LOT * CONTRACT_SIZE


@pytest.fixture()
def farm_db(tmp_path):
    db = tmp_path / "spread_cost_test.db"
    init_db(db)
    insert_account(
        name="P11", balance=100.0, leverage=2000,
        bridge_host="none", bridge_port=8001, signal_group="portfolio",
        db_path=db,
    )
    return db


def _write_m5(data_dir: Path, last_close: float = 4059.5) -> pd.DataFrame:
    """Fresh M5 CSV; returns the frame for reuse by _monitor_positions."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    n = 120
    idx = pd.date_range(now - timedelta(minutes=5 * (n - 1)), periods=n, freq="5min")
    closes = pd.Series([4000.0 + i * 0.5 for i in range(n)], index=idx)
    closes.iloc[-1] = last_close
    df = pd.DataFrame({
        "open": closes, "high": closes + 0.05, "low": closes - 0.05,
        "close": closes, "volume": [100.0] * n,
    })
    df.index.name = "date"
    df.to_csv(data_dir / _M5_CSV, header=False, float_format="%.3f")
    return df


def _make_trader(monkeypatch, tmp_path, db, mock_points: str | None = "20",
                 dry_run: bool = True, partial_tp: str | None = None) -> LiveTrader:
    monkeypatch.setenv("ACCOUNTS", "P11")
    monkeypatch.setenv("MT5_BRIDGE_MAX_RETRIES", "1")
    monkeypatch.setenv("MT5_BRIDGE_RETRY_DELAY", "0")
    monkeypatch.setenv("INITIAL_EQUITY_P11", "100")
    monkeypatch.setenv("ML_FILTER_ENABLED", "0")
    if mock_points is not None:
        monkeypatch.setenv("MOCK_SPREAD_POINTS", mock_points)
    if partial_tp is not None:
        monkeypatch.setenv("PARTIAL_TP_ENABLED_P11", partial_tp)
    t = LiveTrader(
        account="P11", db_path=db, data_dir=tmp_path, dry_run=dry_run,
        risk_config=RiskConfig(risk_per_trade=0.01),
    )
    from metty.bridge.client import MT5Bridge
    monkeypatch.setattr(MT5Bridge, "fetch_account_info_sync", lambda self: None)
    return t


def _force_signal(monkeypatch, t: LiveTrader, direction: str = "SELL") -> None:
    sig = Signal(
        signal_type=SignalType.BUY if direction == "BUY" else SignalType.SELL,
        confidence=0.65, price=4000.0,
        timestamp=datetime.now(timezone.utc),
        indicators={"trend_alignment": 1}, regime="ranging",
    )
    monkeypatch.setattr(t, "_generate_signal", lambda candles: sig)
    monkeypatch.setattr(t, "_get_calendar", lambda: [])


def _insert_open_trade(db, t: LiveTrader, direction: str, entry: float,
                       sl: float, tp: float, spread_at_entry: float | None,
                       tp1_price: float | None = None) -> int:
    return insert_live_trade(
        account_id=t.account_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        direction=direction,
        entry_price=entry,
        stop_loss=sl,
        take_profit=tp,
        lot_size=LOT,
        confidence=0.65,
        regime="ranging",
        session="asian",
        strategy_id="swing-P11",
        spread_at_entry=spread_at_entry,
        tp1_price=tp1_price,
        tp_level=1,
        db_path=db,
    )


class TestEntryFillCrossesSpread:
    """Opened dry-run trades must fill at ask (BUY) / bid (SELL), not mid."""

    def test_sell_entry_fills_at_bid(self, monkeypatch, tmp_path, farm_db):
        _write_m5(tmp_path)
        t = _make_trader(monkeypatch, tmp_path, farm_db, "20")
        _force_signal(monkeypatch, t, "SELL")

        result = t.run_once()
        trades = get_open_trades(t.account_id, farm_db)
        assert trades, f"farm must open the trade for the fill to be priced, got {result}"
        assert trades[0]["entry_price"] == pytest.approx(4000.0 - HALF_SPREAD), (
            f"SELL entry must fill at bid (mid - {HALF_SPREAD}), "
            f"got {trades[0]['entry_price']} (mid fill = the ISSUE-098 bug)"
        )
        assert trades[0]["spread_at_entry"] == 20.0

    def test_buy_entry_fills_at_ask(self, monkeypatch, tmp_path, farm_db):
        _write_m5(tmp_path)
        t = _make_trader(monkeypatch, tmp_path, farm_db, "20")
        _force_signal(monkeypatch, t, "BUY")

        result = t.run_once()
        trades = get_open_trades(t.account_id, farm_db)
        assert trades, f"farm must open the trade for the fill to be priced, got {result}"
        assert trades[0]["entry_price"] == pytest.approx(4000.0 + HALF_SPREAD), (
            f"BUY entry must fill at ask (mid + {HALF_SPREAD}), "
            f"got {trades[0]['entry_price']} (mid fill = the ISSUE-098 bug)"
        )


class TestExitFillCrossesSpread:
    """Closes must pay the other half spread — BUY sells at bid, SELL buys at ask."""

    def _close_via_monitor(self, t: LiveTrader, m5: pd.DataFrame) -> list[dict]:
        return t._monitor_positions({"M5": m5})

    def test_buy_sl_exit_fills_below_level(self, monkeypatch, tmp_path, farm_db):
        # BUY 4000, SL 3990; last close 3985 → SL hit.
        # Live closes at bid = 3990 - 0.10 → pnl one half-spread worse than mid.
        m5 = _write_m5(tmp_path, last_close=3985.0)
        t = _make_trader(monkeypatch, tmp_path, farm_db, "20")
        _insert_open_trade(farm_db, t, "BUY", 4000.0, 3990.0, 4020.0, 20.0)

        closed = self._close_via_monitor(t, m5)
        assert closed and closed[0]["exit_reason"] == "stop_loss"
        assert closed[0]["exit_price"] == pytest.approx(3990.0 - HALF_SPREAD), (
            f"BUY SL close must fill at bid (SL - {HALF_SPREAD}), "
            f"got {closed[0]['exit_price']} (level fill = the ISSUE-098 bug)"
        )
        mid_pnl = (3990.0 - 4000.0) * LOT * CONTRACT_SIZE
        assert closed[0]["pnl"] == pytest.approx(mid_pnl - LEG_COST), (
            f"pnl must absorb the exit-leg spread cost, got {closed[0]['pnl']}"
        )

    def test_sell_sl_exit_fills_above_level(self, monkeypatch, tmp_path, farm_db):
        # SELL 4000, SL 4007; last close 4010 → SL hit; close buys at ask.
        m5 = _write_m5(tmp_path, last_close=4010.0)
        t = _make_trader(monkeypatch, tmp_path, farm_db, "20")
        _insert_open_trade(farm_db, t, "SELL", 4000.0, 4007.0, 3982.5, 20.0)

        closed = self._close_via_monitor(t, m5)
        assert closed and closed[0]["exit_reason"] == "stop_loss"
        assert closed[0]["exit_price"] == pytest.approx(4007.0 + HALF_SPREAD), (
            f"SELL SL close must fill at ask (SL + {HALF_SPREAD}), "
            f"got {closed[0]['exit_price']}"
        )

    def test_buy_tp_exit_fills_below_level(self, monkeypatch, tmp_path, farm_db):
        # BUY 4000, TP 4020; last close 4025 → TP hit; close sells at bid.
        m5 = _write_m5(tmp_path, last_close=4025.0)
        t = _make_trader(monkeypatch, tmp_path, farm_db, "20")
        _insert_open_trade(farm_db, t, "BUY", 4000.0, 3990.0, 4020.0, 20.0)

        closed = self._close_via_monitor(t, m5)
        assert closed and closed[0]["exit_reason"] == "take_profit"
        assert closed[0]["exit_price"] == pytest.approx(4020.0 - HALF_SPREAD), (
            f"BUY TP close must fill at bid (TP - {HALF_SPREAD}), "
            f"got {closed[0]['exit_price']}"
        )

    def test_tp1_close_pays_half_spread_and_scale_in_entry_too(
        self, monkeypatch, tmp_path, farm_db
    ):
        # SELL 4000, TP 3982.5, TP1 3991.25; last close 3990 → TP1 hit.
        m5 = _write_m5(tmp_path, last_close=3990.0)
        t = _make_trader(monkeypatch, tmp_path, farm_db, "20", partial_tp="1")
        _insert_open_trade(
            farm_db, t, "SELL", 4000.0, 4007.0, 3982.5, 20.0, tp1_price=3991.25,
        )

        closed = t._monitor_positions({"M5": m5})
        assert closed and closed[0]["exit_reason"] == "tp1_hit", (
            f"expected tp1_hit close, got {closed}"
        )
        assert closed[0]["exit_price"] == pytest.approx(3991.25 + HALF_SPREAD), (
            f"TP1 close must fill at ask (TP1 + {HALF_SPREAD}), "
            f"got {closed[0]['exit_price']}"
        )
        # Scale-in position 2 (SELL) must ALSO enter at bid, not at tp1_price mid.
        remaining = get_open_trades(t.account_id, farm_db)
        assert remaining, "scale-in position 2 must be open after TP1"
        assert remaining[0]["entry_price"] == pytest.approx(3991.25 - HALF_SPREAD), (
            f"scale-in entry must fill at bid, got {remaining[0]['entry_price']}"
        )
        assert remaining[0]["spread_at_entry"] == 20.0, (
            "scale-in must record spread_at_entry so its own exits cross too"
        )


class TestControls:
    """Legacy rows and live mode must be untouched."""

    def test_no_recorded_spread_keeps_mid_fill(self, monkeypatch, tmp_path, farm_db):
        # Control: trade opened before the fix (spread_at_entry NULL) —
        # closing it must NOT invent a cost retroactively.
        m5 = _write_m5(tmp_path, last_close=3985.0)
        t = _make_trader(monkeypatch, tmp_path, farm_db, "20")
        _insert_open_trade(farm_db, t, "BUY", 4000.0, 3990.0, 4020.0, None)

        closed = t._monitor_positions({"M5": m5})
        assert closed and closed[0]["exit_reason"] == "stop_loss"
        assert closed[0]["exit_price"] == pytest.approx(3990.0), (
            "no recorded spread → mid fill must be preserved (legacy rows untouched)"
        )

    def test_live_mode_never_simulates(self, monkeypatch, tmp_path, farm_db):
        # Control: dry_run=False → fills stay real (live/VPS unchanged).
        m5 = _write_m5(tmp_path, last_close=3985.0)
        t = _make_trader(monkeypatch, tmp_path, farm_db, "20", dry_run=False)
        _insert_open_trade(farm_db, t, "BUY", 4000.0, 3990.0, 4020.0, 20.0)

        closed = t._monitor_positions({"M5": m5})
        # Live path with no ticket skips the MT5 close and closes in DB —
        # the recorded exit must be the raw level, no simulated spread.
        assert closed and closed[0]["exit_reason"] == "stop_loss"
        assert closed[0]["exit_price"] == pytest.approx(3990.0), (
            "live mode must never apply simulated spread fills"
        )