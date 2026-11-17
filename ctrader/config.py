"""CTraderConfig — env-only credentials for the cTrader Open API adapter.

The MT5 bridge pattern applies here too: credentials live in the VPS .env
(never in the repo, never in code). CTraderBridge.from_env() reads:

    CTRADER_NAME           label for this account (e.g. ctrader-demo-1)
    CTRADER_CLIENT_ID      OAuth application client id
    CTRADER_CLIENT_SECRET  OAuth application client secret
    CTRADER_ACCESS_TOKEN   per-account access token (scope=trading)
    CTRADER_ACCOUNT_ID     ctidTraderAccountId (e.g. 6194964)
    CTRADER_HOST_TYPE      demo | live (default demo — real account 6194964
                           must stay untouched until adapter passes demo)
    CTRADER_SYMBOL         default trading symbol name (default XAUUSD)
"""

import os
from typing import Optional

from pydantic import BaseModel, Field


class CTraderConfig(BaseModel):
    """Connection + identity for one cTrader account (env-only)."""

    name: str = "ctrader"
    client_id: str = ""
    client_secret: str = ""
    access_token: str = ""
    account_id: int = 0  # ctidTraderAccountId
    host_type: str = Field(default="demo", pattern="^(demo|live)$")
    symbol: str = "XAUUSD"
    transport_mode: str = Field(default="json", pattern="^(json|protobuf)$")

    # Timing knobs (seconds) — small enough for tests to shrink.
    request_timeout: float = 10.0
    heartbeat_interval: float = 20.0  # SDK behavior: heartbeat when idle >20s
    # Historical data rate limit: 5 req/s — pause between trendbar chunks.
    trendbar_min_interval: float = 0.2

    @property
    def is_configured(self) -> bool:
        """True when enough credentials exist to attempt a real connection."""
        return bool(self.client_id and self.client_secret and self.access_token and self.account_id)

    @classmethod
    def from_env(cls, env: Optional[dict[str, str]] = None) -> "CTraderConfig":
        """Build config from the environment (or an injected env mapping)."""
        source = os.environ if env is None else env

        def _int(key: str) -> int:
            raw = (source.get(key) or "").strip()
            try:
                return int(raw)
            except ValueError:
                return 0

        return cls(
            name=(source.get("CTRADER_NAME") or "ctrader").strip(),
            client_id=(source.get("CTRADER_CLIENT_ID") or "").strip(),
            client_secret=(source.get("CTRADER_CLIENT_SECRET") or "").strip(),
            access_token=(source.get("CTRADER_ACCESS_TOKEN") or "").strip(),
            account_id=_int("CTRADER_ACCOUNT_ID"),
            host_type=(source.get("CTRADER_HOST_TYPE") or "demo").strip().lower() or "demo",
            symbol=(source.get("CTRADER_SYMBOL") or "XAUUSD").strip() or "XAUUSD",
        )

    def redacted(self) -> dict[str, str]:
        """Human-readable summary with secrets masked (for logs/handoffs)."""
        def mask(value: str) -> str:
            if not value:
                return "<unset>"
            return f"{value[:3]}...{value[-3:]} ({len(value)} chars)"

        return {
            "name": self.name,
            "client_id": mask(self.client_id),
            "client_secret": mask(self.client_secret),
            "access_token": mask(self.access_token),
            "account_id": str(self.account_id or "<unset>"),
            "host_type": self.host_type,
            "symbol": self.symbol,
            "configured": str(self.is_configured),
        }
