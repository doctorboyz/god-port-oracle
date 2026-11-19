"""Causal tests for cTrader wire framing — the server sends bare JSON objects.

Evidence (19/11, vpsdeluna raw-socket probe — see the job brief and
ctrader/README.md): every frame from both demo.ctraderapi.com:5036 and
live.ctraderapi.com:5036 is a bare JSON object with NO delimiter and NO
trailing newline —

    b'{"payloadType":2101,"clientMsgId":"probe-1"}'   (44 bytes, then silence)

The old FrameDecoder split frames on newline only, so a real server
response never completed a frame -> CTraderBridge._await_response()
timed out on every message -> connect() always failed. These tests pin
the incremental raw_decode framing that fixed it: exact byte evidence
replayed below, no network, synthetic credentials only (the real 2150
echoes the accessToken — never copy real tokens into fixtures).
"""

import pytest

from ctrader.protocol import (
    ApiMessage,
    FrameDecoder,
    ProtocolError,
    decode_message,
    encode_frame,
)

# Byte-exact replay of the 19/11 probe: 2101 responses carried no newline.
APP_AUTH_RES = b'{"payloadType":2101,"clientMsgId":"probe-1"}'
# Conversation probe: 2101 for "a1" (39 bytes) then 2150 for "b1" (535 bytes
# on the wire; synthetic accessToken, two accounts) back to back, no delimiter.
ACCT_LIST_RES_A = b'{"payloadType":2101,"clientMsgId":"a1"}'
ACCT_LIST_RES_B = (
    b'{"payloadType":2150,"clientMsgId":"b1","payload":{"accessToken":"synthetic",'
    b'"ctidTraderAccount":[{"ctidTraderAccountId":49596566,"traderLogin":6194964},'
    b'{"ctidTraderAccountId":49609239,"traderLogin":10138643}]}}'
)


# ----------------------------------------------------------------------
# (a) One bare object in one chunk — the exact wire case that hung the bridge
# ----------------------------------------------------------------------

def test_single_bare_object_without_newline_is_one_frame():
    decoder = FrameDecoder()
    frames = decoder.feed(APP_AUTH_RES)
    assert len(frames) == 1
    assert frames[0] == APP_AUTH_RES
    assert decode_message(frames[0]).payload_type == 2101
    assert decoder.pending() == b""


def test_wire_replay_two_objects_arrive_in_one_chunk():
    """Conversation probe: 2101 then 2150 concatenated, no separator."""
    decoder = FrameDecoder()
    frames = decoder.feed(ACCT_LIST_RES_A + ACCT_LIST_RES_B)
    assert len(frames) == 2
    first, second = decode_message(frames[0]), decode_message(frames[1])
    assert first.payload_type == 2101
    assert second.payload_type == 2150
    assert second.payload["ctidTraderAccount"][0]["traderLogin"] == 6194964
    assert decoder.pending() == b""


# ----------------------------------------------------------------------
# (b) Two objects touching at }{ inside one chunk
# ----------------------------------------------------------------------

def test_two_objects_back_to_back_in_one_chunk():
    decoder = FrameDecoder()
    frames = decoder.feed(b'{"payloadType":2100,"payload":{"a":1}}{"payloadType":2101}')
    assert [decode_message(f).payload_type for f in frames] == [2100, 2101]
    assert decoder.pending() == b""


# ----------------------------------------------------------------------
# (c) One object split across chunks — mid-key, mid-number, mid-string
# ----------------------------------------------------------------------

def test_object_split_across_chunks_mid_key():
    decoder = FrameDecoder()
    head = b'{"payl'
    tail = b'oadType":2101,"clientMsgId":"x"}'
    assert decoder.feed(head) == []
    assert decoder.pending() == head
    frames = decoder.feed(tail)
    assert len(frames) == 1
    assert decode_message(frames[0]).payload_type == 2101


def test_object_split_across_chunks_mid_number():
    decoder = FrameDecoder()
    head = b'{"payloadType": 21'
    assert decoder.feed(head) == []
    frames = decoder.feed(b'01,"payload":{}}')
    assert len(frames) == 1
    assert decode_message(frames[0]).payload_type == 2101


def test_object_split_across_chunks_mid_string():
    decoder = FrameDecoder()
    head = b'{"payloadType":2100,"payload":{"comment":"god-p'
    assert decoder.feed(head) == []
    frames = decoder.feed(b'ort-demo"}}')
    assert len(frames) == 1
    assert decode_message(frames[0]).payload["comment"] == "god-port-demo"


def test_object_split_byte_by_byte():
    """Worst-case chunking: one byte per feed must still reassemble."""
    decoder = FrameDecoder()
    raw = APP_AUTH_RES + ACCT_LIST_RES_B
    frames: list[bytes] = []
    for byte in raw:
        frames.extend(decoder.feed(bytes([byte])))
    assert len(frames) == 2
    assert frames[0] == APP_AUTH_RES
    assert decode_message(frames[1]).payload_type == 2150
    assert decoder.pending() == b""


# ----------------------------------------------------------------------
# (d) Newline inside a string value must never cut an object
# ----------------------------------------------------------------------

def test_escaped_newline_inside_string_value_keeps_object_whole():
    raw = b'{"payloadType":2100,"payload":{"comment":"line1\\nline2"}}'
    decoder = FrameDecoder()
    frames = decoder.feed(raw)
    assert len(frames) == 1
    assert decode_message(frames[0]).payload["comment"] == "line1\nline2"


# ----------------------------------------------------------------------
# (e)/(f) Legacy newline-terminated frames still work
# ----------------------------------------------------------------------

def test_newline_terminated_frames_still_decode():
    decoder = FrameDecoder()
    frame = encode_frame(ApiMessage(payload_type=2100, payload={"x": 1}))
    assert decoder.feed(frame) == [frame.rstrip(b"\n")]


def test_trailing_newline_after_bare_object_is_consumed():
    decoder = FrameDecoder()
    frames = decoder.feed(APP_AUTH_RES + b"\n")
    assert frames == [APP_AUTH_RES]
    assert decoder.pending() == b""


def test_whitespace_between_objects_is_skipped():
    decoder = FrameDecoder()
    frames = decoder.feed(b"\n  " + APP_AUTH_RES + b"\n\n" + ACCT_LIST_RES_A + b"  ")
    assert [decode_message(f).payload_type for f in frames] == [2101, 2101]
    assert decoder.pending() == b""


# ----------------------------------------------------------------------
# (g) Incomplete tail stays buffered for the next feed
# ----------------------------------------------------------------------

def test_incomplete_tail_is_kept_for_next_feed():
    decoder = FrameDecoder()
    assert decoder.feed(APP_AUTH_RES) != []
    tail = b'{"payloadType":2150,"payl'
    assert decoder.feed(tail) == []
    assert decoder.pending() == tail  # bare object consumed, tail kept verbatim
    frames = decoder.feed(b'oadType":2150,"payload":{}}')
    assert len(frames) == 1
    assert decode_message(frames[0]).payload_type == 2150


def test_partial_multibyte_tail_waits_for_next_chunk():
    """A UTF-8 char split by a chunk boundary must not raise or lose bytes."""
    payload = '{"payloadType":2100,"payload":{"comment":"ทอง"}}'.encode("utf-8")
    split = payload.index(b"\xe0")  # start of a multi-byte Thai char
    decoder = FrameDecoder()
    assert decoder.feed(payload[:split]) == []
    assert decoder.feed(payload[split:split + 2]) == []  # truncated char
    frames = decoder.feed(payload[split + 2:])
    assert len(frames) == 1
    assert decode_message(frames[0]).payload["comment"] == "ทอง"


# ----------------------------------------------------------------------
# (h) Empty feed
# ----------------------------------------------------------------------

def test_empty_feed_returns_no_frames_and_keeps_pending():
    decoder = FrameDecoder()
    assert decoder.feed(b"") == []
    assert decoder.pending() == b""
    tail = b'{"payloadType": 2'
    decoder.feed(tail)
    assert decoder.feed(b"") == []
    assert decoder.pending() == tail


# ----------------------------------------------------------------------
# Garbage still fails loud (never a silent hang)
# ----------------------------------------------------------------------

def test_unparseable_garbage_raises_protocol_error():
    with pytest.raises(ProtocolError):
        FrameDecoder().feed(b"not json at all")


def test_mid_buffer_syntax_error_raises_protocol_error():
    with pytest.raises(ProtocolError):
        FrameDecoder().feed(b'{"a": 1 2}')


def test_invalid_utf8_mid_buffer_raises_protocol_error():
    with pytest.raises(ProtocolError):
        FrameDecoder().feed(b'{"a": "x\xff\xff\xff\xff"}')
