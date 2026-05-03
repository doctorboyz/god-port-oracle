"""RPyC bridge server running in Wine Python.

Exposes MetaTrader5 module via RPyC for remote access from macOS.
Returns dict/list instead of namedtuple/numpy for RPyC serialization.
"""
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

    def exposed_copy_ticks_from(self, symbol, date_from, count, flags):
        import MetaTrader5 as mt5
        return _numpy_to_list(mt5.copy_ticks_from(symbol, date_from, count, flags))

    def exposed_order_send(self, request):
        import MetaTrader5 as mt5
        # RPyC passes dicts, but mt5.order_send() needs a proper request object.
        # Construct the request inside Wine Python where MT5 types are available.
        req = {
            'action': request.get('action', mt5.TRADE_ACTION_DEAL),
            'symbol': request['symbol'],
            'volume': request.get('volume', 0.01),
            'type': request.get('type', mt5.ORDER_TYPE_BUY),
            'price': request.get('price', 0.0),
            'sl': request.get('sl', 0.0),
            'tp': request.get('tp', 0.0),
            'deviation': request.get('deviation', 20),
            'magic': request.get('magic', 0),
            'comment': request.get('comment', ''),
            'type_time': request.get('type_time', mt5.ORDER_TIME_GTC),
            'type_filling': request.get('type_filling', mt5.ORDER_FILLING_IOC),
        }
        result = mt5.order_send(req)
        return _to_dict(result)

    def exposed_order_send_simple(self, symbol, order_type, volume, price,
                                    sl=0.0, tp=0.0, deviation=20,
                                    magic=0, comment=''):
        """Simplified order_send that constructs the request internally."""
        import MetaTrader5 as mt5
        request = {
            'action': mt5.TRADE_ACTION_DEAL,
            'symbol': symbol,
            'volume': volume,
            'type': order_type,
            'price': price,
            'sl': sl,
            'tp': tp,
            'deviation': deviation,
            'magic': magic,
            'comment': comment,
            'type_time': mt5.ORDER_TIME_GTC,
            'type_filling': mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        return _to_dict(result)

    def exposed_positions_get(self, symbol=None, ticket=None):
        import MetaTrader5 as mt5
        if symbol:
            return _to_list_of_dicts(mt5.positions_get(symbol=symbol))
        elif ticket:
            return _to_list_of_dicts(mt5.positions_get(ticket=ticket))
        return _to_list_of_dicts(mt5.positions_get())

    def exposed_history_orders_get(self, **kwargs):
        import MetaTrader5 as mt5
        return _to_list_of_dicts(mt5.history_orders_get(**kwargs)) if kwargs else _to_list_of_dicts(mt5.history_orders_get())

    def exposed_history_deals_get(self, **kwargs):
        import MetaTrader5 as mt5
        return _to_list_of_dicts(mt5.history_deals_get(**kwargs)) if kwargs else _to_list_of_dicts(mt5.history_deals_get())

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