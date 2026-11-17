"""cTrader Open API endpoints — mirrored from the official SDK.

Source: ctrader_open_api/endpoints.py in spotware/OpenApiPy (MIT), verified
against help.ctrader.com/open-api. JSON mode (port 5036) is documented at
https://help.ctrader.com/open-api/protocol-buffers-json/ — it exchanges the
same messages as protobuf mode but with newline-terminated JSON frames, which
lets us connect with the Python stdlib alone (no new dependency; this repo's
pyproject.toml is frozen and holds no protobuf/twisted).
"""


class EndPoints:
    # OAuth2 (authorization code / refresh token, over HTTPS)
    AUTH_URI = "https://openapi.ctrader.com/apps/auth"
    TOKEN_URI = "https://openapi.ctrader.com/apps/token"

    # Protobuf-over-TCP (official SDK transport)
    PROTOBUF_DEMO_HOST = "demo.ctraderapi.com"
    PROTOBUF_LIVE_HOST = "live.ctraderapi.com"
    PROTOBUF_PORT = 5035

    # JSON-over-TCP / WebSocket (newline-terminated frames)
    JSON_DEMO_HOST = "demo.ctraderapi.com"
    JSON_LIVE_HOST = "live.ctraderapi.com"
    JSON_PORT = 5036

    @classmethod
    def host_for(cls, host_type: str, mode: str = "json") -> str:
        """Resolve hostname for 'demo'/'live' and transport 'json'/'protobuf'."""
        host_type = (host_type or "demo").lower()
        mode = (mode or "json").lower()
        if mode == "protobuf":
            return cls.PROTOBUF_LIVE_HOST if host_type == "live" else cls.PROTOBUF_DEMO_HOST
        return cls.JSON_LIVE_HOST if host_type == "live" else cls.JSON_DEMO_HOST

    @classmethod
    def port_for(cls, mode: str = "json") -> int:
        return cls.PROTOBUF_PORT if (mode or "json").lower() == "protobuf" else cls.JSON_PORT
