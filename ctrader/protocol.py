"""cTrader Open API wire protocol — JSON mode.

The cTrader Open API supports two serializations over TLS TCP:
  * protobuf (port 5035): 4-byte length-prefixed ProtoMessage frames
  * JSON (port 5036): bare JSON objects with the same envelope shape
    (clientMsgId / payloadType / payload). The server concatenates them
    on the stream with NO delimiter (verified on the wire 19/11 — the
    docs specify the JSON serialization but not the framing); clients
    may send newline-terminated frames, which FrameDecoder also accepts.

This module implements the JSON variant because it needs only the stdlib —
this repo's dependency set is frozen and holds no protobuf/twisted. Envelope
and framing reference: https://help.ctrader.com/open-api/sending-receiving-json/

Scaling rules (from the canonical .proto, see ctrader/proto_reference/):
  * spot/trendbar prices  : 1/100000 of a price unit (123000 -> 1.23)
  * order/trade volumes   : cents of a unit (10000 cents = 100 oz of gold);
                            lotSize is already in cents, so
                            volume_cents = lots * lotSize (XAUUSD lotSize=10000)
  * money fields          : scaled by 10^moneyDigits (per-message field)
  * trendbar OHLC         : low is absolute (1/100000), open/high/close are
                            deltas from low
"""

import json
import struct
from dataclasses import dataclass, field
from typing import Any, Optional


class ProtocolError(Exception):
    """Raised when a frame or envelope cannot be decoded."""


@dataclass(frozen=True)
class ApiMessage:
    """One Open API message (JSON envelope form)."""

    payload_type: int
    payload: dict[str, Any] = field(default_factory=dict)
    client_msg_id: Optional[str] = None


def encode_message(message: ApiMessage) -> str:
    """Serialize an ApiMessage to the JSON envelope string (no newline)."""
    envelope: dict[str, Any] = {"payloadType": message.payload_type}
    if message.client_msg_id is not None:
        envelope["clientMsgId"] = message.client_msg_id
    envelope["payload"] = message.payload
    return json.dumps(envelope, separators=(",", ":"), default=str)


def decode_message(data: str | bytes) -> ApiMessage:
    """Parse one JSON envelope into an ApiMessage."""
    if isinstance(data, bytes):
        data = data.decode("utf-8")
    try:
        envelope = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"invalid JSON frame: {exc}") from exc
    payload_type = envelope.get("payloadType")
    if not isinstance(payload_type, int):
        raise ProtocolError(f"frame missing integer payloadType: {envelope!r}")
    payload = envelope.get("payload")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ProtocolError(f"payload must be a JSON object: {payload!r}")
    return ApiMessage(
        payload_type=payload_type,
        payload=payload,
        client_msg_id=envelope.get("clientMsgId"),
    )


# --- JSON transport framing: bare JSON objects, UTF-8 (newlines optional) ---

def encode_frame(message: ApiMessage) -> bytes:
    """Wrap one envelope as a newline-terminated frame."""
    return encode_message(message).encode("utf-8") + b"\n"


class FrameDecoder:
    """Incremental JSON-object framing decoder (feeds may split mid-frame).

    Wire truth (verified 19/11 on both demo & live ctraderapi.com:5036,
    see ctrader/README.md): the server writes bare JSON objects back to
    back on the TCP stream with NO delimiter — the official docs specify
    the JSON serialization but not the framing. Newline-terminated frames
    (what encode_frame sends) and any whitespace between objects are also
    accepted. Incomplete JSON never raises: the bytes stay buffered until
    a later feed completes the object. Bytes that can never grow into a
    JSON value raise ProtocolError (fail loud, not hang).
    """

    _JSON_LITERALS = ("true", "false", "null")

    def __init__(self) -> None:
        self._buffer = b""
        self._json = json.JSONDecoder()

    def feed(self, data: bytes) -> list[bytes]:
        """Consume raw bytes, return any complete JSON-object frames found.

        Frames are the exact byte substrings received — decode_message()
        re-parses them downstream, so no dict conversion happens here.
        """
        self._buffer += data
        frames: list[bytes] = []
        while True:
            stripped = self._buffer.lstrip()
            if not stripped:
                self._buffer = b""
                return frames
            text = self._decode_or_wait(stripped)
            if text is None:
                return frames  # trailing bytes incomplete — keep buffering
            try:
                _, end = self._json.raw_decode(text)
            except json.JSONDecodeError as exc:
                if self._is_incomplete(text, exc):
                    return frames
                raise ProtocolError(f"invalid JSON frame: {exc}") from exc
            frame = text[:end].encode("utf-8")
            frames.append(frame)
            self._buffer = stripped[len(frame):]

    def pending(self) -> bytes:
        """Bytes buffered without a complete frame yet (for diagnostics)."""
        return self._buffer

    @staticmethod
    def _decode_or_wait(stripped: bytes) -> Optional[str]:
        """Decode the buffer as UTF-8; None = truncated tail, wait for more.

        A multi-byte char split by a chunk boundary is the only legitimate
        decode failure; anything else is garbage and fails loud.
        """
        try:
            return stripped.decode("utf-8")
        except UnicodeDecodeError as exc:
            lead = stripped[exc.start]
            if 0xC2 <= lead <= 0xF4 and exc.start >= len(stripped) - 3:
                return None
            raise ProtocolError(f"invalid UTF-8 in frame: {exc}") from exc

    @classmethod
    def _is_incomplete(cls, text: str, exc: json.JSONDecodeError) -> bool:
        """True when the buffer is a valid prefix of a longer JSON value.

        raw_decode stops either wanting more input (error at the very end
        of the buffer, an unterminated string, or a true/false/null
        literal still growing) or at genuinely unparseable bytes — only
        the first kind may wait, the second must raise.
        """
        if "Unterminated string" in exc.msg:
            return True
        if exc.pos >= len(text):
            return True
        tail = text[exc.pos:].lower()
        return any(literal.startswith(tail) for literal in cls._JSON_LITERALS)


# --- Protobuf framing (future, port 5035) — length prefix only.
# Kept unimplemented on purpose; see ctrader/README.md "protobuf mode".

def encode_length_prefix(length: int, little_endian: bool = True) -> bytes:
    """4-byte length prefix for protobuf mode (endianness per official docs)."""
    return struct.pack("<I" if little_endian else ">I", length)


# --- Unit/scaling helpers ---

PRICE_SCALE = 100_000  # spot & trendbar prices: 1/100000 of a price unit
DEFAULT_MONEY_DIGITS = 2  # balance/swap/margin default exponent
# XAUUSD wire truth (SymbolByIdRes 19/11): lotSize=10000 — the proto field
# is ALREADY in cents ("Lot size of the Symbol (in cents)",
# proto_reference/OpenApiModelMessages.proto), so volume_cents = lots ×
# lotSize and nothing more. 100 cents = 1 unit (oz) of the underlying.
DEFAULT_LOT_SIZE = 10_000


def price_to_real(scaled: int | float | None) -> float:
    """Protocol price (1/100000) -> real price. Missing -> 0.0."""
    if scaled is None:
        return 0.0
    return float(scaled) / PRICE_SCALE


def price_to_proto(real: float) -> int:
    """Real price -> protocol int64 (1/100000), rounded to nearest."""
    return int(round(float(real) * PRICE_SCALE))


def lots_to_volume_cents(lots: float, lot_size: float = DEFAULT_LOT_SIZE) -> int:
    """Lots -> protocol volume. lotSize is already in cents, so this is a
    plain product: XAUUSD (lotSize=10000) 0.01 lot -> 100 cents, 1 lot ->
    10000 cents (= 100 oz). Never multiply by 100 again on top — that was
    the 100x-oversize bug (1 lot on the real wire became 1,000,000 cents).
    """
    return int(round(float(lots) * float(lot_size)))


def volume_cents_to_lots(volume_cents: int, lot_size: float = DEFAULT_LOT_SIZE) -> float:
    """Protocol volume (cents) -> lots. XAUUSD 10000 cents of gold -> 1.0 lot."""
    if not volume_cents:
        return 0.0
    return float(volume_cents) / float(lot_size)


def money_to_real(amount: int | float | None, money_digits: int = DEFAULT_MONEY_DIGITS) -> float:
    """Protocol money field -> real amount using 10^moneyDigits exponent."""
    if amount is None:
        return 0.0
    return float(amount) / (10 ** int(money_digits))


def leverage_from_cents(leverage_in_cents: int | None, max_leverage: int | None = None) -> int:
    """ProtoOATrader.leverageInCents (1:50 -> 5000) -> engine leverage int."""
    if leverage_in_cents:
        return int(leverage_in_cents) // 100
    if max_leverage:
        return int(max_leverage)
    return 1
