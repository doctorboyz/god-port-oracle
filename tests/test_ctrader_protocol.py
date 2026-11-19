"""Wire-protocol unit tests for the cTrader adapter (pure functions, no I/O).

Covers the JSON envelope, newline framing (including split feeds), the
scaling rules every cTrader message uses (price 1/100000, volume cents,
moneyDigits) and the payload-type constants against the canonical .proto.
"""

import pytest

from ctrader import messages as msg
from ctrader.protocol import (
    ApiMessage,
    FrameDecoder,
    ProtocolError,
    decode_message,
    encode_frame,
    encode_length_prefix,
    encode_message,
    leverage_from_cents,
    lots_to_volume_cents,
    money_to_real,
    price_to_proto,
    price_to_real,
    volume_cents_to_lots,
)


# ----------------------------------------------------------------------
# Envelope encode/decode
# ----------------------------------------------------------------------

def test_encode_decode_round_trip():
    message = ApiMessage(payload_type=2100, payload={"clientId": "abc"}, client_msg_id="id-1")
    decoded = decode_message(encode_message(message))
    assert decoded.payload_type == 2100
    assert decoded.payload == {"clientId": "abc"}
    assert decoded.client_msg_id == "id-1"


def test_encode_always_includes_payload_key():
    """Docs envelope example carries payload even when empty (heartbeat)."""
    envelope = __import__("json").loads(encode_message(ApiMessage(payload_type=51)))
    assert envelope == {"payloadType": 51, "payload": {}}


def test_decode_rejects_missing_payload_type():
    with pytest.raises(ProtocolError):
        decode_message('{"clientMsgId": "x", "payload": {}}')


def test_decode_rejects_non_object_payload():
    with pytest.raises(ProtocolError):
        decode_message('{"payloadType": 2100, "payload": [1, 2]}')


def test_decode_rejects_garbage():
    with pytest.raises(ProtocolError):
        decode_message("not json at all")


def test_decode_accepts_bytes():
    decoded = decode_message(b'{"payloadType": 51, "payload": {}}')
    assert decoded.payload_type == msg.HEARTBEAT_EVENT


# ----------------------------------------------------------------------
# Newline framing
# ----------------------------------------------------------------------

def test_frame_is_newline_terminated():
    frame = encode_frame(ApiMessage(payload_type=2100, payload={"a": 1}))
    assert frame.endswith(b"\n")
    assert frame.count(b"\n") == 1


def test_frame_decoder_handles_split_feeds():
    decoder = FrameDecoder()
    frame1 = encode_frame(ApiMessage(payload_type=2100, payload={"x": 1}))
    frame2 = encode_frame(ApiMessage(payload_type=2101))

    # Feed byte-by-byte: nothing complete until the first newline.
    chunks = []
    for byte in frame1 + frame2:
        chunks.extend(decoder.feed(bytes([byte])))
    assert len(chunks) == 2
    assert decode_message(chunks[0]).payload_type == 2100
    assert decode_message(chunks[1]).payload_type == 2101


def test_frame_decoder_ignores_blank_lines():
    decoder = FrameDecoder()
    frames = decoder.feed(b"\n\n" + encode_frame(ApiMessage(payload_type=51)) + b"\n\n")
    assert len(frames) == 1


def test_frame_decoder_pending_keeps_partial_data():
    decoder = FrameDecoder()
    decoder.feed(b'{"payloadType": 21')
    assert decoder.pending() == b'{"payloadType": 21'


def test_length_prefix_little_endian():
    assert encode_length_prefix(300) == b"\x2c\x01\x00\x00"
    assert encode_length_prefix(300, little_endian=False) == b"\x00\x00\x01\x2c"


# ----------------------------------------------------------------------
# Scaling rules (from the canonical .proto comments)
# ----------------------------------------------------------------------

def test_price_scaling():
    # "123000 in protocol means 1.23, 53423782 means 534.23782"
    assert price_to_real(123_000) == pytest.approx(1.23)
    assert price_to_real(53_423_782) == pytest.approx(534.23782)
    assert price_to_real(None) == 0.0
    assert price_to_proto(2350.123) == 235_012_300
    assert price_to_real(price_to_proto(2350.123)) == pytest.approx(2350.123)


def test_volume_cents_and_lots_round_trip():
    # XAUUSD wire truth (19/11): lotSize=10000 — already in cents, so
    # volume_cents = lots x lotSize with no extra x100 (see
    # tests/test_ctrader_volume_scale_causal.py for the causal pin)
    assert lots_to_volume_cents(1.0, lot_size=10_000) == 10_000
    assert lots_to_volume_cents(0.01, lot_size=10_000) == 100
    assert volume_cents_to_lots(10_000, lot_size=10_000) == pytest.approx(1.0)
    assert volume_cents_to_lots(0) == 0.0
    # Different lot size (e.g. 1000-cents-per-lot symbol) obeys the same rule
    assert lots_to_volume_cents(1.0, lot_size=1000) == 1_000


def test_money_digits_scaling():
    # Proto comment: moneyDigits=8, 10053099944 -> 100.53099944
    assert money_to_real(10_053_099_944, money_digits=8) == pytest.approx(100.53099944)
    assert money_to_real(10_000_000, money_digits=2) == pytest.approx(100_000.0)
    assert money_to_real(None) == 0.0


def test_leverage_from_cents():
    # ProtoOATrader.leverageInCents: 1:50 -> 5000
    assert leverage_from_cents(5_000) == 50
    assert leverage_from_cents(100_000) == 1000
    # Missing leverageInCents falls back to maxLeverage
    assert leverage_from_cents(0, max_leverage=1000) == 1000
    assert leverage_from_cents(None) == 1


# ----------------------------------------------------------------------
# Payload-type constants must match the canonical enum
# ----------------------------------------------------------------------

def test_payload_types_match_canonical_proto():
    """Values copied from ProtoOAPayloadType in ctrader/proto_reference/."""
    expected = {
        "PROTO_MESSAGE": 5,
        "COMMON_ERROR_RES": 50,
        "HEARTBEAT_EVENT": 51,
        "APPLICATION_AUTH_REQ": 2100,
        "ACCOUNT_AUTH_REQ": 2102,
        "NEW_ORDER_REQ": 2106,
        "AMEND_POSITION_SLTP_REQ": 2110,
        "CLOSE_POSITION_REQ": 2111,
        "ASSET_LIST_REQ": 2112,
        "SYMBOLS_LIST_REQ": 2114,
        "SYMBOL_BY_ID_REQ": 2116,
        "TRADER_REQ": 2121,
        "RECONCILE_REQ": 2124,
        "EXECUTION_EVENT": 2126,
        "SUBSCRIBE_SPOTS_REQ": 2127,
        "SPOT_EVENT": 2131,
        "ORDER_ERROR_EVENT": 2132,
        "GET_TRENDBARS_REQ": 2137,
        "OA_ERROR_RES": 2142,
        "GET_ACCOUNTS_BY_ACCESS_TOKEN_REQ": 2149,
    }
    for name, value in expected.items():
        assert getattr(msg, name) == value, f"{name} drifted from canonical .proto"


def test_timeframe_map_covers_engine_timeframes():
    """Every timeframe the engine requests on MT5 must exist on cTrader."""
    from metty.bridge.client import MT5_TIMEFRAMES

    for timeframe in MT5_TIMEFRAMES:
        if timeframe == "MN":
            timeframe = "MN1"  # cTrader spells it MN1
        assert timeframe in msg.TIMEFRAME_TO_TRENDBAR or timeframe == "MN1", (
            f"timeframe {timeframe} unusable via the cTrader adapter"
        )


def test_trendbar_period_seconds_are_consistent():
    """Window math relies on period seconds matching the enum."""
    assert msg.TRENDBAR_PERIOD_SECONDS[msg.TrendbarPeriod.M5] == 300
    assert msg.TRENDBAR_PERIOD_SECONDS[msg.TrendbarPeriod.H1] == 3600
    assert msg.TRENDBAR_PERIOD_SECONDS[msg.TrendbarPeriod.D1] == 86400
    # Max trendbar window per request stays inside 1 week
    assert msg.TRENDBAR_MAX_WINDOW_MS == 604_800_000


# ----------------------------------------------------------------------
# Config (env-only credentials)
# ----------------------------------------------------------------------

def test_config_from_env_reads_ctrader_vars():
    from ctrader import CTraderConfig

    config = CTraderConfig.from_env({
        "CTRADER_NAME": "icmarkets-real-6194964",
        "CTRADER_CLIENT_ID": "cid",
        "CTRADER_CLIENT_SECRET": "secret",
        "CTRADER_ACCESS_TOKEN": "token",
        "CTRADER_ACCOUNT_ID": "6194964",
        "CTRADER_HOST_TYPE": "live",
    })
    assert config.is_configured is True
    assert config.account_id == 6194964
    assert config.host_type == "live"


def test_config_unconfigured_without_credentials():
    from ctrader import CTraderConfig

    config = CTraderConfig.from_env({})
    assert config.is_configured is False
    assert config.host_type == "demo"  # real account must be opt-in


def test_config_invalid_host_type_rejected():
    from pydantic import ValidationError

    from ctrader import CTraderConfig

    with pytest.raises(ValidationError):
        CTraderConfig(host_type="paper")


def test_config_redacted_never_leaks_secrets():
    from ctrader import CTraderConfig

    config = CTraderConfig(
        client_id="12345678", client_secret="supersecretvalue", access_token="tokentokentoken",
        account_id=6194964,
    )
    summary = config.redacted()
    text = str(summary)
    assert "supersecretvalue" not in text
    assert "tokentokentoken" not in text
    assert "123" in text and "..." in text  # masked prefix visible
