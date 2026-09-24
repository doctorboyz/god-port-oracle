"""Causal proof tests: ISSUE-045 — CircuitBreaker cooldown evaded by restart.

Hypothesis
----------
ISSUE-045 occurs because LiveTrader.__init__ constructs CircuitBreaker
without consulting the DB (source of truth): when the train container
restarts mid-cooldown (mr-bet: 3-loss streak → 24h pause, e.g. the
2026-09-22 20:47 restart), consecutive_losses restarts at 0 and
is_active is False, so new trades open immediately — the cooldown is
silently evaded even though live_trades still shows the trailing loss
streak.

Causal proof
------------
Seed a temp DB with a trailing 3-loss streak (closed, recent exit_time)
for account B → construct LiveTrader(account="B", dry_run=True, db_path=…)
→ the circuit breaker must refuse new trades and carry the streak.
The mechanism under test is state restoration at init, not the CB
internals (those are covered by unit tests): a fresh CB over the same
DB history must reproduce the pre-restart blocking decision.

Controls (guard against false-positive RED):
- 0-loss history → can_open_trade True, consecutive_losses == 0
  (legacy behavior preserved — Real-A default path unchanged)
- 2-loss streak → consecutive_losses == 2, can_open_trade True
  (streak carries over so the NEXT post-restart loss triggers in-memory)

References
----------
- ISSUE-045: CircuitBreaker state is in-memory only; restart evades cooldown
- Pattern precedent: DrawdownProtector.sync_pnl_from_db (state from DB truth)
- metty/core/db.py get_consecutive_losses — existing trailing-streak query
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _seed_db(tmp_path, losses: int) -> str:
    """Create a temp DB whose newest `losses` closed trades are all losses."""
    from metty.core.db import init_db

    db_path = str(tmp_path / f"test_{losses}.db")
    init_db(db_path)
    now = datetime.now(timezone.utc)
    conn = sqlite3.connect(db_path)
    # Losses at hours 1..N ago (oldest first so ids ascend with time —
    # ties in exit_time break by id DESC), then one WIN at N+1h ago so
    # the streak has a definite older boundary (or nothing when losses=0).
    rows = [("win", losses + 1)]
    rows += [("loss", losses - i) for i in range(losses)]
    for kind, hours_ago in rows:
        exit_ts = now - timedelta(hours=hours_ago)
        pnl = -5.0 if kind == "loss" else 5.0
        conn.execute(
            "INSERT INTO live_trades (account_id, timestamp, direction, "
            "symbol, entry_price, stop_loss, take_profit, lot_size, confidence, "
            "regime, session, d1_trend, reason, trading_mode, strategy_id, "
            "is_open, exit_time, exit_price, pnl, exit_reason) "
            "VALUES (2, ?, 'BUY', 'XAUUSD', 4000.0, 3990.0, 4010.0, 0.01, 0.6, "
            "'ranging', 'overlap', 'neutral', 'mr entry', 'swing', 'mr-bet-B', "
            "0, ?, ?, ?, 'stop_loss')",
            (exit_ts.isoformat(), exit_ts.isoformat(), 3990.0, pnl),
        )
    conn.commit()
    conn.close()
    return db_path


def _make_trader(db_path, monkeypatch):
    from metty.execution.live_trader import LiveTrader
    monkeypatch.setenv("MT5_BRIDGE_B_HOST", "localhost")
    monkeypatch.setenv("MT5_BRIDGE_B_PORT", "8001")
    monkeypatch.setenv("MT5_LOGIN_B", "1")
    monkeypatch.setenv("MT5_PASSWORD_B", "x")
    monkeypatch.setenv("MT5_SERVER_B", "Exness-MT5Real15")
    # mr-bet's real cooldown (24h). The seeded losses are 1-3h old, so with
    # the legacy 15-min default the pause would already be expired (and
    # correctly lifted) — pin the window to measure the restore mechanism.
    monkeypatch.setenv("CIRCUIT_BREAKER_COOLDOWN_MINUTES_B", "1440")
    return LiveTrader(account="B", dry_run=True, db_path=db_path)


class TestCircuitBreakerRestartRehydration:
    """ISSUE-045: CB state must be restored from DB trade history at init."""

    def test_three_loss_streak_blocks_after_restart(self, tmp_path, monkeypatch):
        """Causal test: DB shows 3 trailing losses (the mr-bet trigger) →
        a freshly constructed trader must still refuse to open trades."""
        db_path = _seed_db(tmp_path, losses=3)
        t = _make_trader(db_path, monkeypatch)
        can_trade, reason = t.circuit_breaker.can_open_trade()
        assert can_trade is False, (
            "ISSUE-045 unfixed: restart evaded the 3-loss cooldown even "
            f"though DB shows a trailing loss streak (reason={reason})"
        )

    def test_streak_count_carried_from_db(self, tmp_path, monkeypatch):
        """The streak count itself must carry over (not just the boolean
        block) — rejection events and post-restart accounting read it."""
        db_path = _seed_db(tmp_path, losses=3)
        t = _make_trader(db_path, monkeypatch)
        assert t.circuit_breaker.state.consecutive_losses == 3

    def test_two_loss_streak_carried_not_active(self, tmp_path, monkeypatch):
        """2 trailing losses → streak carried as 2 but NOT active: the
        next post-restart loss must complete the streak in-memory."""
        db_path = _seed_db(tmp_path, losses=2)
        t = _make_trader(db_path, monkeypatch)
        assert t.circuit_breaker.state.consecutive_losses == 2
        can_trade, _ = t.circuit_breaker.can_open_trade()
        assert can_trade is True
        # Next loss (post-restart, in-memory) completes the streak → triggers
        triggered = t.circuit_breaker.record_loss(-5.0)
        assert triggered is True
        assert t.circuit_breaker.is_active is True

    def test_no_losses_default_unblocked(self, tmp_path, monkeypatch):
        """Control: loss-free history → trader starts unblocked (legacy
        default path — protects Real-A and any account without a streak)."""
        db_path = _seed_db(tmp_path, losses=0)
        t = _make_trader(db_path, monkeypatch)
        assert t.circuit_breaker.state.consecutive_losses == 0
        can_trade, reason = t.circuit_breaker.can_open_trade()
        assert can_trade is True and reason == "OK"