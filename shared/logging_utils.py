"""Structured logging — machine-parseable prefixes for trade events.

Usage:
    from shared.logging_utils import log_trade, log_signal, log_position, log_circuit_break

    log_trade(logger, "OPENED", account="A", direction="BUY", price=1900.50, lots=0.05)
    log_signal(logger, "GENERATED", symbol="XAUUSD", confidence=0.72, reason="ribbon_expansion")
    log_position(logger, "LIMIT", account="A", count=5, max=5)
    log_circuit_break(logger, "TRIGGERED", account="A", reason="3_consecutive_losses")
"""

from __future__ import annotations

import logging
from typing import Any, Optional


def log_trade(
    logger: logging.Logger,
    action: str,
    account: Optional[str] = None,
    direction: Optional[str] = None,
    price: Optional[float] = None,
    lots: Optional[float] = None,
    sl: Optional[float] = None,
    tp: Optional[float] = None,
    ticket: Optional[str] = None,
    pnl: Optional[float] = None,
    confidence: Optional[float] = None,
    reason: Optional[str] = None,
    **extra: Any,
) -> None:
    """Log a trade event with TRADE_ prefix."""
    parts = [f"TRADE_{action}"]
    if account:
        parts.append(f"account={account}")
    if direction:
        parts.append(f"dir={direction}")
    if price is not None:
        parts.append(f"price={price:.2f}")
    if lots is not None:
        parts.append(f"lots={lots:.2f}")
    if sl is not None:
        parts.append(f"sl={sl:.2f}")
    if tp is not None:
        parts.append(f"tp={tp:.2f}")
    if ticket:
        parts.append(f"ticket={ticket}")
    if pnl is not None:
        parts.append(f"pnl={pnl:.2f}")
    if confidence is not None:
        parts.append(f"conf={confidence:.2f}")
    if reason:
        parts.append(f"reason={reason}")
    for k, v in extra.items():
        parts.append(f"{k}={v}")
    logger.info(" | ".join(parts))


def log_signal(
    logger: logging.Logger,
    action: str,
    symbol: str = "XAUUSD",
    signal_type: Optional[str] = None,
    confidence: Optional[float] = None,
    reason: Optional[str] = None,
    **extra: Any,
) -> None:
    """Log a signal event with SIGNAL_ prefix."""
    parts = [f"SIGNAL_{action}"]
    parts.append(f"symbol={symbol}")
    if signal_type:
        parts.append(f"type={signal_type}")
    if confidence is not None:
        parts.append(f"conf={confidence:.2f}")
    if reason:
        parts.append(f"reason={reason}")
    for k, v in extra.items():
        parts.append(f"{k}={v}")
    logger.info(" | ".join(parts))


def log_position(
    logger: logging.Logger,
    action: str,
    account: Optional[str] = None,
    count: Optional[int] = None,
    max: Optional[int] = None,
    **extra: Any,
) -> None:
    """Log a position event with POSITION_ prefix."""
    parts = [f"POSITION_{action}"]
    if account:
        parts.append(f"account={account}")
    if count is not None:
        parts.append(f"count={count}")
    if max is not None:
        parts.append(f"max={max}")
    for k, v in extra.items():
        parts.append(f"{k}={v}")
    logger.info(" | ".join(parts))


def log_circuit_break(
    logger: logging.Logger,
    action: str,
    account: Optional[str] = None,
    reason: Optional[str] = None,
    **extra: Any,
) -> None:
    """Log a circuit breaker event with CIRCUIT_BREAK_ prefix."""
    parts = [f"CIRCUIT_BREAK_{action}"]
    if account:
        parts.append(f"account={account}")
    if reason:
        parts.append(f"reason={reason}")
    for k, v in extra.items():
        parts.append(f"{k}={v}")
    logger.warning(" | ".join(parts))