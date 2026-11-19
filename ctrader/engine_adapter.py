"""CTraderEngineAdapter — LiveTrader-facing facade over CTraderBridge.

Engine wiring stage 2 (dispatch 20/11, option A): LiveTrader's nine
self-serve points construct their bridge inline as
`MT5Bridge(config)`; the sanctioned injection is a `bridge_factory`
param on the LiveTrader constructor that replaces that seam for the
process. The object this factory returns must carry the FULL surface the
engine touches — six sync wrappers plus the async order path — and must
do it on ONE persistent connection:

- The engine's async flows (`asyncio.run(_execute())` in the open/close
  paths) run on a THROWAWAY loop, but CTraderBridge's connection
  (receive task, pending futures, heartbeat) is bound to the loop that
  ran connect(). A future created on any other loop never resolves —
  responses arrive on the owning loop. Every adapter method therefore
  routes through CTraderBridge.run_coroutine_sync (persistent loop),
  never awaits bridge coroutines directly.

- The engine's per-flow `connect()` / `disconnect()` idiom assumes a
  disposable bridge (MT5Bridge connects and tears down per call because
  its MT5 terminal is cheap to re-open). Here a connect is ~2s of app+account auth
  shared by a whole cycle of sync calls, so `disconnect()` is a NO-OP BY
  DESIGN: the connection is process-lifetime, one per engine per
  account. The wire is torn down when the process dies.

Not a general-purpose wrapper: it exists so the engine can trade through
cTrader without touching metty/. Anything outside the engine surface
(health monitoring, discovery) uses CTraderBridge directly.
"""

import logging
from typing import Any, Optional

from metty.core.models import OrderResult  # read-only import (engine contract)

from .bridge import CTraderApiError, CTraderBridge
from .config import CTraderConfig

logger = logging.getLogger(__name__)


class CTraderEngineAdapter:
    """Persistent-connection facade with the MT5Bridge engine surface."""

    def __init__(
        self,
        config: CTraderConfig,
        account: str,
        transport: Optional[Any] = None,
    ) -> None:
        self._account = account.upper()
        self._bridge = CTraderBridge(config, transport=transport)

    @property
    def account(self) -> str:
        """Engine account name this adapter was built for (e.g. 'IC1')."""
        return self._account

    @property
    def bridge(self) -> CTraderBridge:
        """The wrapped bridge (diagnostics/tests — not engine surface)."""
        return self._bridge

    # ------------------------------------------------------------------
    # Sync surface — the six wrappers the engine's swing path calls.
    # The persistent-loop policy lives in CTraderBridge itself, so these
    # are pure delegation: same signature, same failure shape.
    # ------------------------------------------------------------------

    def fetch_candles_sync(
        self,
        symbol: str = "XAUUSD",
        timeframe: str = "M5",
        count: int = 500,
    ):
        """Candles (DataFrame; empty on failure — never None)."""
        return self._bridge.fetch_candles_sync(symbol, timeframe, count)

    def fetch_account_info_sync(self):
        """AccountInfo (None when the bridge cannot answer)."""
        return self._bridge.fetch_account_info_sync()

    def get_spread_sync(self, symbol: str = "XAUUSD") -> Optional[float]:
        """Spread in POINTS (None when unavailable)."""
        return self._bridge.get_spread_sync(symbol)

    def get_quote_sync(self, symbol: str = "XAUUSD") -> Optional[tuple[float, float]]:
        """(bid, ask) — None when unavailable."""
        return self._bridge.get_quote_sync(symbol)

    def position_exists_sync(self, ticket: int) -> Optional[bool]:
        """True/False/None (None = UNKNOWN — never treat as gone)."""
        return self._bridge.position_exists_sync(ticket)

    def fetch_deal_history_sync(
        self, symbol: str = "XAUUSD", days_back: int = 7,
    ) -> Optional[list[dict]]:
        """MT5-shaped deal dicts; None = bridge failure (skip reconcile)."""
        return self._bridge.fetch_deal_history_sync(symbol, days_back=days_back)

    # ------------------------------------------------------------------
    # Async surface — the engine's open/close order flows
    # (connect → send_order / close_position_with_fill → disconnect).
    # Each method is a coroutine so `await bridge.connect()` etc. work
    # unchanged, but the work happens on the persistent loop.
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Idempotent ensure-connected on the persistent loop."""
        return self._bridge.ensure_connected_sync()

    async def disconnect(self) -> None:
        """NO-OP BY DESIGN (see module docstring).

        The engine calls this after each order flow believing the bridge
        is disposable. Keeping the connection hot is the point of this
        adapter — the next cycle's sync calls reuse it instead of paying
        the ~2s handshake per cycle.
        """
        logger.debug(
            "[%s] adapter.disconnect() no-op — persistent connection by design",
            self._account,
        )
        return None

    async def send_order(
        self,
        symbol: str,
        direction: str,
        lots: float,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> OrderResult:
        """MARKET order routed through the persistent loop.

        Connect failure returns a failed OrderResult (the engine's
        ISSUE-C2 guard treats success=False as no-DB-row — exactly the
        MT5 behavior).
        """
        if not self._bridge.ensure_connected_sync():
            return OrderResult(
                success=False,
                error=f"cTrader connect failed for {self._account}",
            )
        return self._bridge.run_coroutine_sync(
            self._bridge.send_order(symbol, direction, lots, stop_loss, take_profit),
        )

    async def close_position_with_fill(
        self, ticket: int,
    ) -> tuple[bool, Optional[float]]:
        """Close a position; (success, fill_price). (False, None) on any
        failure — same shape the engine expects from MT5Bridge."""
        if not self._bridge.ensure_connected_sync():
            return False, None
        return self._bridge.run_coroutine_sync(
            self._bridge.close_position_with_fill(ticket),
        )

    async def close_position(self, ticket: int) -> bool:
        """Close by ticket (bool) — parity with MT5Bridge.close_position."""
        ok, _fill = await self.close_position_with_fill(ticket)
        return ok

    def assert_account(self, mt5_config: Any) -> None:
        """Guard: this adapter serves ONE engine account per process.

        The call sites build an MT5-style AccountConfig from the registry
        for the account the engine was started with; if a different name
        arrives, two engines (or an MT5 account) share this process and
        the factory would silently route the WRONG account's orders.
        """
        name = str(getattr(mt5_config, "name", "") or "").upper()
        if name and name != self._account:
            raise CTraderApiError(
                "ACCOUNT_MISMATCH",
                f"adapter built for {self._account} but call site asked for "
                f"{name} — one engine account per process (wiring rule 20/11)",
            )
