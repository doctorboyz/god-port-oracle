"""RPyC bridge server running in Wine Python.

Exposes MetaTrader5 module via RPyC for remote access from macOS.
Returns JSON-serialized strings instead of netref dicts to avoid
slow per-key RPyC round-trips on large data sets.
"""
import json
import sys
import rpyc
from rpyc.utils.server import ThreadedServer


def _to_dict(obj):
    """Convert namedtuple or object to dict for RPyC serialization."""
    if obj is None:
        return None
    if hasattr(obj, '_asdict'):
        return dict(obj._asdict())
    if hasattr(obj, '__dict__'):
        return {k: v for k, v in obj.__dict__.items() if not k.startswith('_')}
    return str(obj)


def _to_list_of_dicts(objs):
    """Convert list of namedtuples to list of dicts."""
    if objs is None:
        return None
    return [_to_dict(o) for o in objs]


def _numpy_to_list(arr):
    """Convert numpy structured array or ndarray to list of dicts or list."""
    if arr is None:
        return None
    import numpy as np
    if isinstance(arr, np.ndarray):
        if arr.dtype.names:
            return [dict(zip(arr.dtype.names, row)) for row in arr]
        return arr.tolist()
    return str(arr)


def _numpy_to_json(arr):
    """Convert numpy structured array to JSON string for efficient transfer.

    RPyC netref dicts require a round-trip per key access, making large
    datasets (500+ candles) extremely slow. Returning JSON string avoids this.
    """
    if arr is None:
        return "[]"
    import numpy as np
    if isinstance(arr, np.ndarray):
        if arr.dtype.names:
            result = [dict(zip(arr.dtype.names, row)) for row in arr]
            return json.dumps(result, default=float)
        return json.dumps(arr.tolist(), default=float)
    return "[]"


class MT5Service(rpyc.Service):
    """RPyC service exposing MetaTrader5 functions."""

    def exposed_initialize(self):
        import MetaTrader5 as mt5
        return mt5.initialize()

    def exposed_login(self, login, password, server):
        import MetaTrader5 as mt5
        return mt5.login(int(login), password, server)

    def exposed_shutdown(self):
        import MetaTrader5 as mt5
        return mt5.shutdown()

    def exposed_account_info(self):
        import MetaTrader5 as mt5
        return _to_dict(mt5.account_info())

    def exposed_symbol_info(self, symbol):
        import MetaTrader5 as mt5
        return _to_dict(mt5.symbol_info(symbol))

    def exposed_symbol_info_tick(self, symbol):
        import MetaTrader5 as mt5
        return _to_dict(mt5.symbol_info_tick(symbol))

    def exposed_symbol_select(self, symbol, enable=True):
        import MetaTrader5 as mt5
        return mt5.symbol_select(symbol, enable)

    def exposed_copy_rates_from(self, symbol, timeframe, date_from, count):
        import MetaTrader5 as mt5
        return _numpy_to_list(mt5.copy_rates_from(symbol, timeframe, date_from, count))

    def exposed_copy_rates_from_pos(self, symbol, timeframe, start_pos, count):
        import MetaTrader5 as mt5
        return _numpy_to_list(mt5.copy_rates_from_pos(symbol, timeframe, start_pos, count))

    def exposed_copy_rates_from_pos_json(self, symbol, timeframe, start_pos, count):
        """Same as copy_rates_from_pos but returns JSON string for efficient transfer."""
        import MetaTrader5 as mt5
        return _numpy_to_json(mt5.copy_rates_from_pos(symbol, timeframe, start_pos, count))

    def exposed_copy_ticks_from(self, symbol, date_from, count, flags):
        import MetaTrader5 as mt5
        return _numpy_to_list(mt5.copy_ticks_from(symbol, date_from, count, flags))

    def exposed_order_send(self, action, symbol, volume, order_type, price,
                           sl=0.0, tp=0.0, deviation=20, magic=0,
                           comment='', type_time=0, type_filling=2,
                           position=0):
        """Send a trade order. All params passed as arguments (not dict) to avoid RPyC netref issues.

        Args:
            action: TRADE_ACTION_DEAL=1, TRADE_ACTION_PENDING=5, etc.
            symbol: e.g. 'XAUUSDm'
            volume: lot size (0.01 minimum)
            order_type: ORDER_TYPE_BUY=0, ORDER_TYPE_SELL=1
            price: order price
            sl: stop loss (0 = none)
            tp: take profit (0 = none)
            deviation: max slippage in points
            magic: magic number
            comment: order comment
            type_time: ORDER_TIME_GTC=0, ORDER_TIME_DAY=1
            type_filling: ORDER_FILLING_IOC=2, ORDER_FILLING_FOK=0, ORDER_FILLING_RETURN=1
            position: ticket for closing (0 = new order)
        """
        import MetaTrader5 as mt5
        request = {
            'action': action,
            'symbol': symbol,
            'volume': volume,
            'type': order_type,
            'price': price,
            'sl': sl,
            'tp': tp,
            'deviation': deviation,
            'magic': magic,
            'comment': comment,
            'type_time': type_time,
            'type_filling': type_filling,
        }
        if position:
            request['position'] = position
        result = mt5.order_send(request)
        return _to_dict(result)

    def exposed_positions_get(self, symbol=None, ticket=None):
        import MetaTrader5 as mt5
        if symbol:
            return _to_list_of_dicts(mt5.positions_get(symbol=symbol))
        elif ticket:
            return _to_list_of_dicts(mt5.positions_get(ticket=ticket))
        return _to_list_of_dicts(mt5.positions_get())

    def exposed_history_orders_get(self, date_from=None, date_to=None, **kwargs):
        """Get historical orders from MT5.

        Args:
            date_from: Unix timestamp (int) or datetime object for start date.
            date_to: Unix timestamp (int) or datetime object for end date.

        RPyC cannot serialize datetime objects reliably, so we accept Unix
        timestamps (integers) and convert them to datetime inside the bridge.
        """
        import MetaTrader5 as mt5
        from datetime import datetime, timezone

        # Convert Unix timestamps to datetime if needed
        if isinstance(date_from, (int, float)):
            date_from = datetime.fromtimestamp(date_from, tz=timezone.utc)
        if isinstance(date_to, (int, float)):
            date_to = datetime.fromtimestamp(date_to, tz=timezone.utc)

        if date_from is not None and date_to is not None:
            return _to_list_of_dicts(mt5.history_orders_get(date_from, date_to))
        if kwargs:
            return _to_list_of_dicts(mt5.history_orders_get(**kwargs))
        return _to_list_of_dicts(mt5.history_orders_get())

    def exposed_history_deals_get(self, date_from=None, date_to=None, **kwargs):
        """Get historical deals from MT5.

        Args:
            date_from: Unix timestamp (int) or datetime object for start date.
            date_to: Unix timestamp (int) or datetime object for end date.

        RPyC cannot serialize datetime objects reliably, so we accept Unix
        timestamps (integers) and convert them to datetime inside the bridge.
        """
        import MetaTrader5 as mt5
        from datetime import datetime, timezone

        # Convert Unix timestamps to datetime if needed
        if isinstance(date_from, (int, float)):
            date_from = datetime.fromtimestamp(date_from, tz=timezone.utc)
        if isinstance(date_to, (int, float)):
            date_to = datetime.fromtimestamp(date_to, tz=timezone.utc)

        if date_from is not None and date_to is not None:
            return _to_list_of_dicts(mt5.history_deals_get(date_from, date_to))
        if kwargs:
            return _to_list_of_dicts(mt5.history_deals_get(**kwargs))
        return _to_list_of_dicts(mt5.history_deals_get())

    def exposed_version(self):
        import MetaTrader5 as mt5
        return mt5.version()

    def exposed_last_error(self):
        import MetaTrader5 as mt5
        return mt5.last_error()

    def exposed_symbols_total(self):
        import MetaTrader5 as mt5
        return mt5.symbols_total()

    def exposed_symbols_get(self, symbol=None):
        import MetaTrader5 as mt5
        if symbol:
            return _to_list_of_dicts(mt5.symbols_get(symbol))
        return _to_list_of_dicts(mt5.symbols_get())


if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8001
    print(f'MT5 Bridge RPyC server starting on port {port}...')
    t = ThreadedServer(MT5Service, hostname='0.0.0.0', port=port)
    t.start()