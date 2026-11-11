"""The bet contract — TP/SL set once at entry, held to the end, always.

Iron rules (ada-task-20261107-true-bet-farm.md):
- NO trailing, NO time-stop, NO max-holding, NO entry-hour gate, NO circuit
  breaker — nothing decides the exit except the contract levels.
- Resolution prefers TICKS when available (bridge copy_ticks_from,
  read-only); the bar-conservative fallback resolves with closed-bar
  high/low and reports both edges when one bar touches both levels.

Price model (documented in the backtest report):
- Levels anchor at the ENTRY MID price. Both live (tick mid = (bid+ask)/2)
  and backtest (Exness bar prices) judge TP/SL on the same series the entry
  came from, so TP6:SL12 really means six/twelve dollars — no hidden spread
  distortion of the geometry.
- Spread is charged as a flat round-trip cost per ounce
  (``spread_cost_per_oz`` — farm parity: MOCK_SPREAD_POINTS 20 × $0.01/pt
  = $0.20/oz), subtracted from EVERY resolved bet's pnl. Win/loss labels
  stay pure geometry; the report shows both with and without the cost.

Ambiguity:
- A single bar whose high ≥ TP and low ≤ SL cannot order the two touches
  → the bet is AMBIGUOUS. Policy ``sl_first`` (pessimistic) books the loss,
  ``tp_first`` (optimistic) books the win. The backtest reports both edges
  as separate rows. Tick resolution is ordered, so live bets are never
  ambiguous.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

BUY = "BUY"
SELL = "SELL"

OUTCOME_TP = "tp"
OUTCOME_SL = "sl"

# Ambiguity policies for a bar touching both contract levels.
POLICY_SL_FIRST = "sl_first"   # pessimistic — the loss is booked
POLICY_TP_FIRST = "tp_first"   # optimistic — the win is booked
POLICIES = (POLICY_SL_FIRST, POLICY_TP_FIRST)

# Default round-trip spread cost per ounce ($/oz) — paper-farm parity
# (MOCK_SPREAD_POINTS=20 points × $0.01/point).
DEFAULT_SPREAD_COST_PER_OZ = 0.20


@dataclass(frozen=True)
class BetGeometry:
    """Fixed contract distances in dollars, identical for long and short."""
    tp_dist: float
    sl_dist: float

    @property
    def breakeven_wr(self) -> float:
        """Win rate (pure geometry, no spread) at which expectancy = 0."""
        return self.sl_dist / (self.tp_dist + self.sl_dist)


@dataclass
class OpenBet:
    """A bet opened under the contract — mutable only via BetResolver."""
    cell_id: str
    direction: str                     # BUY / SELL
    signal_ts: datetime               # closed bar that produced the signal
    entry_ts: datetime                # bar open / live tick time of the fill
    entry_price: float                # mid price the contract anchors to
    qty_oz: float                      # position size in troy ounces
    tp_price: float
    sl_price: float
    risk_amount: float                # dollars risked at SL (qty × sl_dist)
    spread_cost: float                 # round-trip $ cost booked at resolution


def open_bet(
    cell_id: str,
    direction: str,
    signal_ts: datetime,
    entry_ts: datetime,
    entry_price: float,
    geometry: BetGeometry,
    risk_amount: float,
    spread_cost_per_oz: float = DEFAULT_SPREAD_COST_PER_OZ,
) -> OpenBet:
    """Create a bet: fixed levels from the entry price, size from the risk.

    qty = risk_amount / sl_dist — the dollar risk at SL is exactly
    `risk_amount` (sizing per คุณหมอ: 1.2–2% of the cell's $1,000 seed).
    """
    if direction not in (BUY, SELL):
        raise ValueError(f"direction must be {BUY}/{SELL}, got {direction!r}")
    if geometry.sl_dist <= 0 or geometry.tp_dist <= 0:
        raise ValueError("geometry distances must be positive")
    if risk_amount <= 0:
        raise ValueError("risk_amount must be positive")
    if entry_price <= 0:
        raise ValueError("entry_price must be positive")

    qty = risk_amount / geometry.sl_dist
    sign = 1.0 if direction == BUY else -1.0
    return OpenBet(
        cell_id=cell_id,
        direction=direction,
        signal_ts=signal_ts,
        entry_ts=entry_ts,
        entry_price=entry_price,
        qty_oz=qty,
        tp_price=entry_price + sign * geometry.tp_dist,
        sl_price=entry_price - sign * geometry.sl_dist,
        risk_amount=risk_amount,
        spread_cost=qty * spread_cost_per_oz,
    )


@dataclass
class Resolution:
    """Final state of a resolved bet (outcome is tp/sl; None = still open)."""
    outcome: Optional[str]              # OUTCOME_TP / OUTCOME_SL / None
    exit_ts: Optional[datetime]
    exit_price: Optional[float]
    pnl: Optional[float]                # dollars, spread cost already booked
    mfe_price: float = 0.0              # max favorable excursion, $ price dist
    mae_price: float = 0.0              # max adverse excursion, $ price dist
    bars_to_finish: int = 0             # bars from entry bar to resolution bar
    ambiguous: bool = False             # one bar touched both levels
    events: list[dict[str, Any]] = field(default_factory=list)
    resolution_method: str = "bars"     # "bars" | "ticks"


def _update_excursions(res: Resolution, bet: OpenBet, high: float, low: float) -> None:
    """Running MFE/MAE in price distance from entry (always positive)."""
    if bet.direction == BUY:
        res.mfe_price = max(res.mfe_price, high - bet.entry_price)
        res.mae_price = max(res.mae_price, bet.entry_price - low)
    else:
        res.mfe_price = max(res.mfe_price, bet.entry_price - high)
        res.mae_price = max(res.mae_price, low - bet.entry_price)


def _finalize(res: Resolution, bet: OpenBet, outcome: str, exit_ts: datetime,
              exit_price: float, method: str) -> None:
    res.outcome = outcome
    res.exit_ts = exit_ts
    res.exit_price = exit_price
    sign = 1.0 if bet.direction == BUY else -1.0
    gross = sign * (exit_price - bet.entry_price) * bet.qty_oz
    res.pnl = gross - bet.spread_cost
    res.resolution_method = method


class BetResolver:
    """Step a single open bet toward resolution, one observation at a time.

    Shared by backtest (bar steps) and the live container (tick steps, with
    closed-bar steps as the fallback) — one code path, one contract.
    """

    def __init__(self, bet: OpenBet, policy: str = POLICY_SL_FIRST,
                 mfe_price: float = 0.0, mae_price: float = 0.0,
                 bars_to_finish: int = 0, last_tick_msc: int = 0) -> None:
        """`mfe/mae/bars/last_tick_msc` resume a persisted bet after a
        container restart (the live store saves them every cycle)."""
        if policy not in POLICIES:
            raise ValueError(f"policy must be one of {POLICIES}, got {policy!r}")
        self.bet = bet
        self.policy = policy
        self.state = Resolution(outcome=None, exit_ts=None, exit_price=None,
                                pnl=None, mfe_price=mfe_price,
                                mae_price=mae_price,
                                bars_to_finish=bars_to_finish)
        self.last_tick_msc = last_tick_msc
        self._bars_seen = bars_to_finish
        self._first_step = bars_to_finish == 0

    def step_bar(self, ts: datetime, high: float, low: float) -> Resolution:
        """Advance one closed bar. Returns the (possibly still-open) state."""
        if self.state.outcome is not None:
            return self.state
        self._bars_seen += 1
        self.state.bars_to_finish = self._bars_seen
        _update_excursions(self.state, self.bet, high, low)

        if self.bet.direction == BUY:
            hit_tp = high >= self.bet.tp_price
            hit_sl = low <= self.bet.sl_price
        else:
            hit_tp = low <= self.bet.tp_price
            hit_sl = high >= self.bet.sl_price

        if hit_tp and hit_sl:
            # Ambiguous bar: both levels touched, order unknowable from OHLC.
            outcome = OUTCOME_SL if self.policy == POLICY_SL_FIRST else OUTCOME_TP
            price = self.bet.sl_price if outcome == OUTCOME_SL else self.bet.tp_price
            self.state.ambiguous = True
            self.state.events.append({
                "ts": ts.isoformat(), "event": "ambiguous_both",
                "booked": outcome,
                "policy": self.policy,
            })
            _finalize(self.state, self.bet, outcome, ts, price, "bars")
        elif hit_tp:
            self.state.events.append({"ts": ts.isoformat(), "event": OUTCOME_TP})
            _finalize(self.state, self.bet, OUTCOME_TP, ts, self.bet.tp_price, "bars")
        elif hit_sl:
            self.state.events.append({"ts": ts.isoformat(), "event": OUTCOME_SL})
            _finalize(self.state, self.bet, OUTCOME_SL, ts, self.bet.sl_price, "bars")
        self._first_step = False
        return self.state

    def step_tick(self, ts: datetime, mid: float,
                  tick_msc: Optional[int] = None) -> Resolution:
        """Advance one tick (mid = (bid+ask)/2). Ordered → never ambiguous.

        `tick_msc` (millisecond epoch) de-dupes re-fetch overlaps across
        cycles: a tick at or before the last processed msc is ignored.
        """
        if self.state.outcome is not None:
            return self.state
        if tick_msc is not None:
            if tick_msc <= self.last_tick_msc:
                return self.state
            self.last_tick_msc = tick_msc
        # A tick is a point observation: update excursions with mid itself.
        _update_excursions(self.state, self.bet, mid, mid)

        if self.bet.direction == BUY:
            hit_tp = mid >= self.bet.tp_price
            hit_sl = mid <= self.bet.sl_price
        else:
            hit_tp = mid <= self.bet.tp_price
            hit_sl = mid >= self.bet.sl_price

        if hit_tp or hit_sl:
            outcome = OUTCOME_TP if hit_tp else OUTCOME_SL
            price = self.bet.tp_price if hit_tp else self.bet.sl_price
            self.state.events.append({
                "ts": ts.isoformat(), "event": outcome,
                "tick_mid": mid,
            })
            _finalize(self.state, self.bet, outcome, ts, price, "ticks")
        self._first_step = False
        return self.state


def resolve_bet_on_bars(
    bet: OpenBet,
    bars: "pd.DataFrame",
    policy: str = POLICY_SL_FIRST,
) -> Resolution:
    """Bar-conservative resolution: walk closed bars until the bet finishes.

    `bars` are the bars STRICTLY AFTER the entry bar (the entry bar's open
    IS the fill; its own high/low belongs to the resolver's first step —
    callers pass it as the first element when they have it). Returns the
    final state; outcome=None means data ended before any level was touched.
    """
    resolver = BetResolver(bet, policy)
    for ts, row in bars.iterrows():
        state = resolver.step_bar(
            ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts,
            float(row["high"]), float(row["low"]),
        )
        if state.outcome is not None:
            return state
    return resolver.state


def resolve_bet_on_ticks(
    bet: OpenBet,
    ticks: list[dict[str, Any]],
    policy: str = POLICY_SL_FIRST,
) -> Resolution:
    """Tick resolution: walk (bid/ask) ticks chronologically; first touch wins."""
    resolver = BetResolver(bet, policy)
    for tick in ticks:
        # Naive-local wall-clock time: ordering within one walk is all that
        # matters (tests use synthetic ticks; live walks in bet_engine.py
        # normalize to UTC before calling the resolver).
        ts = datetime.fromtimestamp(float(tick["time"]))
        mid = (float(tick["bid"]) + float(tick["ask"])) / 2.0
        msc = tick.get("time_msc")
        state = resolver.step_tick(ts, mid, int(msc) if msc else None)
        if state.outcome is not None:
            return state
    return resolver.state
