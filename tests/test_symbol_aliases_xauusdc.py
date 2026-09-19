"""Regression: XAUUSDc must be in SYMBOL_ALIASES (and last) for cent accounts.

Bug symptom ("bxau-zombie", 2026-09-19): P1-P3 engines on Exness Standard Cent
(Exness-MT5Real25) got zero candle data forever — the cent server only serves
XAUUSDc for gold, which was missing from SYMBOL_ALIASES, so _resolve_symbol
fell back to XAUUSD (not on the cent server) and every candle fetch returned
empty.

These tests pin the fix AND guard the ordering constraint: XAUUSDc must stay
at the END so Real-A (XAUUSDm) and demo B/C/D (XAUUSD) resolve unchanged.
"""

from metty.bridge.client import SYMBOL_ALIASES


class TestXAUUSDcAlias:
    def test_xauusdc_in_alias_list(self):
        """The causal fix: cent accounts can now resolve gold."""
        assert "XAUUSDc" in SYMBOL_ALIASES["XAUUSD"]

    def test_xauusdc_is_last(self):
        """Cent variant must be tried last — Real-A/demo resolution first."""
        aliases = SYMBOL_ALIASES["XAUUSD"]
        assert aliases[-1] == "XAUUSDc"

    def test_existing_priority_unchanged(self):
        """Real-A (XAUUSDm) and demo (XAUUSD) must resolve exactly as before."""
        assert SYMBOL_ALIASES["XAUUSD"][:4] == ["XAUUSDm", "XAUUSD", "XAUUSD.i", "XAUUSDb"]