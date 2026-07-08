"""Causal proof tests for reconcile picking wrong deal (2026-07-08, ISSUE-080).

Hypothesis
-----------
`_match_closing_deal` (metty/core/db.py) and `_reconcile_external_close`
(metty/execution/live_trader.py) pick the wrong closing deal when multiple
trades cluster in a price neighborhood. Strategy "price proximity + direction"
does NOT filter by deal time, so it considers closing deals that happened
BEFORE the trade opened (belonging to a different trade).

Concrete failure on production data (Real-A, 2026-07-07/08):
  Trade #5543 BUY @ 4124.679 (ticket 2718857306, opened ~06:55)
  Real SL close: deal 1451023649 @ 4112.76 (loss -11.92, [sl 4112.76000])
  Wrong pick:    deal 1450457579 @ 4132.34 (loss -10.16, [sl 4132.34000])
                 — this is the SL close of trade #5541 (opened 18:00 previous
                   day, ticket 2718357762) which happened BEFORE #5543 opened

Result: DB recorded #5543 with exit_price=4132.34, pnl=+$7.66 (PROFIT!),
exit_reason=stop_loss. But the real broker close was at 4112.76, pnl=-$11.92
(LOSS). One trade's loss was recorded as another trade's profit.

Root cause
----------
Strategy 1 (price proximity) does not filter deals by time. Without a time
guard, closing deals from earlier trades (with similar entry prices) can be
picked as the "closest" deal.

Fix
---
Find the OPEN deal (deal.order == trade.ticket AND deal.type == opening_type)
to get its time. Then Strategy 1 only considers closing deals with
deal.time > open_deal.time. This excludes deals that happened before the
trade opened.

References
----------
- Bug found 2026-07-08 during Real-A performance audit
- Affected code:
  - metty/core/db.py:_match_closing_deal (used by m5_scalp via reconcile_closed_positions)
  - metty/execution/live_trader.py:_reconcile_external_close (swing trader)
- Production evidence: 3 of 9 trades on Real-A had wrong exit_price/pnl/reason
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# ---------- test fixtures ----------


def _make_deal(
    ticket: int,
    order: int,
    deal_type: int,
    price: float,
    time: int,
    profit: float = 0.0,
    comment: str = "god-port-A",
    reason: int = 3,
) -> dict:
    """Build a synthetic MT5 deal dict matching the bridge's output shape."""
    return {
        "ticket": ticket,
        "order": order,
        "time": time,
        "time_msc": time * 1000,
        "type": deal_type,
        "magic": 234000,
        "reason": reason,
        "volume": 0.01,
        "price": price,
        "commission": 0.0,
        "swap": 0.0,
        "profit": profit,
        "symbol": "XAUUSDm",
        "comment": comment,
        "external_id": "",
    }


# Reproduces the Real-A 2026-07-07/08 scenario:
#   Trade #5541 BUY @ 4142.495 (ticket 2718357762)
#     open  deal 1450435489 (order=2718357762, type=0, time=1783466400, 18:00 ICT)
#     close deal 1450457579 (order=0,          type=1, time=1783469200, 18:52 ICT,
#                            price=4132.34, profit=-10.16, comment="[sl 4132.34000]")
#
#   Trade #5543 BUY @ 4124.679 (ticket 2718857306) — opened AFTER #5541 closed
#     open  deal 1450937509 (order=2718857306, type=0, time=1783505700, 06:55 ICT next day)
#     close deal 1451023649 (order=0,          type=1, time=1783511959, 08:19 ICT,
#                            price=4112.76, profit=-11.92, comment="[sl 4112.76000]")
#
# Old bug: _match_closing_deal for #5543 picks deal 1450457579 (SL of #5541)
# because |4132.34 - 4124.679| = 7.661 < |4112.76 - 4124.679| = 11.919.
# Result: DB records #5543 as exit_price=4132.34, pnl=+$7.66 (profit!) but
# the real broker close was at 4112.76 with pnl=-$11.92 (loss).


def _real_scenario_deals() -> list[dict]:
    return [
        # Trade #5541 (BUY, opened 18:00, closed 18:52)
        _make_deal(1450435489, 2718357762, 0, 4142.495, 1783466400,
                  profit=0.0, comment="god-port-A"),
        _make_deal(1450457579, 0, 1, 4132.34, 1783469200,
                  profit=-10.16, comment="[sl 4132.34000]", reason=4),
        # Trade #5543 (BUY, opened 06:55 next day, closed 08:19)
        _make_deal(1450937509, 2718857306, 0, 4124.679, 1783505700,
                  profit=0.0, comment="god-port-A"),
        _make_deal(1451023649, 0, 1, 4112.76, 1783511959,
                  profit=-11.92, comment="[sl 4112.76000]", reason=4),
    ]


def _make_trade(trade_id: int, ticket: int, direction: str, entry: float) -> dict:
    return {
        "id": trade_id,
        "ticket": ticket,
        "direction": direction,
        "entry_price": entry,
        "lot_size": 0.01,
        "stop_loss": 0.0,
        "take_profit": 0.0,
        "timestamp": None,
    }


# ---------- db.py _match_closing_deal tests ----------


class TestMatchClosingDealTimeFilter:
    """_match_closing_deal must NOT pick a closing deal that happened BEFORE
    the trade opened. Without a time filter, it conflates one trade's close
    with another trade's close (ISSUE-080)."""

    def test_does_not_pick_earlier_trade_close_deal(self):
        """Trade #5543 (opened 06:55) must match its OWN SL close at 4112.76,
        NOT trade #5541's SL close at 4132.34 (which happened at 18:52 the
        previous day, BEFORE #5543 opened)."""
        from metty.core.db import _match_closing_deal

        trade_5543 = _make_trade(5543, 2718857306, "BUY", 4124.679)
        deals = _real_scenario_deals()

        result = _match_closing_deal(trade_5543, deals)

        # Must match a deal
        assert result is not None, "Expected a closing deal match for #5543"

        # CRITICAL: must NOT be the SL close of trade #5541 (deal 1450457579)
        result_ticket = result.get("ticket")
        result_price = float(result.get("price", 0))
        assert result_ticket != 1450457579, (
            f"BUG (ISSUE-080): _match_closing_deal picked deal 1450457579 "
            f"(SL close of trade #5541 at 4132.34, time=1783469200) for trade "
            f"#5543 (opened time=1783505700). This deal happened BEFORE #5543 "
            f"opened — it cannot be #5543's close. With a time filter, this "
            f"deal would be excluded. Got price={result_price}."
        )

        # Must be the actual SL close of #5543 (deal 1451023649 @ 4112.76)
        assert result_ticket == 1451023649, (
            f"Expected deal 1451023649 (actual SL close of #5543 at 4112.76), "
            f"got ticket={result_ticket} price={result_price}."
        )
        assert result_price == pytest.approx(4112.76, abs=0.01), (
            f"Expected exit price 4112.76 (actual SL), got {result_price}"
        )

    def test_does_not_pick_earlier_trade_close_for_first_trade(self):
        """Trade #5541 (opened 18:00) must match its OWN SL close at 4132.34,
        NOT trade #5538's close at 4134.426 (which happened at 08:35, BEFORE
        #5541 opened at 18:00)."""
        from metty.core.db import _match_closing_deal

        # Add #5538's close deal (BEFORE #5541 opened) to the deal pool
        deals = _real_scenario_deals() + [
            # Trade #5538 (BUY, opened 07:55, closed 08:35) — earlier trade
            _make_deal(1449716482, 2717630215, 0, 4126.125, 1783410943,
                      profit=0.0, comment="god-port-A"),
            _make_deal(1449762869, 2717676301, 1, 4134.426, 1783413350,
                      profit=8.30, comment="close-2717630215"),
        ]

        trade_5541 = _make_trade(5541, 2718357762, "BUY", 4142.495)

        result = _match_closing_deal(trade_5541, deals)

        assert result is not None, "Expected a closing deal match for #5541"

        result_ticket = result.get("ticket")
        result_price = float(result.get("price", 0))
        # Must NOT be #5538's close (deal 1449762869 @ 4134.426) — it happened
        # at time 1783413350 which is BEFORE #5541 opened at 1783466400.
        assert result_ticket != 1449762869, (
            f"BUG (ISSUE-080): _match_closing_deal picked deal 1449762869 "
            f"(close of trade #5538 at 4134.426, time=1783413350) for trade "
            f"#5541 (opened time=1783466400). This deal happened BEFORE #5541 "
            f"opened. With a time filter, this deal would be excluded."
        )
        # Must be #5541's actual SL close (deal 1450457579 @ 4132.34)
        assert result_ticket == 1450457579, (
            f"Expected deal 1450457579 (actual SL close of #5541 at 4132.34), "
            f"got ticket={result_ticket} price={result_price}."
        )


# ---------- live_trader.py _reconcile_external_close tests ----------


class TestSwingReconcileTimeFilter:
    """Swing trader's _reconcile_external_close must NOT pick a closing deal
    that happened BEFORE the trade opened (same ISSUE-080 bug)."""

    def test_does_not_pick_earlier_trade_close_deal(self, tmp_path, monkeypatch):
        """Same scenario as db.py test — swing trader's reconcile must
        respect deal time and not pick #5541's SL close for #5543."""
        import os
        from metty.execution.live_trader import LiveTrader

        os.environ.setdefault("MT5_BRIDGE_A_HOST", "localhost")
        os.environ.setdefault("MT5_BRIDGE_A_PORT", "8001")
        os.environ.setdefault("MT5_LOGIN_A", "1")
        os.environ.setdefault("MT5_PASSWORD_A", "x")
        os.environ.setdefault("MT5_SERVER_A", "Exness-MT5Real15")

        t = LiveTrader(account="A", dry_run=True)

        deals = _real_scenario_deals()
        # Monkeypatch _get_deal_history to return our scenario
        monkeypatch.setattr(t, "_get_deal_history", lambda days_back=7: deals)

        # Trade #5543 BUY @ 4124.679 (ticket 2718857306)
        result = t._reconcile_external_close(
            ticket=2718857306,
            direction="BUY",
            entry_price=4124.679,
            sl=0.0,
            tp=0.0,
        )

        assert result is not None, (
            "Expected reconcile to find a closing deal for #5543"
        )
        exit_price = result.get("exit_price")
        exit_reason = result.get("exit_reason")

        # Must NOT be #5541's SL close at 4132.34
        assert exit_price != pytest.approx(4132.34, abs=0.01), (
            f"BUG (ISSUE-080): swing reconcile picked deal 1450457579 "
            f"(SL close of trade #5541 at 4132.34, time=1783469200) for "
            f"trade #5543 (opened time=1783505700). This deal happened BEFORE "
            f"#5543 opened — it cannot be #5543's close. With a time filter, "
            f"this deal would be excluded."
        )

        # Must be #5543's actual SL close at 4112.76
        assert exit_price == pytest.approx(4112.76, abs=0.01), (
            f"Expected exit price 4112.76 (actual SL of #5543), got {exit_price}"
        )
        assert exit_reason == "stop_loss", (
            f"Expected exit_reason='stop_loss', got {exit_reason!r}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])