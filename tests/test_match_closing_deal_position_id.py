"""Causal proof tests for _match_closing_deal position_id match bug (2026-07-02).

Hypothesis
----------
On demo B/C/D, manual reconcile closed ghost trades #96/#97/#98 with WRONG PnL
(-7.36/-7.38/-7.15) when the real MT5 closing deals had profit +0.21/+0.59/+2.12.

Root cause: `_match_closing_deal` Strategy 0 matches by `deal.order == ticket`.
In MT5 deal history:
  - ENTRY deal has order == position_ticket (e.g. order=3407314957 pos=3407314957)
  - CLOSING deal has order == closing_order_ticket (NOT position_ticket),
    but position_id == original position_ticket
    (e.g. order=3407339330 pos=3407314957)

So Strategy 0 never matches closing deals — it only matches entry deals
(which then fail the closing_type check and are skipped). The function falls
through to Strategy 1 (price proximity), which picks the WRONG deal when
multiple positions close at the same price in a batch (e.g. trailing-TP batch
close at 08:46:09 with 7 deals all at price=4073.976).

Fix: Strategy 0 must match by `deal.position_id == ticket` (with entry=1 to
ensure it's a closing deal), not by `deal.order == ticket`.

References
----------
- Bug found during manual reconcile of demo B/C/D ghosts on 2026-07-02
- Function: metty/core/db.py:_match_closing_deal
- MT5 deal fields: position_id links closing deal -> original position ticket
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from metty.core.db import _match_closing_deal


def _trade(trade_id: int, ticket: int, direction: str, entry_price: float) -> dict:
    return {
        "id": trade_id,
        "ticket": ticket,
        "direction": direction,
        "entry_price": entry_price,
        "stop_loss": 0.0,
        "take_profit": 0.0,
        "timestamp": "2026-07-02T08:41:00+00:00",
    }


def _deal(deal_id: int, order: int, position_id: int, deal_type: int,
          entry: int, price: float, profit: float, comment: str = "") -> dict:
    return {
        "ticket": deal_id,
        "order": order,
        "position_id": position_id,
        "type": deal_type,        # 0=BUY, 1=SELL
        "entry": entry,           # 0=entry, 1=exit
        "price": price,
        "profit": profit,
        "comment": comment,
        "time": 1782996377,
    }


class TestMatchByPositionId:
    """Strategy 0 must match closing deals via deal.position_id == trade.ticket."""

    def test_closing_deal_matched_by_position_id(self):
        """A closing deal with position_id == trade.ticket must be returned,
        not the entry deal (which has order == ticket but wrong type)."""
        trade = _trade(trade_id=98, ticket=3407314957, direction="SELL",
                       entry_price=4076.005)
        # Entry deal: order=ticket, type=SELL (1), entry=0 — must NOT match
        # (closing_type for SELL is BUY=0, this deal is type=1)
        entry_deal = _deal(3092243251, order=3407314957, position_id=3407314957,
                           deal_type=1, entry=0, price=4076.005, profit=0.0,
                           comment="god-port-C")
        # Correct closing deal: order != ticket, position_id == ticket
        correct_close = _deal(3092264676, order=3407339330,
                              position_id=3407314957, deal_type=0, entry=1,
                              price=4073.891, profit=2.12, comment="god-port-C")
        deals = [entry_deal, correct_close]

        matched = _match_closing_deal(trade, deals)

        assert matched is not None, "must match a deal"
        assert matched["position_id"] == 3407314957, (
            "must match the closing deal whose position_id == trade.ticket"
        )
        assert matched["profit"] == 2.12, (
            f"must use the correct closing deal profit=+2.12, "
            f"got profit={matched.get('profit')}"
        )

    def test_batch_close_picks_correct_position_not_first_at_price(self):
        """Reproduces the actual production bug: 5 positions closed at the
        SAME price 4073.891 in a batch at 08:46:17. Strategy 1 (price
        proximity) picks the first one (profit=-7.15, wrong). Strategy 0
        via position_id must pick the one belonging to this trade's ticket.

        Without the fix, _match_closing_deal returns the wrong deal and
        reconcile records pnl=-7.15 instead of +2.12 for trade #98.
        """
        trade = _trade(trade_id=98, ticket=3407314957, direction="SELL",
                       entry_price=4076.005)
        # 5 closing deals, all BUY (type=0) at price=4073.891, all entry=1
        # Only the last has position_id == trade.ticket
        deals = [
            _deal(3092264672, order=3407339326, position_id=3406414211,
                  deal_type=0, entry=1, price=4073.891, profit=-7.15),
            _deal(3092264673, order=3407339327, position_id=3406461144,
                  deal_type=0, entry=1, price=4073.891, profit=-1.31),
            _deal(3092264674, order=3407339328, position_id=3406518553,
                  deal_type=0, entry=1, price=4073.891, profit=-4.84),
            _deal(3092264675, order=3407339329, position_id=3406544302,
                  deal_type=0, entry=1, price=4073.891, profit=-0.94),
            _deal(3092264676, order=3407339330, position_id=3407314957,
                  deal_type=0, entry=1, price=4073.891, profit=2.12),
        ]

        matched = _match_closing_deal(trade, deals)

        assert matched is not None, "must match a deal"
        assert matched["position_id"] == 3407314957, (
            "must match by position_id, not by price proximity (which picks "
            "the first deal at the shared price — the production bug)"
        )
        assert matched["profit"] == 2.12, (
            f"trade #98 real profit is +2.12 (deal 3092264676). "
            f"Without position_id match, reconcile recorded -7.15. "
            f"Got profit={matched.get('profit')}"
        )

    def test_buy_position_matched_by_position_id(self):
        """Same fix must work for BUY positions (closing deal is SELL type=1)."""
        trade = _trade(trade_id=100, ticket=2167377000, direction="BUY",
                       entry_price=4050.0)
        entry_deal = _deal(1, order=2167377000, position_id=2167377000,
                           deal_type=0, entry=0, price=4050.0, profit=0.0)
        correct_close = _deal(2, order=9999999, position_id=2167377000,
                              deal_type=1, entry=1, price=4060.0, profit=10.0)
        # Wrong closing deal at same price, different position
        wrong_close = _deal(3, order=8888888, position_id=1234567890,
                            deal_type=1, entry=1, price=4060.0, profit=-5.0)
        deals = [entry_deal, wrong_close, correct_close]

        matched = _match_closing_deal(trade, deals)

        assert matched is not None
        assert matched["position_id"] == 2167377000, (
            "BUY position must match closing deal by position_id"
        )
        assert matched["profit"] == 10.0


class TestStrategy0DoesNotMatchEntryDeal:
    """Sanity: even with position_id match, must not return the entry deal
    (entry=0) — only closing deals (entry=1)."""

    def test_entry_deal_not_returned_even_if_position_id_matches(self):
        """If only the entry deal exists (position still open in MT5 but
        deal history fetched), we must NOT match the entry deal as a
        'closing' deal — that would record pnl=0 and wrong exit_price."""
        trade = _trade(trade_id=50, ticket=777, direction="SELL",
                       entry_price=4100.0)
        # Only the entry deal exists; no closing deal yet
        entry_deal = _deal(10, order=777, position_id=777, deal_type=1,
                           entry=0, price=4100.0, profit=0.0)
        deals = [entry_deal]

        matched = _match_closing_deal(trade, deals)

        # Either return None (no closing deal) — both are acceptable.
        # What's NOT acceptable: returning the entry deal as a closing deal.
        if matched is not None:
            assert matched.get("entry") == 1, (
                "must not return entry deal (entry=0) as a closing deal"
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])