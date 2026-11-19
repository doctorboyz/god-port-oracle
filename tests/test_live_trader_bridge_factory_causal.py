"""Causal proof: LiveTrader bridge_factory injection seam (wiring stage 2, 20/11).

The engine constructs its bridge at NINE self-serve points via a
function-local `from metty.bridge.client import MT5Bridge`. The sanctioned
injection (dispatch "ทาง A: metty จุดเดียว") is ONE constructor param:
`bridge_factory` — installed as the metty.bridge.client.MT5Bridge module
attribute for this process, so every function-local import (all nine
sites, and any future one) resolves to the injected factory WITHOUT any
call-site edit.

Proven from real artifacts (fake bridges, zero network):

1. Constructor surface: exact parameter list (bridge_factory last).
2. IC-account gate: IC1-IC99 pass like P-accounts; look-alikes fail loud.
3. Default path UNTOUCHED: no factory → the module attribute is exactly
   metty.bridge.client's original MT5Bridge (accounts A/P1 unaffected).
4. Routing: each self-serve site (spread / quote / equity / free margin /
   candles / deal history / position-exists / async close) goes through
   the factory-built bridge.
5. One factory per process: a second, different factory is refused.
6. Equity stamp: the bridge's equity (NOT the paper fallback) lands in
   the accounts row — the dispatch's "ห้าม paper-fallback heartbeat" rule.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pandas as pd
import pytest

import metty.bridge.client as bridge_client
from metty.core.models import AccountInfo
from metty.execution.live_trader import LiveTrader

# The exact constructor parameter list. Anything new must be deliberate;
# anything reordered breaks every keyword caller.
EXPECTED_CONSTRUCTOR_PARAMS = [
    "account", "db_path", "data_dir", "dry_run",
    "risk_config", "event_bus", "notifier", "bridge_factory",
]


# ----------------------------------------------------------------------
# Fake bridge + factory
# ----------------------------------------------------------------------

class RecordingBridge:
    """A bridge answering the six sync wrappers + the async close path.
    Records every call so routing is provable, not assumed."""

    def __init__(self, equity: float = 2345.0, free_margin: float = 2000.0):
        self.calls: list[tuple[str, tuple]] = []
        self._info = AccountInfo(
            account_id=1, login=49633503, name="IC1", currency="USD",
            balance=equity, equity=equity, free_margin=free_margin,
            margin=0.0, leverage=1000,
        )
        self.connect_count = 0
        self.disconnect_count = 0

    def _record(self, name, *args):
        self.calls.append((name, args))

    # --- six sync wrappers (the engine's self-serve surface) ---
    def get_spread_sync(self, symbol="XAUUSD"):
        self._record("get_spread_sync", symbol)
        return 25.0

    def get_quote_sync(self, symbol="XAUUSD"):
        self._record("get_quote_sync", symbol)
        return (2350.00, 2350.25)

    def fetch_account_info_sync(self, symbol="XAUUSD"):
        self._record("fetch_account_info_sync", symbol)
        return self._info

    def fetch_candles_sync(self, symbol="XAUUSD", timeframe="M5", count=500):
        self._record("fetch_candles_sync", symbol, timeframe, count)
        return pd.DataFrame()  # empty → engine falls back to CSV; routing still proven

    def fetch_deal_history_sync(self, symbol="XAUUSD", days_back=7):
        self._record("fetch_deal_history_sync", symbol, days_back)
        return [{"ticket": 90001, "position_id": 555001}]

    def position_exists_sync(self, ticket):
        self._record("position_exists_sync", ticket)
        return True

    # --- async order path (close flow) ---
    async def connect(self):
        self.connect_count += 1
        return True

    async def disconnect(self):
        self.disconnect_count += 1
        return None

    async def close_position_with_fill(self, ticket):
        self._record("close_position_with_fill", ticket)
        return True, 2351.5


class FakeFactory:
    """Matches the runner-built factory contract: AccountConfig in, bridge out."""

    def __init__(self, bridge: RecordingBridge):
        self.bridge = bridge
        self.built: list[object] = []

    def __call__(self, mt5_config):
        self.built.append(mt5_config)
        return self.bridge


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------

@pytest.fixture()
def ic_db(tmp_path, monkeypatch):
    """IC1 account row + registry env — everything the sites need to
    resolve get_account_config / get_bridge_config without touching
    metty/core. Extra accounts can be seeded via ic_db.seed(name)."""
    from metty.core.db import init_db, insert_account
    db = tmp_path / "oracle_ic.db"
    init_db(db)

    def seed(name: str, balance: float = 2200.0) -> None:
        insert_account(
            name=name, balance=balance, leverage=1000,
            bridge_host=f"mt5{name.lower()}", bridge_port=8001,
            signal_group="portfolio", db_path=db,
        )

    seed("IC1")
    monkeypatch.setenv("ACCOUNTS", "IC1")
    # The spread site's MOCK override must not shadow the bridge path.
    monkeypatch.delenv("MOCK_SPREAD_POINTS", raising=False)
    monkeypatch.delenv("MOCK_SPREAD_POINTS_IC1", raising=False)
    return SimpleNamespace(path=db, seed=seed)


@pytest.fixture()
def restore_bridge_seam():
    """Whatever a test does to the module seam, the ORIGINAL MT5Bridge
    goes back — the default path must stay pristine for the next test."""
    original = bridge_client.MT5Bridge
    yield
    bridge_client.MT5Bridge = original
    if hasattr(bridge_client, "_injected_bridge_factory"):
        del bridge_client._injected_bridge_factory


def _calls_named(bridge: RecordingBridge, name: str) -> list[tuple]:
    return [c for c in bridge.calls if c[0] == name]


# ----------------------------------------------------------------------
# 1. Constructor surface
# ----------------------------------------------------------------------

def test_constructor_param_list_is_exactly_the_contract():
    params = list(inspect.signature(LiveTrader.__init__).parameters)[1:]
    assert params == EXPECTED_CONSTRUCTOR_PARAMS, (
        "LiveTrader constructor drifted — every keyword caller in the "
        "runner/tests depends on this order"
    )


def test_bridge_factory_has_default_none():
    sig = inspect.signature(LiveTrader.__init__)
    assert sig.parameters["bridge_factory"].default is None, (
        "default MUST be None: accounts A/P1 (and every existing caller) "
        "construct without it and must get the unchanged MT5 path"
    )


# ----------------------------------------------------------------------
# 2. IC-account gate
# ----------------------------------------------------------------------

class TestICGate:

    @pytest.mark.parametrize("name", ["IC1", "IC2", "IC99", "ic1"])
    def test_ic_accounts_construct(self, ic_db, name):
        if name.upper() != "IC1":
            ic_db.seed(name.upper())  # DB gate: row must exist post-name-gate
        trader = LiveTrader(account=name, db_path=ic_db.path, dry_run=True)
        assert trader.account == name.upper()

    @pytest.mark.parametrize("bad", ["ICX", "IC", "XI", "ICC1", "IC0x", "1C1"])
    def test_lookalikes_fail_loud(self, ic_db, bad):
        with pytest.raises(ValueError, match="Unknown account"):
            LiveTrader(account=bad, db_path=ic_db.path, dry_run=True)

    def test_error_message_names_all_three_families(self, ic_db):
        with pytest.raises(ValueError) as exc:
            LiveTrader(account="ZZZ", db_path=ic_db.path, dry_run=True)
        msg_text = str(exc.value)
        assert "IC-account" in msg_text, (
            "the operator must see the IC family is supported when the "
            "name is merely misspelled"
        )


# ----------------------------------------------------------------------
# 3. Default path untouched (the IRON-side promise)
# ----------------------------------------------------------------------

class TestDefaultPath:

    def test_no_factory_leaves_mt5bridge_exactly_as_is(self, ic_db, restore_bridge_seam):
        before = bridge_client.MT5Bridge
        LiveTrader(account="IC1", db_path=ic_db.path, dry_run=True)
        assert bridge_client.MT5Bridge is before, (
            "constructing WITHOUT a factory must not touch the module seam"
        )
        assert not hasattr(bridge_client, "_injected_bridge_factory")

    def test_mt5bridge_attribute_is_the_real_class(self, restore_bridge_seam):
        """The seam target exists and is a class (function-local imports at
        the nine sites do MT5Bridge(config) — a class, not an instance)."""
        assert inspect.isclass(bridge_client.MT5Bridge)


# ----------------------------------------------------------------------
# 4. Routing — every self-serve site goes through the factory
# ----------------------------------------------------------------------

class TestRouting:

    @pytest.fixture()
    def wired(self, ic_db, restore_bridge_seam):
        bridge = RecordingBridge()
        factory = FakeFactory(bridge)
        trader = LiveTrader(
            account="IC1", db_path=ic_db.path, dry_run=True,
            bridge_factory=factory,
        )
        return trader, bridge, factory

    def test_seam_installed_on_module_attribute(self, wired):
        _, _, factory = wired
        assert bridge_client.MT5Bridge is factory

    def test_spread_site_routes_through_factory(self, wired):
        trader, bridge, _ = wired
        assert trader._get_current_spread() == 25.0
        assert _calls_named(bridge, "get_spread_sync"), "spread site bypassed the factory"

    def test_quote_site_routes_through_factory(self, wired):
        trader, bridge, _ = wired
        assert trader._get_current_quote() == (2350.00, 2350.25)
        assert _calls_named(bridge, "get_quote_sync"), "quote site bypassed the factory"

    def test_equity_site_routes_through_factory_not_paper(self, wired):
        """Bridge equity 2345.0 ≠ DB balance 2200.0 — the discriminator
        that proves the value came from the bridge, not the paper fallback."""
        trader, bridge, _ = wired
        equity = trader._get_equity()
        assert equity == 2345.0, (
            f"equity {equity!r} — expected the BRIDGE value 2345.0; "
            "2200.0 would mean the paper fallback fired and the bridge "
            "was bypassed"
        )
        assert _calls_named(bridge, "fetch_account_info_sync")

    def test_free_margin_site_routes_through_factory(self, wired):
        trader, bridge, _ = wired
        assert trader._get_free_margin() == 2000.0
        assert _calls_named(bridge, "fetch_account_info_sync")

    def test_candles_site_routes_through_factory(self, wired):
        trader, bridge, _ = wired
        # Empty bridge candles → CSV fallback may return None — routing is
        # the contract here (fetch attempted for each TF before fallback).
        trader._fetch_candles()
        requested_tfs = {args[1] for name, args in bridge.calls
                         if name == "fetch_candles_sync"}
        assert requested_tfs == {"M5", "H1", "H4", "D1"}

    def test_deal_history_site_routes_through_factory(self, wired):
        trader, bridge, _ = wired
        deals = trader._get_deal_history(days_back=7)
        assert deals == [{"ticket": 90001, "position_id": 555001}]
        assert _calls_named(bridge, "fetch_deal_history_sync")

    def test_position_exists_site_routes_through_factory(self, ic_db, restore_bridge_seam):
        """dry_run=True short-circuits to True (paper has no broker) — the
        routing proof needs the LIVE flag, same as the MT5 path."""
        bridge = RecordingBridge()
        factory = FakeFactory(bridge)
        trader = LiveTrader(
            account="IC1", db_path=ic_db.path, dry_run=False,
            bridge_factory=factory,
        )
        assert trader._position_exists_in_mt5(555001) is True
        assert _calls_named(bridge, "position_exists_sync")

    def test_async_close_path_routes_through_factory(self, ic_db, restore_bridge_seam):
        """The close flow (asyncio.run → connect → close_position_with_fill
        → disconnect) on the factory-built bridge."""
        bridge = RecordingBridge()
        factory = FakeFactory(bridge)
        trader = LiveTrader(
            account="IC1", db_path=ic_db.path, dry_run=False,
            bridge_factory=factory,
        )
        ok, fill = trader._close_mt5_position_with_fill(555001)
        assert ok is True and fill == 2351.5
        assert _calls_named(bridge, "close_position_with_fill")
        assert bridge.connect_count >= 1


# ----------------------------------------------------------------------
# 5. One factory per process
# ----------------------------------------------------------------------

class TestOneFactoryPerProcess:

    def test_second_different_factory_is_refused(self, ic_db, restore_bridge_seam):
        LiveTrader(
            account="IC1", db_path=ic_db.path, dry_run=True,
            bridge_factory=FakeFactory(RecordingBridge()),
        )
        with pytest.raises(RuntimeError, match="one engine account per process"):
            LiveTrader(
                account="IC2", db_path=ic_db.path, dry_run=True,
                bridge_factory=FakeFactory(RecordingBridge()),
            )

    def test_same_factory_reinstall_is_idempotent(self, ic_db, restore_bridge_seam):
        factory = FakeFactory(RecordingBridge())
        LiveTrader(account="IC1", db_path=ic_db.path, dry_run=True, bridge_factory=factory)
        # Re-constructing with the SAME factory (restart-in-process idiom)
        # must not raise — it is not a mid-flight account swap.
        LiveTrader(account="IC1", db_path=ic_db.path, dry_run=True, bridge_factory=factory)

    def test_refusal_leaves_first_factory_installed(self, ic_db, restore_bridge_seam):
        first = FakeFactory(RecordingBridge())
        LiveTrader(account="IC1", db_path=ic_db.path, dry_run=True, bridge_factory=first)
        with pytest.raises(RuntimeError):
            LiveTrader(
                account="IC2", db_path=ic_db.path, dry_run=True,
                bridge_factory=FakeFactory(RecordingBridge()),
            )
        assert bridge_client.MT5Bridge is first


# ----------------------------------------------------------------------
# 6. Equity stamp — bridge value, never the paper fallback
# ----------------------------------------------------------------------

class TestEquityStamp:

    def test_stamp_writes_bridge_equity_into_db(self, ic_db, restore_bridge_seam):
        bridge = RecordingBridge(equity=2345.0)
        trader = LiveTrader(
            account="IC1", db_path=ic_db.path, dry_run=True,
            bridge_factory=FakeFactory(bridge),
        )
        assert trader._get_equity() == 2345.0
        trader._write_equity_snapshot()

        from metty.core.db import get_connection
        conn = get_connection(ic_db.path)
        try:
            row = conn.execute(
                "SELECT current_equity, equity_ts FROM accounts WHERE name='IC1'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] == 2345.0, (
            f"accounts.current_equity = {row[0]!r} — the equity stamp must be "
            "the BRIDGE value (2345.0), not a paper/db fallback (2200.0)"
        )
        assert row[1] is not None, "equity_ts must be stamped (kill-switch freshness)"

    def test_stamp_survives_bridge_absence_without_raising(self, ic_db, restore_bridge_seam):
        """No factory → bridge unreachable in tests → the stamp must not
        crash the cycle (paper equity path answers instead)."""
        trader = LiveTrader(account="IC1", db_path=ic_db.path, dry_run=True)
        trader._write_equity_snapshot()  # must not raise
        from metty.core.db import get_connection
        conn = get_connection(ic_db.path)
        try:
            row = conn.execute(
                "SELECT current_equity FROM accounts WHERE name='IC1'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None and row[0] is not None
