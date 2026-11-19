"""Causal proof: CTraderEngineAdapter — the object the bridge factory
returns to LiveTrader's nine self-serve points (engine wiring, 20/11).

The adapter's one job: give the engine the FULL MT5Bridge surface it
touches (six sync wrappers + the async order path) on ONE persistent
cTrader connection — routing everything through the bridge's persistent
loop because the engine's asyncio.run flows create throwaway loops that
a future created on them could never resolve (responses arrive on the
loop that owns the transport).

Proven from real artifacts (mock transport, zero network):

1. Exact surface keyset — the engine-facing methods and NOTHING else
   drifting in silently.
2. One handshake for: a cycle of sync calls → async order flow →
   adapter.disconnect() → more sync calls. The engine's per-flow
   connect/disconnect idiom must NOT tear the connection down.
3. Async order path works from a throwaway loop (asyncio.run — the
   engine's actual pattern) and returns metty's OrderResult shape.
4. Account guard: the adapter serves ONE engine account per process.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from ctrader import CTraderApiError, CTraderEngineAdapter
from ctrader import messages as msg
from metty.core.models import OrderResult

from tests.test_ctrader_bridge import (
    FakeTransport,
    execution_fill_payload,
    make_config,
    open_position_payload,
    push_execution_reply,
    spot_event,
    trader_payload,
)

# The exact public surface the engine's swing path touches. Anything new
# must be a deliberate act (extend this list with a reason), anything
# removed is a wiring break.
ADAPTER_ENGINE_SURFACE = {
    # sync wrappers (six self-serve points)
    "fetch_candles_sync",
    "fetch_account_info_sync",
    "get_spread_sync",
    "get_quote_sync",
    "position_exists_sync",
    "fetch_deal_history_sync",
    # async order path (open/close flows)
    "connect",
    "disconnect",
    "send_order",
    "close_position_with_fill",
    "close_position",
    # account guard — the runner-built factory calls it before handing
    # the adapter to the engine (one engine account per process)
    "assert_account",
}


def _public_methods(cls_or_obj) -> set[str]:
    return {
        name for name, member in inspect.getmembers(cls_or_obj, callable)
        if not name.startswith("_") and not isinstance(member, (property,))
    }


def _adapter(transport=None, account="IC1", **config_overrides):
    config = make_config(**config_overrides)
    return CTraderEngineAdapter(config, account=account, transport=transport)


# ----------------------------------------------------------------------
# 1. Exact surface keyset
# ----------------------------------------------------------------------

def test_adapter_surface_is_exactly_the_engine_contract():
    adapter = _adapter()
    assert _public_methods(adapter) == ADAPTER_ENGINE_SURFACE, (
        "adapter surface drifted — extend ADAPTER_ENGINE_SURFACE "
        "deliberately (with a reason) or fix the regression"
    )


@pytest.mark.parametrize("name", sorted(ADAPTER_ENGINE_SURFACE - {"close_position"}))
def test_engine_surface_methods_exist(name):
    adapter = _adapter()
    assert callable(getattr(adapter, name))


def test_async_surface_methods_are_coroutines():
    """The engine AWAITS connect/disconnect/send_order/close_position*."""
    for name in ("connect", "disconnect", "send_order", "close_position_with_fill",
                 "close_position"):
        assert asyncio.iscoroutinefunction(getattr(CTraderEngineAdapter, name)), (
            f"{name} must be awaitable — the engine's order flows await it"
        )


# ----------------------------------------------------------------------
# 2. One handshake across sync cycle + async order flow + disconnect
# ----------------------------------------------------------------------

class TestPersistentConnection:

    def _full_engine_cycle(self, adapter: CTraderEngineAdapter) -> None:
        """Everything the swing loop does to the bridge in one cycle."""
        info = adapter.fetch_account_info_sync()
        assert info is not None and info.equity == 2200.0
        assert adapter.get_quote_sync("XAUUSD") == (2350.00, 2350.30)
        assert adapter.get_spread_sync("XAUUSD") == 30.0
        assert adapter.position_exists_sync(555001) is False
        assert adapter.fetch_deal_history_sync("XAUUSD") == []
        assert adapter.fetch_candles_sync("XAUUSD", "M5", 10) is not None

    def test_one_handshake_across_two_cycles_and_order_flow(self):
        transport = FakeTransport()
        transport.replies[msg.TRADER_REQ] = [trader_payload(220_000)] * 16
        asyncio.run(transport.push(spot_event()))
        adapter = _adapter(transport)

        self._full_engine_cycle(adapter)
        self._full_engine_cycle(adapter)

        # async order flow from a THROWAWAY loop — the engine's pattern
        # (LiveTrader._execute → asyncio.run)
        async def _order_flow():
            ok = await adapter.connect()
            assert ok is True
            return await adapter.send_order("XAUUSD", "BUY", 0.01, 2344.0, 2356.0)

        asyncio.run(push_execution_reply(
            transport, msg.NEW_ORDER_REQ, execution_fill_payload(),
        ))
        result = asyncio.run(_order_flow())
        assert result.success is True

        # adapter.disconnect() is a no-op BY DESIGN — the engine calls it
        # believing the bridge is disposable; the connection stays hot.
        asyncio.run(adapter.disconnect())
        assert adapter.fetch_account_info_sync().equity == 2200.0

        auth_count = len(transport.sent_of(msg.APPLICATION_AUTH_REQ))
        assert auth_count == 1, (
            f"persistent-connection rule violated: {auth_count} handshakes for "
            "two sync cycles + one async order flow + disconnect (must be 1)"
        )


# ----------------------------------------------------------------------
# 3. Async order path shapes
# ----------------------------------------------------------------------

class TestOrderPath:

    def test_send_order_returns_metty_order_result(self):
        transport = FakeTransport()
        adapter = _adapter(transport)
        asyncio.run(transport.push(spot_event()))  # order path needs a live quote
        asyncio.run(push_execution_reply(
            transport, msg.NEW_ORDER_REQ, execution_fill_payload(
                position_id=555002, fill_price=2350.20, filled_cents=100,
            ),
        ))
        result = asyncio.run(adapter.send_order("XAUUSD", "BUY", 0.01, 2344.0, 2356.0))
        assert isinstance(result, OrderResult)
        assert set(type(result).model_fields.keys()) == set(OrderResult.model_fields.keys())
        assert result.success is True
        assert result.ticket == 555002
        assert result.price == 2350.20
        assert result.volume == 0.01  # broker fill echoes 100 cents at lotSize 10_000

    def test_send_order_connect_failure_is_failed_order_result(self):
        """Unconfigured credentials → success=False (the engine's ISSUE-C2
        guard then inserts NO DB row — same as MT5)."""
        adapter = _adapter(client_id="")
        result = asyncio.run(adapter.send_order("XAUUSD", "BUY", 0.01))
        assert isinstance(result, OrderResult)
        assert result.success is False
        assert result.error

    def test_close_position_with_fill_shape(self):
        transport = FakeTransport()
        adapter = _adapter(transport)
        transport.replies[msg.RECONCILE_REQ] = [
            {"position": open_position_payload(position_id=555001)},
        ]
        asyncio.run(push_execution_reply(
            transport, msg.CLOSE_POSITION_REQ,
            execution_fill_payload(position_id=555001, fill_price=2351.5),
        ))
        ok, fill = asyncio.run(adapter.close_position_with_fill(555001))
        assert ok is True
        assert fill == 2351.5

    def test_close_missing_position_is_false_none(self):
        transport = FakeTransport()
        adapter = _adapter(transport)  # reconcile default: no positions
        ok, fill = asyncio.run(adapter.close_position_with_fill(999999))
        assert ok is False
        assert fill is None


# ----------------------------------------------------------------------
# 4. One engine account per process
# ----------------------------------------------------------------------

class TestAccountGuard:

    def test_mismatched_account_raises(self):
        adapter = _adapter(account="IC1")
        other = type("FakeMT5Config", (), {"name": "IC2"})()
        with pytest.raises(CTraderApiError) as exc:
            adapter.assert_account(other)
        assert exc.value.code == "ACCOUNT_MISMATCH"

    def test_matching_account_passes(self):
        adapter = _adapter(account="IC1")
        adapter.assert_account(type("FakeMT5Config", (), {"name": "IC1"})())

    def test_no_name_is_tolerated(self):
        """Call sites always pass a registry config with a name, but a
        nameless config must not crash the guard (nothing to compare)."""
        adapter = _adapter(account="IC1")
        adapter.assert_account(type("FakeMT5Config", (), {"name": ""})())

    def test_account_uppercased(self):
        adapter = _adapter(account="ic1")
        assert adapter.account == "IC1"


# ----------------------------------------------------------------------
# 5. LiveTrader integration through the REAL injection seam
# ----------------------------------------------------------------------

class TestEngineIntegration:
    """The full loop the compose will run: LiveTrader("IC1") with the
    runner-built factory → equity/spread/quote come from the (mock) bridge,
    and the equity stamp lands in the DB — NOT the paper fallback."""

    @pytest.fixture()
    def ic_env(self, monkeypatch, tmp_path):
        import metty.bridge.client as bridge_client
        from metty.core.db import init_db, insert_account
        db = tmp_path / "oracle_ic.db"
        init_db(db)
        insert_account(
            name="IC1", balance=2200.0, leverage=1000,
            bridge_host="mt5ic1", bridge_port=8001,
            signal_group="portfolio", db_path=db,
        )
        # Minimal per-account env: the constructor needs the registry to
        # accept IC1 (ACCOUNTS) and resolve its symbol.
        monkeypatch.setenv("ACCOUNTS", "IC1")
        monkeypatch.setenv("ACCOUNT_TYPE_IC1", "ctrader")
        monkeypatch.delenv("MOCK_SPREAD_POINTS", raising=False)
        monkeypatch.delenv("MOCK_SPREAD_POINTS_IC1", raising=False)
        original_bridge = bridge_client.MT5Bridge
        yield db
        # teardown: restore the module seam the injection patched (in
        # place — other modules hold the same module object)
        bridge_client.MT5Bridge = original_bridge
        if getattr(bridge_client, "_injected_bridge_factory", None) is not None:
            del bridge_client._injected_bridge_factory

    def test_equity_and_stamp_come_from_the_bridge(self, ic_env):
        import os

        import scripts.oracle_runner as runner
        monkeypatch_env = {
            "CTRADER_CLIENT_ID": "test-client-id",
            "CTRADER_CLIENT_SECRET": "test-client-secret",
            "CTRADER_ACCESS_TOKEN": "test-access-token",
            "CTRADER_ACCOUNT_ID_IC1": "49633503",
            "CTRADER_HOST_TYPE_IC1": "demo",
        }
        for k, v in monkeypatch_env.items():
            os.environ[k] = v
        try:
            factory = runner._build_ctrader_bridge_factory("IC1")
            # Swap the adapter's transport for the mock BEFORE any engine
            # call: the runner built the real (lazy) TCP transport — safe
            # to construct, but only the mock may ever see traffic.
            from metty.execution.live_trader import LiveTrader
            trader = LiveTrader(
                account="IC1", db_path=ic_env, dry_run=True,
                bridge_factory=factory,
            )
            adapter = factory(type("C", (), {"name": "IC1"})())
            transport = FakeTransport()
            transport.replies[msg.TRADER_REQ] = [trader_payload(220_000)] * 8
            asyncio.run(transport.push(spot_event()))
            # Direct swap on the live adapter's bridge transport:
            adapter.bridge._transport = transport

            equity = trader._get_equity()
            assert equity == 2200.0, (
                "equity must come from the bridge (real account snapshot), "
                f"got {equity!r} — the paper fallback must never fire while "
                "the bridge answers"
            )
            trader._write_equity_snapshot()
            from metty.core.db import get_connection
            conn = get_connection(ic_env)
            try:
                row = conn.execute(
                    "SELECT current_equity FROM accounts WHERE name='IC1'"
                ).fetchone()
            finally:
                conn.close()
            assert row[0] == 2200.0
        finally:
            for k in monkeypatch_env:
                os.environ.pop(k, None)
