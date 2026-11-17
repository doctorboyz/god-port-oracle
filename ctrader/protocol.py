"""cTrader Open API wire protocol — JSON mode.

The cTrader Open API supports two serializations over TLS TCP:
  * protobuf (port 5035): 4-byte length-prefixed ProtoMessage frames
  * JSON (port 5036): newline-terminated JSON objects with the same
    envelope shape (clientMsgId / payloadType / payload)

This module implements the JSON variant because it needs only the stdlib —
this repo's dependency set is frozen and holds no protobuf/twisted. Envelope
and framing reference: https://help.ctrader.com/open-api/sending-receiving-json/

Scaling rules (from the canonical .proto, see ctrader/proto_reference/):
  * spot/trendbar prices  : 1/100000 of a price unit (123000 -> 1.23)
  * order/trade volumes   : cents of a unit (1000 -> 10.00 units);
                            lots = volume_cents / 100 / lotSize
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


# --- JSON transport framing: one JSON object per line, UTF-8 ---

def encode_frame(message: ApiMessage) -> bytes:
    """Wrap one envelope as a newline-terminated frame."""
    return encode_message(message).encode("utf-8") + b"\n"


class FrameDecoder:
    """Incremental newline-framing decoder (feeds may split mid-frame)."""

    def __init__(self) -> None:
        self._buffer = b""

    def feed(self, data: bytes) -> list[bytes]:
        """Consume raw bytes, return any complete frames found."""
        self._buffer += data
        frames: list[bytes] = []
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            if line.strip():
                frames.append(line)
        return frames

    def pending(self) -> bytes:
        """Bytes buffered without a newline yet (for diagnostics)."""
        return self._buffer


# --- Protobuf framing (future, port 5035) — length prefix only.
# Kept unimplemented on purpose; see ctrader/README.md "protobuf mode".

def encode_length_prefix(length: int, little_endian: bool = True) -> bytes:
    """4-byte length prefix for protobuf mode (endianness per official docs)."""
    return struct.pack("<I" if little_endian else ">I", length)


# --- Unit/scaling helpers ---

PRICE_SCALE = 100_000  # spot & trendbar prices: 1/100000 of a price unit
VOLUME_CENTS_PER_UNIT = 100  # volumes are expressed in cents of a unit
DEFAULT_MONEY_DIGITS = 2  # balance/swap/margin default exponent
DEFAULT_LOT_SIZE = 100  # XAUUSD: 1 lot = 100 oz — replaced by symbol data


def price_to_real(scaled: int | float | None) -> float:
    """Protocol price (1/100000) -> real price. Missing -> 0.0."""
    if scaled is None:
        return 0.0
    return float(scaled) / PRICE_SCALE


def price_to_proto(real: float) -> int:
    """Real price -> protocol int64 (1/100000), rounded to nearest."""
    return int(round(float(real) * PRICE_SCALE))


def lots_to_volume_cents(lots: float, lot_size: float = DEFAULT_LOT_SIZE) -> int:
    """Lots -> protocol volume (cents of a unit). XAUUSD 0.01 lot -> 100."""
    return int(round(float(lots) * float(lot_size) * VOLUME_CENTS_PER_UNIT))


def volume_cents_to_lots(volume_cents: int, lot_size: float = DEFAULT_LOT_SIZE) -> float:
    """Protocol volume (cents) -> lots. 10000 cents of gold -> 1.0 lot."""
    if not volume_cents:
        return 0.0
    return float(volume_cents) / VOLUME_CENTS_PER_UNIT / float(lot_size)


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
