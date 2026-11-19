"""Causal proof: sync facility survives the loop that ran connect() dying.

Dispatch 20/11 (sync-loop timeout): the isolation probe showed every
request/response sync wrapper timing out at exactly request_timeout with
an EMPTY error message (str(TimeoutError())) while cache-backed wrappers
answered instantly — through the REAL bridge, on the demo host.

Root cause this file pins (proven against the pre-fix code, RED):

    connect() binds the receive pump + transport internals to the loop
    that runs it. ensure_connected_sync() only checked ``_connected`` —
    it had no idea WHICH loop owned the connection — so a bridge
    connected on loop A (an asyncio.run() flow) happily answered True
    from sync code driving loop B (the persistent loop): requests went
    out on B, responses were consumed by a pump parked on A, and every
    request/response wait died at request_timeout.

The lifecycle the engine + probe actually produce is therefore a
CONTRACT (dispatch done-criteria 1): a bridge whose connect() ran on a
throwaway loop must still serve sync calls on the persistent loop — by
migrating the connection there exactly once.

Proven here (mock transport, zero network):

1. RED core: connect on loop A, park it (never run it again) → sync
   wrappers must return real data (probe shape).
2. Same with loop A actually DEAD (asyncio.run() closed it).
3. Migration is exactly ONCE — never per call (hot-path rule).
4. A zombie receive/heartbeat task from the old loop can never mark the
   NEW connection dead nor fail its pending futures (identity guard).
5. Spot cache is LIVE on the persistent loop (frozen-cache disease:
   the engine's spread gate must read pump-updated quotes, not the
   connect-time snapshot).
6. Adapter/engine lifecycle (control): asyncio.run(adapter.connect()) +
   sync wrappers + a second async connect = ONE handshake total.
"""

from __future__ import annotations

import asyncio

import pytest

from ctrader import CTraderBridge
from ctrader import messages as msg
from ctrader.engine_adapter import CTraderEngineAdapter
from metty.core.models import AccountInfo

from tests.test_ctrader_bridge import (
    FakeTransport,
    make_config,
    spot_event,
    trader_payload,
)


def _two_connect_server(transport: FakeTransport) -> None:
    """Script enough replies for TWO full connect() handshakes.

    The auth pairs auto-answer (FakeTransport.DEFAULT_REPLIES); symbol/
    asset discovery needs explicit scripting — two entries each so the
    loop-migration reconnect can complete without new test code.
    """
    from tests.test_ctrader_bridge import (
        assets_payload,
        gold_symbols_payload,
        symbol_by_id_payload,
    )

    transport.replies[msg.SYMBOLS_LIST_REQ] = [
        gold_symbols_payload(), gold_symbols_payload(),
    ]
    transport.replies[msg.SYMBOL_BY_ID_REQ] = [
        symbol_by_id_payload(), symbol_by_id_payload(),
    ]
    transport.replies[msg.ASSET_LIST_REQ] = [
        assets_payload(), assets_payload(),
    ]
    transport.replies[msg.TRADER_REQ] = [trader_payload(), trader_payload()]


def _auth_count(transport: FakeTransport) -> int:
    return sum(
        m.payload_type == msg.APPLICATION_AUTH_REQ for m in transport.sent_messages
    )


class TestForeignLoopMigration:
    """connect() on loop A → sync calls must migrate to loop B."""

    def test_probe_shape_sync_calls_resolve_after_foreign_connect(self):
        """THE probe contract (dispatch done-criteria 1): a bridge
        connected on a parked foreign loop must still serve real data
        through the persistent loop — account info with balance > 0."""
        transport = FakeTransport()
        _two_connect_server(transport)
        bridge = CTraderBridge(make_config(), transport=transport)

        loop_a = asyncio.new_event_loop()
        try:
            assert loop_a.run_until_complete(bridge.connect()) is True
            # loop A stays PARKED from here on — exactly the probe: its
            # thread blocks in future.result() while sync calls run.

            info = bridge.fetch_account_info_sync()
            assert isinstance(info, AccountInfo), (
                "sync wrapper must resolve on the persistent loop — "
                f"got {info!r} (probe: 10s timeout + empty error)"
            )
            assert info.balance > 0
        finally:
            loop_a.close()

    def test_asyncio_run_loop_death_sync_calls_resolve(self):
        """Variant with loop A really dead (asyncio.run closed it) —
        sync calls must still resolve on the persistent loop."""
        transport = FakeTransport()
        _two_connect_server(transport)
        bridge = CTraderBridge(make_config(), transport=transport)

        assert asyncio.run(bridge.connect()) is True  # loop A dies here

        info = bridge.fetch_account_info_sync()
        assert isinstance(info, AccountInfo), (
            "sync wrapper must survive the connect() loop dying"
        )
        assert info.balance > 0

    def test_migration_exactly_once_never_per_call(self):
        """Hot-path rule: the persistent connection is rebuilt ONCE when
        ownership moves to the persistent loop — never per sync call."""
        transport = FakeTransport()
        _two_connect_server(transport)
        bridge = CTraderBridge(make_config(), transport=transport)

        loop_a = asyncio.new_event_loop()
        try:
            loop_a.run_until_complete(bridge.connect())
            assert bridge.fetch_account_info_sync() is not None
            # A spot flows in on the persistent loop (the pump that owns
            # the migrated connection):
            bridge.run_coroutine_sync(transport.push(spot_event()))
            # A whole engine cycle of further calls:
            assert bridge.get_quote_sync("XAUUSD") is not None
            assert bridge.get_spread_sync("XAUUSD") is not None
            assert bridge.fetch_account_info_sync() is not None
            assert bridge.position_exists_sync(12345) is False
        finally:
            loop_a.close()

        # one handshake on loop A + exactly one migration handshake
        assert _auth_count(transport) == 2, (
            "migration must reconnect exactly once, not per call"
        )
        assert bridge._conn_loop is bridge._sync_loop.loop, (
            "after migration the persistent loop must own the connection"
        )

    def test_connect_fast_path_when_persistent_loop_owns_connection(self):
        """Control: once the persistent loop owns the connection, a
        second ensure is a no-op (no reconnect churn)."""
        transport = FakeTransport()
        _two_connect_server(transport)
        bridge = CTraderBridge(make_config(), transport=transport)

        assert bridge.ensure_connected_sync() is True
        assert bridge.ensure_connected_sync() is True
        assert bridge.fetch_account_info_sync() is not None
        assert _auth_count(transport) == 1

    def test_zombie_old_loop_tasks_cannot_kill_new_connection(self):
        """Identity guard: reviving loop A runs the cancelled old
        receive/heartbeat tasks' teardown — that must NOT mark the new
        connection dead nor fail its pending futures."""
        transport = FakeTransport()
        _two_connect_server(transport)
        bridge = CTraderBridge(make_config(), transport=transport)

        loop_a = asyncio.new_event_loop()
        try:
            loop_a.run_until_complete(bridge.connect())
            assert bridge.fetch_account_info_sync() is not None, (
                "precondition: migration must have happened"
            )
            assert bridge._connected is True

            # Revive loop A: zombie cancels deliver their CancelledError
            # here, running the old receive loop's finally block.
            loop_a.run_until_complete(asyncio.sleep(0.1))

            assert bridge._connected is True, (
                "a zombie task from the abandoned loop must not kill the "
                "new connection"
            )
            assert bridge.fetch_account_info_sync() is not None
        finally:
            loop_a.close()


class TestZombieBodyGate:
    """Cross-context review HIGH (20/11): the identity guards protect task
    TEARDOWN only — a zombie receive-loop BODY from an abandoned foreign
    loop still reads the CURRENT transport at call time (a heartbeat
    echo would write to the NEW connection's socket; a late anonymous
    EXECUTION_EVENT could claim a NEW order's waiter with the OLD
    order's ticket). The body must be gated on the transport captured
    at loop entry.

    Injected fakes keep their identity across a migration (only the real
    path replaces the object), so these tests swap `bridge._transport`
    after the migration — exactly what `_refresh_transport_for` does
    for a real TcpNewlineTransport (pinned separately below).
    """

    def test_frames_from_abandoned_transport_never_reach_handler(self):
        """A frame arriving on the OLD transport after its replacement must
        be dropped — observable through the spot cache it would otherwise
        poison.

        The zombie here is the ORIGINAL receive loop, still alive: a real
        migration swaps the transport object while the abandoned socket's
        frame can still land in the old queue BEFORE the loop's cancel is
        delivered — that window is what the gate closes. (Driving it through
        the real abandon() would cancel the zombie first and the test would
        pass with or without the gate — proven tautological, so it pins the
        body gate directly instead.)
        """
        from ctrader.protocol import encode_frame

        transport = FakeTransport()
        _two_connect_server(transport)
        bridge = CTraderBridge(make_config(), transport=transport)
        loop_a = asyncio.new_event_loop()
        try:
            loop_a.run_until_complete(bridge.connect())
            # The connection's receive loop is ALIVE on loop A, consuming
            # this queue (the abandoned socket's frame source).
            old_queue = transport._queue
            # The real migration path (_refresh_transport_for) replaces the
            # transport object — mirror that swap:
            bridge._transport = FakeTransport()

            # The abandoned socket still delivers (real wire: the leaked fd
            # keeps receiving server pushes).
            old_queue.put_nowait(encode_frame(spot_event(bid=2360.00, ask=2360.50)))
            loop_a.run_until_complete(asyncio.sleep(0.05))  # zombie runs

            # Read the cache directly — get_quote_sync() would migrate the
            # connection first and stop being about this window.
            assert bridge._spot.get(101) is None, (
                "frames from the abandoned transport must be dropped — got "
                f"{bridge._spot.get(101)!r} poisoned from a stale frame"
            )
        finally:
            loop_a.close()

    def test_zombie_heartbeat_never_writes_to_new_transport(self):
        """A zombie heartbeat must not send on the NEW transport after a
        migration — the gate must stop it before any _send."""
        transport = FakeTransport()
        _two_connect_server(transport)
        config = make_config(heartbeat_interval=0.02)
        bridge = CTraderBridge(config, transport=transport)
        loop_a = asyncio.new_event_loop()
        try:
            async def _scenario() -> None:
                assert await bridge.connect() is True
                old_writes = len(transport.sent_messages)
                await asyncio.sleep(0.06)  # ≥2 ticks: zombie heartbeats flow
                assert len(transport.sent_messages) > old_writes, (
                    "precondition: the heartbeat is alive on this transport"
                )
                bridge._transport = FakeTransport()  # migration swap
                await asyncio.sleep(0.06)  # more ticks on the SAME loop
                # (task was never cancelled here — the gate alone must
                # stop the writes; cancel would mask it)

            loop_a.run_until_complete(_scenario())
            # Wait out of run_until_complete: heartbeat task is still
            # alive on loop A — assert the NEW transport stayed untouched.
            new_transport = bridge._transport
            assert isinstance(new_transport, FakeTransport)
            heartbeats_on_new = [
                m for m in new_transport.sent_messages
                if m.payload_type == msg.HEARTBEAT_EVENT
            ]
            assert not heartbeats_on_new, (
                "zombie heartbeat must not write to the new transport"
            )
            # And the zombie stopped itself instead of spinning:
            loop_a.run_until_complete(asyncio.sleep(0.05))
        finally:
            loop_a.close()


class TestRefreshTransportFor:
    """Cross-context review LOW (20/11): the real-transport cross-loop
    recreation branch is production logic with no coverage (every test
    uses FakeTransport). Pin the decision itself."""

    def test_real_transport_cross_loop_gets_fresh_object(self):
        from ctrader.transport import TcpNewlineTransport

        class SeamTransport(TcpNewlineTransport):
            # Test seam: never opens a socket (connect is never called —
            # this test only exercises the DECISION in _refresh_transport_for).
            pass

        bridge = CTraderBridge(make_config(), transport=SeamTransport("h", 1))
        loop_old, loop_new = asyncio.new_event_loop(), asyncio.new_event_loop()
        try:
            bridge._transport_loop = loop_old  # transport was used there
            bridge._refresh_transport_for(loop_new)
            assert bridge._transport is not None
            assert not isinstance(bridge._transport, SeamTransport), (
                "cross-loop must REPLACE the real transport (fresh object, "
                "loop-bound queue/decoder/pump)"
            )
            assert isinstance(bridge._transport, TcpNewlineTransport)
        finally:
            loop_old.close()
            loop_new.close()

    def test_same_loop_and_injected_transports_keep_identity(self):
        from ctrader.transport import TcpNewlineTransport

        class SeamTransport(TcpNewlineTransport):
            pass

        bridge = CTraderBridge(make_config(), transport=SeamTransport("h", 1))
        loop, other = asyncio.new_event_loop(), asyncio.new_event_loop()
        try:
            # Same loop → keep the transport object.
            bridge._transport_loop = loop
            bridge._refresh_transport_for(loop)
            assert isinstance(bridge._transport, SeamTransport)

            # Injected fake (test seam / any non-TcpNewlineTransport):
            # identity is kept even across loops — fakes are loop-safe.
            fake = FakeTransport()
            bridge._transport = fake
            bridge._transport_loop = other
            bridge._refresh_transport_for(loop)
            assert bridge._transport is fake, (
                "injected transports must keep their identity — only the "
                "real TcpNewlineTransport is loop-bound and gets recreated"
            )
        finally:
            loop.close()
            other.close()


class TestSpotCacheLiveness:
    """Frozen-cache disease (dispatch done-criteria 2): quote/spread
    must reflect pump updates on the persistent loop, not the
    connect-time snapshot."""

    def _migrated_bridge(self):
        transport = FakeTransport()
        _two_connect_server(transport)
        bridge = CTraderBridge(make_config(), transport=transport)
        loop_a = asyncio.new_event_loop()
        loop_a.run_until_complete(bridge.connect())
        loop_a.close()
        # Trigger the migration the engine cycle would (first sync call):
        # until this runs, the receive pump is still parked on dead loop A
        # and pushes into the old queue would fail on the closed loop.
        assert bridge.ensure_connected_sync() is True
        assert bridge._conn_loop is bridge._sync_loop.loop
        return bridge, transport

    def test_quote_updates_from_persistent_loop_pushes(self):
        """Spot events flowing into the persistent loop must reach
        get_quote_sync — two pushes, two different values."""
        bridge, transport = self._migrated_bridge()

        bridge.run_coroutine_sync(transport.push(spot_event(bid=2360.00, ask=2360.50)))
        quote = bridge.get_quote_sync("XAUUSD")
        assert quote == (2360.00, 2360.50), (
            f"first spot push must be live, got {quote!r}"
        )

        bridge.run_coroutine_sync(transport.push(spot_event(bid=2361.25, ask=2361.75)))
        quote = bridge.get_quote_sync("XAUUSD")
        assert quote == (2361.25, 2361.75), (
            "second push must REPLACE the cache value — a frozen cache "
            "would still answer the first push"
        )

    def test_spread_tracks_quote_updates(self):
        """The engine's spread gate reads get_spread_sync — it must see
        the live pump, not a stale snapshot."""
        bridge, transport = self._migrated_bridge()

        bridge.run_coroutine_sync(transport.push(spot_event(bid=2360.00, ask=2360.30)))
        assert bridge.get_spread_sync("XAUUSD") == pytest.approx(30.0)

        bridge.run_coroutine_sync(transport.push(spot_event(bid=2360.00, ask=2362.30)))
        assert bridge.get_spread_sync("XAUUSD") == pytest.approx(230.0), (
            "spread must track the newest spot event"
        )


class TestAdapterLifecycleControl:
    """Control/lock: the ENGINE's own lifecycle (adapter routes every
    touch through the persistent loop) stays a single handshake — the
    migration path must not add reconnect churn to it."""

    def test_adapter_async_connect_then_sync_all_one_handshake(self):
        from metty.core.models import AccountConfig

        transport = FakeTransport()
        _two_connect_server(transport)
        config = make_config()
        adapter = CTraderEngineAdapter(config, account="IC1", transport=transport)

        # The engine idiom: asyncio.run flows on throwaway loops.
        assert asyncio.run(adapter.connect()) is True
        assert adapter.fetch_account_info_sync() is not None
        adapter.bridge.run_coroutine_sync(transport.push(spot_event()))
        assert adapter.get_quote_sync("XAUUSD") is not None
        # The engine opens/closes flows around each order:
        assert asyncio.run(adapter.connect()) is True

        assert _auth_count(transport) == 1, (
            "adapter lifecycle must keep ONE connection — migration only "
            "applies to bridges connected on a foreign loop"
        )
        assert adapter.bridge._conn_loop is adapter.bridge._sync_loop.loop

        # The adapter guard still works after migration machinery exists.
        with pytest.raises(Exception, match="ACCOUNT_MISMATCH"):
            adapter.assert_account(
                AccountConfig(
                    name="IC2", broker_login="", broker_server="",
                    balance=0.0, leverage=1, bridge_host="", bridge_port=1,
                    signal_group="portfolio",
                )
            )
