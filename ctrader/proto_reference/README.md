# cTrader Open API protobuf reference (verbatim, unmodified)

Source: https://github.com/spotware/openapi-proto-messages (MIT License,
Copyright (c) 2021 Spotware) — copied 2026-11-17 for offline reference.

These are the **canonical wire-contract definitions** of the cTrader Open
API. Every payload-type ID, enum value, field name and scaling comment in
`ctrader/messages.py`, `ctrader/protocol.py` and `ctrader/bridge.py` was
taken from these files — do not trust secondary sources (blog posts, this
package's docstrings) over them.

Files:

- `OpenApiCommonMessages.proto` — ProtoMessage envelope, ProtoErrorRes, ProtoHeartbeatEvent
- `OpenApiCommonModelMessages.proto` — ProtoErrorCode (common, 1-99), ProtoPayloadType
- `OpenApiMessages.proto` — all ProtoOA*Req/Res/Event messages
- `OpenApiModelMessages.proto` — ProtoOAPayloadType enum (2100-2198), entity messages
  (ProtoOATrader, ProtoOAPosition, ProtoOAOrder, ProtoOADeal, ProtoOASymbol, ...)

If the upstream repo revises, re-diff against these copies and re-verify the
adapter before connecting for real.
