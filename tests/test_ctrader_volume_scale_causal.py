"""Causal tests for cTrader volume conversion — lotSize is already in cents.

Evidence (19/11 deep probe, demo host): SymbolByIdRes for XAUUSD returns
lotSize=10000, and the canonical proto in this repo says it outright —
proto_reference/OpenApiModelMessages.proto:

    optional int64 lotSize = 30;  // Lot size of the Symbol (in cents).

(minVolume/maxVolume/stepVolume are "(in cents)" too.) The correct formula
is therefore volume_cents = lots x lotSize, nothing more. The old code
multiplied by VOLUME_CENTS_PER_UNIT (100) on top, so 1 lot on the real
wire (lotSize=10000) became 1,000,000 cents = 100 lots — a 100x oversize,
the same blow-up class as the USC face-value trap. The old DEFAULT_LOT_SIZE
of 100 was right only by coincidence (100x100=10000) and masked the bug in
every mock test. No network in this file.
"""

import pytest

from ctrader.protocol import (
    DEFAULT_LOT_SIZE,
    lots_to_volume_cents,
    volume_cents_to_lots,
)

XAUUSD_WIRE_LOT_SIZE = 10_000  # SymbolByIdRes 19/11 — the real server value


# ----------------------------------------------------------------------
# Wire-truth conversions (lotSize=10000 exactly as the server sends it)
# ----------------------------------------------------------------------

def test_wire_lot_size_conversions():
    assert lots_to_volume_cents(0.01, lot_size=XAUUSD_WIRE_LOT_SIZE) == 100
    assert lots_to_volume_cents(1.0, lot_size=XAUUSD_WIRE_LOT_SIZE) == 10_000
    assert lots_to_volume_cents(100.0, lot_size=XAUUSD_WIRE_LOT_SIZE) == 1_000_000


def test_wire_lot_size_round_trip():
    """1,000,000 cents must convert back to 100 lots — not 1 lot, not 10,000."""
    assert volume_cents_to_lots(1_000_000, lot_size=XAUUSD_WIRE_LOT_SIZE) == pytest.approx(100.0)
    assert volume_cents_to_lots(10_000, lot_size=XAUUSD_WIRE_LOT_SIZE) == pytest.approx(1.0)
    assert volume_cents_to_lots(100, lot_size=XAUUSD_WIRE_LOT_SIZE) == pytest.approx(0.01)


def test_no_extra_x100_hides_in_the_formula():
    """Any lot size obeys volume_cents = lots x lotSize — no hidden constant.

    A 1000-cents-per-lot symbol: 1 lot -> 1000 cents (the old code would
    have returned 100,000 here).
    """
    assert lots_to_volume_cents(1.0, lot_size=1000) == 1_000
    assert volume_cents_to_lots(1_000, lot_size=1000) == pytest.approx(1.0)


def test_default_lot_size_matches_wire_truth():
    """The fallback must be the wire-true XAUUSD value, not the coincidence.

    Old DEFAULT_LOT_SIZE=100 gave correct answers only by accident
    (100 x 100 = 10000) — pin the real value so a silent revert cannot
    re-hide the scale bug in pre-connect code paths.
    """
    assert DEFAULT_LOT_SIZE == 10_000
    assert lots_to_volume_cents(1.0) == 10_000
    assert lots_to_volume_cents(0.01) == 100
    assert volume_cents_to_lots(10_000) == pytest.approx(1.0)


def test_round_trip_lands_back_on_the_original_lots():
    for lots in (0.01, 0.1, 0.5, 1.0, 2.37, 100.0):
        cents = lots_to_volume_cents(lots, lot_size=XAUUSD_WIRE_LOT_SIZE)
        assert volume_cents_to_lots(cents, lot_size=XAUUSD_WIRE_LOT_SIZE) == pytest.approx(lots)


def test_zero_volume_stays_zero():
    assert volume_cents_to_lots(0, lot_size=XAUUSD_WIRE_LOT_SIZE) == 0.0
