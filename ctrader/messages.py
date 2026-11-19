"""cTrader Open API payload types, enums and message names.

Every value below is copied from the canonical protobuf definitions
(spotware/openapi-proto-messages, MIT license — reference copy kept in
ctrader/proto_reference/). Do NOT invent or shift these numbers: they are
the wire contract, and the JSON transport addresses messages by these IDs.
"""

# --- Common payload types (OpenApiCommonModelMessages.proto) ---
PROTO_MESSAGE = 5
COMMON_ERROR_RES = 50
HEARTBEAT_EVENT = 51

# --- Open API payload types (OpenApiModelMessages.proto enum ProtoOAPayloadType) ---
APPLICATION_AUTH_REQ = 2100
APPLICATION_AUTH_RES = 2101
ACCOUNT_AUTH_REQ = 2102
ACCOUNT_AUTH_RES = 2103
VERSION_REQ = 2104
VERSION_RES = 2105
NEW_ORDER_REQ = 2106
CANCEL_ORDER_REQ = 2108
AMEND_ORDER_REQ = 2109
AMEND_POSITION_SLTP_REQ = 2110
CLOSE_POSITION_REQ = 2111
ASSET_LIST_REQ = 2112
ASSET_LIST_RES = 2113
SYMBOLS_LIST_REQ = 2114
SYMBOLS_LIST_RES = 2115
SYMBOL_BY_ID_REQ = 2116
SYMBOL_BY_ID_RES = 2117
TRADER_REQ = 2121
TRADER_RES = 2122
TRADER_UPDATE_EVENT = 2123
RECONCILE_REQ = 2124
RECONCILE_RES = 2125
DEAL_LIST_REQ = 2133
DEAL_LIST_RES = 2134
EXECUTION_EVENT = 2126
SUBSCRIBE_SPOTS_REQ = 2127
SUBSCRIBE_SPOTS_RES = 2128
UNSUBSCRIBE_SPOTS_REQ = 2129
UNSUBSCRIBE_SPOTS_RES = 2130
SPOT_EVENT = 2131
ORDER_ERROR_EVENT = 2132
GET_TRENDBARS_REQ = 2137
GET_TRENDBARS_RES = 2138
OA_ERROR_RES = 2142
GET_TICKDATA_REQ = 2145
GET_TICKDATA_RES = 2146
CLIENT_DISCONNECT_EVENT = 2148
GET_ACCOUNTS_BY_ACCESS_TOKEN_REQ = 2149
GET_ACCOUNTS_BY_ACCESS_TOKEN_RES = 2150
ACCOUNT_LOGOUT_REQ = 2162
ACCOUNT_LOGOUT_RES = 2163
ACCOUNT_DISCONNECT_EVENT = 2164


class TradeSide:
    """ProtoOATradeSide enum."""

    BUY = 1
    SELL = 2


class OrderType:
    """ProtoOAOrderType enum."""

    MARKET = 1
    LIMIT = 2
    STOP = 3
    STOP_LOSS_TAKE_PROFIT = 4
    MARKET_RANGE = 5
    STOP_LIMIT = 6


class ExecutionType:
    """ProtoOAExecutionType enum — ProtoOAExecutionEvent.executionType."""

    ORDER_ACCEPTED = 2
    ORDER_FILLED = 3
    ORDER_REPLACED = 4
    ORDER_CANCELLED = 5
    ORDER_EXPIRED = 6
    ORDER_REJECTED = 7
    ORDER_CANCEL_REJECTED = 8
    SWAP = 9
    DEPOSIT_WITHDRAW = 10
    ORDER_PARTIAL_FILL = 11
    BONUS_DEPOSIT_WITHDRAW = 12


class PositionStatus:
    """ProtoOAPositionStatus enum."""

    POSITION_STATUS_OPEN = 1
    POSITION_STATUS_CLOSED = 2
    POSITION_STATUS_CREATED = 3


class DealStatus:
    """ProtoOADealStatus enum (subset used for fills)."""

    FILLED = 2


class TrendbarPeriod:
    """ProtoOATrendbarPeriod enum — ProtoOAGetTrendbarsReq.period."""

    M1 = 1
    M2 = 2
    M3 = 3
    M4 = 4
    M5 = 5
    M10 = 6
    M15 = 7
    M30 = 8
    H1 = 9
    H4 = 10
    H12 = 11
    D1 = 12
    W1 = 13
    MN1 = 14


class CommonErrorCode:
    """ProtoErrorCode enum (common, 1-99) — subset relevant to us."""

    UNKNOWN_ERROR = 1
    UNSUPPORTED_MESSAGE = 2
    INVALID_REQUEST = 3
    TIMEOUT_ERROR = 5
    ENTITY_NOT_FOUND = 6
    CANT_ROUTE_REQUEST = 7
    FRAME_TOO_LONG = 8
    MARKET_CLOSED = 9
    CONCURRENT_MODIFICATION = 10
    BLOCKED_PAYLOAD_TYPE = 11


# Engine timeframe string -> ProtoOATrendbarPeriod value.
# Mirrors MT5_TIMEFRAMES coverage in metty/bridge/client.py (M1..D1 + W1/MN).
TIMEFRAME_TO_TRENDBAR = {
    "M1": TrendbarPeriod.M1,
    "M5": TrendbarPeriod.M5,
    "M15": TrendbarPeriod.M15,
    "M30": TrendbarPeriod.M30,
    "H1": TrendbarPeriod.H1,
    "H4": TrendbarPeriod.H4,
    "D1": TrendbarPeriod.D1,
    "W1": TrendbarPeriod.W1,
    "MN": TrendbarPeriod.MN1,
}

# TrendbarPeriod value -> bar duration in seconds (for window math).
TRENDBAR_PERIOD_SECONDS = {
    TrendbarPeriod.M1: 60,
    TrendbarPeriod.M5: 300,
    TrendbarPeriod.M15: 900,
    TrendbarPeriod.M30: 1800,
    TrendbarPeriod.H1: 3600,
    TrendbarPeriod.H4: 14400,
    TrendbarPeriod.H12: 43200,
    TrendbarPeriod.D1: 86400,
    TrendbarPeriod.W1: 604800,
    TrendbarPeriod.MN1: 2592000,  # nominal 30 days
}

# Fallback window per ProtoOAGetTrendbarsReq request (1 week). The
# official message reference documents NO span cap for trendbars (Unix
# bounds only) plus an optional count field — the primary fetch path
# asks for the full span with count in one request. 1 week is the
# documented cap for historical TICK data; we keep it as the fallback
# window for backends that reject wide trendbar spans (D1 fix 20/11).
TRENDBAR_MAX_WINDOW_MS = 604_800_000

# Consecutive empty weekly windows before the fallback declares the
# start of history and stops paging backward. 2 tolerates one odd
# empty week without chasing history the server does not have —
# pre-fix, D1 x 500 on a ~130-day backend walked ~53 empty windows
# (~72 requests total) and died at the sync timeout with zero bars.
TRENDBAR_EMPTY_WINDOW_LIMIT = 2

# Historical data rate limit: 5 requests/second per connection (official docs).
HISTORICAL_REQUESTS_PER_SECOND = 5
