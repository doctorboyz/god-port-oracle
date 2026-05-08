#!/usr/bin/env python3
"""Oracle runner — starts data collector and live trader on VPS.

Runs both processes concurrently:
1. Data collector: fetches candles + sentiment → SQLite snapshots (every 5 min)
2. Live trader: generates signals → executes trades on MT5 (every 5 min)
3. Scalp trader: M1 scalping alongside swing (every 60s, if enabled)
4. Telegram notifier: real-time trade alerts + daily summary + bridge status

Environment variables:
    TRADING_PHASE: collect|trade|both (default: both)
    ACCOUNTS: comma-separated list (default: A,B,C)
    COLLECT_INTERVAL: seconds between collection cycles (default: 300)
    TRADE_INTERVAL: seconds between trading cycles (default: 300)
    DRY_RUN: 1=dry run, 0=live trading (default: 1)
    DB_PATH: path to SQLite database (default: /app/data/oracle.db)
    SCALP_ENABLED: 1=enable scalp mode (default: 0)
    SCALP_INTERVAL: seconds between scalp cycles (default: 60)
    M5_SCALP_ENABLED: 1=enable M5 scalp mode (default: 0)
    M5_SCALP_INTERVAL: seconds between M5 scalp cycles (default: 300)
    TG_BOT_TOKEN: Telegram Bot API token (optional)
    TG_CHAT_ID: Telegram chat ID (optional)
"""

import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("oracle")

# Shared event bus for inter-module communication
from shared.events import EventBus

_event_bus = EventBus()


def run_collector(account: str, db_path: str, interval: int):
    """Run data collector in a loop."""
    from dotenv import load_dotenv
    load_dotenv()

    from metty.execution.live_collector import LiveCollector

    collector = LiveCollector(account=account, db_path=Path(db_path))
    logger.info("[Collector:%s] Starting (interval=%ds)", account, interval)

    while True:
        try:
            result = collector.run_once()
            if result:
                logger.info("[Collector:%s] Snapshot #%d collected", account, result)
            else:
                logger.warning("[Collector:%s] No snapshot collected", account)
        except Exception as e:
            logger.error("[Collector:%s] Collection error: %s", account, e)

        time.sleep(interval)


def run_trader(account: str, db_path: str, interval: int, dry_run: bool):
    """Run live trader in a loop."""
    from dotenv import load_dotenv
    load_dotenv()

    from metty.execution.live_trader import LiveTrader, RiskConfig

    risk = RiskConfig(risk_per_trade=float(os.environ.get("RISK_PER_TRADE", "0.02")))
    trader = LiveTrader(
        account=account,
        db_path=Path(db_path),
        dry_run=dry_run,
        risk_config=risk,
        event_bus=_event_bus,
    )
    mode = "DRY-RUN" if dry_run else "LIVE"
    logger.info("[Trader:%s] Starting %s trader (interval=%ds)", account, mode, interval)

    while True:
        try:
            result = trader.run_once()
            action = result.get("action", "unknown")
            logger.info("[Trader:%s] %s: %s", account, mode, result)
        except Exception as e:
            logger.error("[Trader:%s] Trading error: %s", account, e)

        time.sleep(interval)


def run_scalp_trader(account: str, db_path: str, interval: int, dry_run: bool):
    """Run scalp trader in a loop."""
    from dotenv import load_dotenv
    load_dotenv()

    from metty.execution.scalp_trader import ScalpTrader, ScalpRiskConfig

    risk = ScalpRiskConfig(
        risk_per_trade=float(os.environ.get("SCALP_RISK_PER_TRADE", "0.01")),
        max_spread_points=float(os.environ.get("SCALP_SPREAD_MAX", "30")),
    )
    trader = ScalpTrader(
        account=account,
        db_path=Path(db_path),
        dry_run=dry_run,
        risk_config=risk,
        event_bus=_event_bus,
    )
    mode = "DRY-RUN" if dry_run else "LIVE"
    logger.info("[Scalp:%s] Starting %s scalp trader (interval=%ds)", account, mode, interval)

    try:
        while True:
            try:
                result = trader.run_once()
                action = result.get("action", "unknown")
                logger.info("[Scalp:%s] %s: %s", account, mode, result)
            except Exception as e:
                logger.error("[Scalp:%s] Scalp error: %s", account, e)

            time.sleep(interval)
    finally:
        trader.shutdown()


def run_m5_scalp_trader(account: str, db_path: str, interval: int, dry_run: bool):
    """Run M5 scalp trader (6-EMA Ribbon Cloud) in a loop."""
    from dotenv import load_dotenv
    load_dotenv()

    from metty.execution.m5_scalp_trader import M5ScalpTrader, M5ScalpRiskConfig

    risk = M5ScalpRiskConfig(
        risk_per_trade=float(os.environ.get("M5_SCALP_RISK_PER_TRADE", "0.015")),
        max_spread_points=float(os.environ.get("M5_SCALP_SPREAD_MAX", "30")),
    )
    trader = M5ScalpTrader(
        account=account,
        db_path=Path(db_path),
        dry_run=dry_run,
        risk_config=risk,
        event_bus=_event_bus,
    )
    mode = "DRY-RUN" if dry_run else "LIVE"
    logger.info("[M5Scalp:%s] Starting %s M5 scalp trader (interval=%ds)", account, mode, interval)

    while True:
        try:
            result = trader.run_once()
            action = result.get("action", "unknown")
            logger.info("[M5Scalp:%s] %s: %s", account, mode, result)
        except Exception as e:
            logger.error("[M5Scalp:%s] M5 scalp error: %s", account, e)

        time.sleep(interval)


def run_daily_summary(db_path: str, notifier):
    """Send daily summary every 24h at 00:00 UTC."""
    from metty.notify.telegram_bot import TelegramNotifier

    while True:
        now = datetime.now(timezone.utc)
        # Seconds until next midnight UTC
        seconds_until_midnight = (
            (86400 - now.hour * 3600 - now.minute * 60 - now.second) % 86400
        ) or 86400
        logger.info("[DailySummary] Sleeping %ds until next midnight UTC", seconds_until_midnight)
        time.sleep(seconds_until_midnight)

        try:
            notifier.send_daily_summary(db_path=Path(db_path))
        except Exception as e:
            logger.error("[DailySummary] Failed: %s", e)


def run_daily_learning(db_path: str, notifier=None):
    """Run daily learning loop at 00:05 UTC (5 min after midnight).

    Analyzes yesterday's trades, adjusts indicator weights, sends
    Telegram summary, and saves vault report.
    """
    from broky.performance.learning_loop import run_daily_learning as _run_learning

    while True:
        now = datetime.now(timezone.utc)
        # Seconds until 00:05 UTC
        target_seconds = 5 * 60  # 00:05
        current_seconds = now.hour * 3600 + now.minute * 60 + now.second
        if current_seconds < target_seconds:
            seconds_until = target_seconds - current_seconds
        else:
            seconds_until = 86400 - (current_seconds - target_seconds)

        logger.info("[DailyLearning] Sleeping %ds until next 00:05 UTC", seconds_until)
        time.sleep(seconds_until)

        try:
            result = _run_learning(
                db_path=Path(db_path),
                notifier=notifier,
            )
            report = result.get("report")
            adj = result.get("adjustment")
            if adj and not adj.skipped:
                logger.info(
                    "[DailyLearning] Weights adjusted: %d changes",
                    sum(1 for a in adj.adjustments if a.delta != 0),
                )
            elif adj and adj.skipped:
                logger.info("[DailyLearning] Skipped: %s", adj.skip_reason)
            else:
                logger.info("[DailyLearning] No adjustment data")
        except Exception as e:
            logger.error("[DailyLearning] Failed: %s", e)


def run_bridge_status(db_path: str, notifier, accounts: list):
    """Send bridge health status every 4 hours."""
    while True:
        time.sleep(4 * 3600)  # 4 hours
        try:
            from metty.bridge.client import MT5Bridge
            from metty.core.models import AccountConfig, AccountName

            bridge_results = {}
            symbol_map = {"A": "XAUUSDm", "B": "XAUUSD", "C": "XAUUSD"}
            account_configs = {
                "A": AccountConfig(
                    name=AccountName.A,
                    broker_login=os.environ.get("MT5_LOGIN_A", ""),
                    broker_server=os.environ.get("MT5_SERVER_A", "Exness-MT5Trial17"),
                    balance=100.0, leverage=2000,
                    bridge_host=os.environ.get("MT5_BRIDGE_A_HOST", "mt5a"),
                    bridge_port=int(os.environ.get("MT5_BRIDGE_A_PORT", "8001")),
                    signal_group="volume",
                ),
                "B": AccountConfig(
                    name=AccountName.B,
                    broker_login=os.environ.get("MT5_LOGIN_B", ""),
                    broker_server=os.environ.get("MT5_SERVER_B", "Exness-MT5Trial17"),
                    balance=500.0, leverage=500,
                    bridge_host=os.environ.get("MT5_BRIDGE_B_HOST", "mt5b"),
                    bridge_port=int(os.environ.get("MT5_BRIDGE_B_PORT", "8001")),
                    signal_group="ob_os",
                ),
                "C": AccountConfig(
                    name=AccountName.C,
                    broker_login=os.environ.get("MT5_LOGIN_C", ""),
                    broker_server=os.environ.get("MT5_SERVER_C", "Exness-MT5Trial7"),
                    balance=1000.0, leverage=500,
                    bridge_host=os.environ.get("MT5_BRIDGE_C_HOST", "mt5c"),
                    bridge_port=int(os.environ.get("MT5_BRIDGE_C_PORT", "8001")),
                    signal_group="ma",
                ),
            }

            for account in accounts:
                account = account.strip()
                if not account or account not in account_configs:
                    continue
                try:
                    config = account_configs[account]
                    bridge = MT5Bridge(config)
                    info = bridge.fetch_account_info_sync()
                    symbol = symbol_map.get(account, "XAUUSD")
                    candles = bridge.fetch_candles_sync(symbol, "M5", 1)
                    price = float(candles["close"].iloc[-1]) if candles is not None and not candles.empty else 0
                    equity = info.equity if info else 0
                    balance = info.balance if info else 0
                    bridge_results[account] = {
                        "connected": True,
                        "symbol": symbol,
                        "price": price,
                        "equity": equity,
                        "balance": balance,
                    }
                    logger.info("[BridgeStatus] %s: connected, %s=%.2f, equity=%.2f, balance=%.2f",
                                account, symbol, price, equity, balance)
                except Exception as e:
                    bridge_results[account] = {"connected": False}
                    logger.warning("[BridgeStatus] %s: disconnected (%s)", account, e)

            notifier.send_bridge_status(bridge_results)
        except Exception as e:
            logger.error("[BridgeStatus] Failed: %s", e)


def main():
    phase = os.environ.get("TRADING_PHASE", "both")
    accounts = os.environ.get("ACCOUNTS", "A,B,C").split(",")
    db_path = os.environ.get("DB_PATH", "/app/data/oracle.db")
    collect_interval = int(os.environ.get("COLLECT_INTERVAL", "300"))
    trade_interval = int(os.environ.get("TRADE_INTERVAL", "300"))
    dry_run = os.environ.get("DRY_RUN", "1") == "1"

    from dotenv import load_dotenv
    load_dotenv()

    from metty.core.db import init_db, insert_account
    init_db(Path(db_path))

    # Seed demo accounts if they don't exist yet
    default_accounts = {
        "A": {"balance": 100.0, "leverage": 2000, "host": os.environ.get("MT5_BRIDGE_A_HOST", "mt5a"), "port": int(os.environ.get("MT5_BRIDGE_A_PORT", "8001")), "group": "conservative"},
        "B": {"balance": 500.0, "leverage": 500, "host": os.environ.get("MT5_BRIDGE_B_HOST", "mt5b"), "port": int(os.environ.get("MT5_BRIDGE_B_PORT", "8001")), "group": "moderate"},
        "C": {"balance": 1000.0, "leverage": 500, "host": os.environ.get("MT5_BRIDGE_C_HOST", "mt5c"), "port": int(os.environ.get("MT5_BRIDGE_C_PORT", "8001")), "group": "moderate"},
    }
    for acct_name, acct_cfg in default_accounts.items():
        try:
            insert_account(
                name=acct_name,
                balance=acct_cfg["balance"],
                leverage=acct_cfg["leverage"],
                bridge_host=acct_cfg["host"],
                bridge_port=acct_cfg["port"],
                signal_group=acct_cfg["group"],
                db_path=Path(db_path),
            )
            logger.info("Seeded account %s", acct_name)
        except Exception:
            pass  # Account already exists

    # Setup Telegram notifier
    tg_token = os.environ.get("TG_BOT_TOKEN", "")
    tg_chat_id = os.environ.get("TG_CHAT_ID", "")
    from metty.notify.telegram_bot import TelegramNotifier
    notifier = TelegramNotifier(
        token=tg_token,
        chat_id=tg_chat_id,
        db_path=Path(db_path),
        enabled=bool(tg_token and tg_chat_id),
    )
    if notifier.enabled:
        notifier.subscribe(_event_bus)
        logger.info("Telegram notifier ENABLED (chat_id=%s)", tg_chat_id)
    else:
        logger.info("Telegram notifier DISABLED (set TG_BOT_TOKEN + TG_CHAT_ID to enable)")

    logger.info("=== Oracle Engine Starting ===")
    logger.info("Phase: %s | Accounts: %s | DB: %s", phase, accounts, db_path)
    logger.info("Collector interval: %ds | Trader interval: %ds | Dry run: %s",
                collect_interval, trade_interval, dry_run)

    scalp_enabled = os.environ.get("SCALP_ENABLED", "0") == "1"
    scalp_interval = int(os.environ.get("SCALP_INTERVAL", "60"))
    if scalp_enabled:
        logger.info("Scalp mode ENABLED | Scalp interval: %ds", scalp_interval)
    else:
        logger.info("Scalp mode DISABLED (set SCALP_ENABLED=1 to enable)")

    m5_scalp_enabled = os.environ.get("M5_SCALP_ENABLED", "0") == "1"
    m5_scalp_interval = int(os.environ.get("M5_SCALP_INTERVAL", "300"))
    if m5_scalp_enabled:
        logger.info("M5 Scalp mode ENABLED | M5 Scalp interval: %ds", m5_scalp_interval)
    else:
        logger.info("M5 Scalp mode DISABLED (set M5_SCALP_ENABLED=1 to enable)")

    swing_disabled_accounts = [a.strip().upper() for a in os.environ.get("SWING_DISABLED_ACCOUNTS", "").split(",") if a.strip()]
    if swing_disabled_accounts:
        logger.info("Swing DISABLED for accounts: %s", swing_disabled_accounts)

    learning_mode = os.environ.get("LEARNING_MODE", "0") == "1"
    if learning_mode:
        logger.info("LEARNING MODE ENABLED — all blockers bypassed, max trades for data collection")
    else:
        logger.info("LEARNING MODE DISABLED (set LEARNING_MODE=1 to enable)")

    threads = []

    for account in accounts:
        account = account.strip()
        if not account:
            continue

        if phase in ("collect", "both"):
            t = threading.Thread(
                target=run_collector,
                args=(account, db_path, collect_interval),
                name=f"collector-{account}",
                daemon=True,
            )
            threads.append(t)

        if phase in ("trade", "both"):
            if account in swing_disabled_accounts:
                logger.info("[Trader:%s] SKIPPED — swing disabled for this account", account)
            else:
                t = threading.Thread(
                    target=run_trader,
                    args=(account, db_path, trade_interval, dry_run),
                    name=f"trader-{account}",
                    daemon=True,
                )
                threads.append(t)

            # Scalp trader (parallel thread, M1)
            if scalp_enabled:
                t = threading.Thread(
                    target=run_scalp_trader,
                    args=(account, db_path, scalp_interval, dry_run),
                    name=f"scalp-{account}",
                    daemon=True,
                )
                threads.append(t)

            # M5 Scalp trader (parallel thread, 6-EMA Ribbon Cloud)
            if m5_scalp_enabled:
                t = threading.Thread(
                    target=run_m5_scalp_trader,
                    args=(account, db_path, m5_scalp_interval, dry_run),
                    name=f"m5-scalp-{account}",
                    daemon=True,
                )
                threads.append(t)

    # Notification threads (only if Telegram is enabled)
    if notifier.enabled:
        t = threading.Thread(
            target=run_daily_summary,
            args=(db_path, notifier),
            name="daily-summary",
            daemon=True,
        )
        threads.append(t)

        t = threading.Thread(
            target=run_bridge_status,
            args=(db_path, notifier, accounts),
            name="bridge-status",
            daemon=True,
        )
        threads.append(t)

    # Daily learning thread (always runs — adjusts weights, sends Telegram if available)
    t = threading.Thread(
        target=run_daily_learning,
        args=(db_path, notifier if notifier.enabled else None),
        name="daily-learning",
        daemon=True,
    )
    threads.append(t)

    # Start all threads
    for t in threads:
        t.start()
        logger.info("Started thread: %s", t.name)

    # Wait for shutdown signal
    def shutdown(signum, frame):
        logger.info("Received signal %s, shutting down...", signum)
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # Keep main thread alive
    try:
        while True:
            alive = [t.name for t in threads if t.is_alive()]
            if not alive:
                logger.error("All threads died, exiting")
                sys.exit(1)
            time.sleep(60)
    except KeyboardInterrupt:
        logger.info("Shutting down...")


if __name__ == "__main__":
    main()