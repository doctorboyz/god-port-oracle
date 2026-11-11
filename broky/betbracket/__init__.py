"""Theory-free bracket bet machinery (deep tick + quarterly grid).

Two ledgers (BUY + SELL) at every indicator event, tick-resolved.
Additive module — does not touch broky/bet (the live farm).
"""
from broky.betbracket.bracket import (
    BarBracketResult,
    BarSideResult,
    BracketResult,
    PAIR_BOTH_LOSE,
    PAIR_BOTH_WIN,
    PAIR_BUY_WIN,
    PAIR_OPEN,
    PAIR_SELL_WIN,
    SideResult,
    max_streak,
    quarter_of_msc,
    resolve_bracket,
    resolve_bracket_on_bars,
)
from broky.betbracket.events import (
    EVENT_TYPES,
    all_event_indices,
    event_fire_indices,
    event_polarity_series,
)

__all__ = [
    "BarBracketResult",
    "BarSideResult",
    "BracketResult",
    "SideResult",
    "PAIR_BUY_WIN",
    "PAIR_SELL_WIN",
    "PAIR_BOTH_WIN",
    "PAIR_BOTH_LOSE",
    "PAIR_OPEN",
    "EVENT_TYPES",
    "all_event_indices",
    "event_fire_indices",
    "event_polarity_series",
    "max_streak",
    "quarter_of_msc",
    "resolve_bracket",
    "resolve_bracket_on_bars",
]
