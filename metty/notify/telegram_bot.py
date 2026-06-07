"""Telegram Bot notifier — sends trade alerts and status via Bot API.

Uses requests to POST messages directly to the Telegram Bot API.
No polling or command handling — one-way notifications only.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

from shared.events import EventBus, EventType, Event

logger = logging.getLogger(__name__)

# Telegram Bot API limits
MAX_MSG_LENGTH = 4096
MIN_MSG_INTERVAL = 1.0  # seconds between messages (rate limit)


class TelegramNotifier:
    """Sends trading notifications via Telegram Bot API.

    Subscribes to EventBus events and formats concise messages:
    - Trade opened/closed alerts (real-time)
    - Circuit breaker alerts (real-time)
    - Daily summary (every 24h)
    - Bridge health status (every 4h)
    """

    def __init__(
        self,
        token: str,
        chat_id: str,
        db_path: Optional[Path] = None,
        enabled: bool = True,
    ):
        self.token = token
        self.chat_id = chat_id
        self.db_path = db_path
        self.enabled = enabled and bool(token) and bool(chat_id)
        self.base_url = f"https://api.telegram.org/bot{token}"
        self._last_sent: float = 0.0

        if not self.enabled:
            logger.info("Telegram notifications DISABLED (no token or chat_id)")

    def send(self, text: str) -> bool:
        """Send a message to Telegram. Returns True if successful."""
        if not self.enabled:
            return False

        # Rate limit
        now = time.time()
        elapsed = now - self._last_sent
        if elapsed < MIN_MSG_INTERVAL:
            time.sleep(MIN_MSG_INTERVAL - elapsed)

        # Split long messages
        if len(text) > MAX_MSG_LENGTH:
            return self._send_chunked(text)

        try:
            resp = requests.post(
                f"{self.base_url}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
            self._last_sent = time.time()
            if resp.status_code == 200:
                return True
            logger.warning("Telegram API error: %d %s", resp.status_code, resp.text[:200])
            return False
        except Exception as e:
            logger.warning("Telegram send failed: %s", e)
            return False

    def _send_chunked(self, text: str) -> bool:
        """Send a long message in chunks."""
        lines = text.split("\n")
        chunk = ""
        success = True
        for line in lines:
            if len(chunk) + len(line) + 1 > MAX_MSG_LENGTH:
                success = self.send(chunk.strip()) and success
                chunk = ""
            chunk += line + "\n"
        if chunk.strip():
            success = self.send(chunk.strip()) and success
        return success

    def subscribe(self, bus: EventBus) -> None:
        """Subscribe to EventBus events."""
        bus.subscribe(EventType.TRADE_OPENED, self.on_trade_opened)
        bus.subscribe(EventType.TRADE_CLOSED, self.on_trade_closed)
        bus.subscribe(EventType.CIRCUIT_BREAKER_TRIGGERED, self.on_circuit_breaker)
        bus.subscribe(EventType.TREND_FLIP, self.on_trend_flip)

    # --- Event handlers ---

    def on_trade_opened(self, event: Event) -> None:
        """Format and send trade opened notification."""
        d = event.data
        direction_emoji = "✅" if d.get("direction") == "BUY" else "⬇️"
        mode = d.get("trading_mode", "swing")
        account = d.get("account", "?")
        msg = (
            f"{direction_emoji} <b>{d.get('direction', '?')} {d.get('symbol', 'XAUUSD')}</b> @"
            f" {d.get('price', 0):.2f}\n"
            f"Account: {account} | Mode: {mode}\n"
            f"SL: {d.get('sl', 0):.2f} | TP: {d.get('tp', 0):.2f} | "
            f"Lots: {d.get('lots', 0):.4f}\n"
            f"Confidence: {d.get('confidence', 0):.2f} | Regime: {d.get('regime', '?')}\n"
            f"<i>{d.get('reason', '')}</i>"
        )
        self.send(msg)

    def on_trade_closed(self, event: Event) -> None:
        """Format and send trade closed notification."""
        d = event.data
        direction = d.get("direction", "?")
        exit_reason = d.get("exit_reason", "?")
        pnl = d.get("pnl", 0)
        pnl_pct = d.get("pnl_pct", 0)
        mode = d.get("trading_mode", "swing")
        account = d.get("account", "?")

        emoji = "💰" if pnl > 0 else "💸"
        reason_emoji = {
            "take_profit": "🎯",
            "stop_loss": "⚠️",
            "max_holding": "⏰",
        }.get(exit_reason, "")

        msg = (
            f"{emoji} {direction} <b>closed</b> → {reason_emoji}{exit_reason}\n"
            f"Entry: {d.get('entry_price', 0):.2f} → Exit: {d.get('exit_price', 0):.2f}\n"
            f"PnL: {pnl:+.2f} ({pnl_pct:+.4f}%) | "
            f"Account: {account} | Mode: {mode}"
        )
        self.send(msg)

    def on_circuit_breaker(self, event: Event) -> None:
        """Format and send circuit breaker alert."""
        d = event.data
        msg = (
            f"⚠️ <b>Circuit Breaker</b> — Account {d.get('account', '?')}\n"
            f"Consecutive losses: {d.get('consecutive_losses', '?')}\n"
            f"Daily loss: {d.get('daily_loss_pct', 0):.1f}%"
        )
        self.send(msg)

    def on_trend_flip(self, event: Event) -> None:
        """Format and send trend flip alert (D1 or H4 direction change)."""
        d = event.data
        tf = d.get("timeframe", "?")
        direction = d.get("direction", "?")
        old_direction = d.get("old_direction", "?")
        symbol = d.get("symbol", "XAUUSD")
        price = d.get("price", 0)
        # Flip emojis: bullish = 📈, bearish = 📉
        emoji = "📈" if direction == "bullish" else "📉"
        msg = (
            f"🔄 <b>Trend Flip</b> — {symbol} {tf}\n"
            f"{old_direction} → {emoji} <b>{direction}</b>\n"
            f"Price: {price:.2f}"
        )
        self.send(msg)

    # --- Periodic reports ---

    def send_daily_summary(self, db_path: Optional[Path] = None) -> None:
        """Send daily trading summary from DB."""
        path = db_path or self.db_path
        if not path:
            return

        try:
            from metty.core.db import get_connection
            conn = get_connection(path)
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

            # Trades today
            rows = conn.execute(
                """SELECT trading_mode, direction, COUNT(*),
                          SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                          SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses,
                          ROUND(SUM(pnl), 2) as total_pnl
                   FROM live_trades
                   WHERE timestamp >= ? AND is_open = 0
                   GROUP BY trading_mode, direction""",
                (f"{today}%",),
            ).fetchall()

            # Open trades
            open_count = conn.execute(
                "SELECT trading_mode, COUNT(*) FROM live_trades WHERE is_open = 1 GROUP BY trading_mode"
            ).fetchall()

            # Account balances
            accounts = conn.execute(
                "SELECT name, balance FROM accounts WHERE is_active = 1"
            ).fetchall()

            conn.close()

            lines = [f"📊 <b>Daily Summary</b> — {today}"]

            for row in rows:
                mode, direction, count, wins, losses, total_pnl = row
                lines.append(
                    f"  {mode} {direction}: {count} trades ({wins}W/{losses}L) | "
                    f"PnL: {total_pnl:+.2f}"
                )

            if open_count:
                open_str = ", ".join(f"{mode}: {cnt} open" for mode, cnt in open_count)
                lines.append(f"Open: {open_str}")

            bal_str = " | ".join(f"{name}=${bal:.2f}" for name, bal in accounts)
            lines.append(f"Balance: {bal_str}")

            self.send("\n".join(lines))

        except Exception as e:
            logger.error("Daily summary failed: %s", e)

    def send_bridge_status(self, bridge_results: dict) -> None:
        """Send bridge health status.

        Args:
            bridge_results: dict mapping account name to status dict:
                {"connected": bool, "symbol": str, "price": float, "equity": float, "balance": float}
        """
        lines = [f"🏥 <b>Bridge Status</b> — "
                  f"{datetime.now(timezone.utc).strftime('%H:%M UTC')}"]

        for account, status in bridge_results.items():
            if status.get("connected"):
                price = status.get("price", 0)
                symbol = status.get("symbol", "?")
                equity = status.get("equity", 0)
                balance = status.get("balance", 0)
                equity_str = f" | Eq=${equity:.2f}" if equity else ""
                balance_str = f" Bal=${balance:.2f}" if balance else ""
                lines.append(f"  mt5{account.lower()} ({account}): ✅ {symbol} {price:.2f}{equity_str}{balance_str}")
            else:
                lines.append(f"  mt5{account.lower()} ({account}): ❌ disconnected")

        self.send("\n".join(lines))