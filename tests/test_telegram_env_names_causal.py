"""Causal proof test: portfolio_manager must read the Telegram env names the
composes actually set (ISSUE-108).

Hypothesis
----------
Every compose file (paper-farm, vps ×4 services) sets TG_BOT_TOKEN /
TG_CHAT_ID, but scripts/portfolio_manager.py:247-248 reads
TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID — a name nothing anywhere sets (the
same env-name-mismatch class as the margin-gate MT5_LEVERAGE bug, ISSUE-100).
Result: _send_alerts logs "Telegram not configured" even on a fully
configured host — a silent no-op.

Fix contract (same as scripts/pf_monitor.py:274-275): read TG_* first, fall
back to the legacy TELEGRAM_* names so old local setups keep working.

Causal proof
------------
TG_* set + TELEGRAM_* absent → _send_alerts must ATTEMPT the send
(RED before the fix: it skipped). Controls: legacy TELEGRAM_* names still
work; TG_* wins when both are present.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

repo_root = Path(__file__).parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))


@pytest.fixture()
def _reporting_on(monkeypatch):
    monkeypatch.setenv("REPORTING_ENABLED", "1")
    for name in ("TG_BOT_TOKEN", "TG_CHAT_ID",
                 "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(name, raising=False)


def _record_send(monkeypatch):
    """Replace TelegramNotifier with a recorder; return its call dict."""
    calls: dict = {}
    monkeypatch.setattr(
        "metty.notify.telegram_bot.TelegramNotifier",
        lambda token, chat_id: calls.update(token=token, chat_id=chat_id) or
        type("Fake", (), {"send": lambda self, text: calls.update(sent=text)})(),
    )
    return calls


class TestTelegramEnvNamesCausal:
    """The compose names (TG_*) must reach the notifier."""

    def test_tg_names_only_attempt_send(self, monkeypatch, _reporting_on):
        from scripts.portfolio_manager import _send_alerts

        monkeypatch.setenv("TG_BOT_TOKEN", "tg-token-123")
        monkeypatch.setenv("TG_CHAT_ID", "tg-chat-42")
        calls = _record_send(monkeypatch)

        _send_alerts("PORTFOLIO ALERT\nP1 FROZEN: test")

        assert calls.get("sent") == "PORTFOLIO ALERT\nP1 FROZEN: test", (
            "every compose sets TG_* — _send_alerts must use them, not "
            "silently log 'Telegram not configured'"
        )
        assert calls.get("token") == "tg-token-123"
        assert calls.get("chat_id") == "tg-chat-42"

    def test_legacy_telegram_names_still_work(self, monkeypatch, _reporting_on):
        # Control: old local setups with TELEGRAM_* keep working (fallback)
        from scripts.portfolio_manager import _send_alerts

        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "legacy-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "legacy-chat")
        calls = _record_send(monkeypatch)

        _send_alerts("PORTFOLIO ALERT\nP1 FROZEN: test")

        assert calls.get("token") == "legacy-token"
        assert calls.get("chat_id") == "legacy-chat"

    def test_tg_names_win_over_legacy(self, monkeypatch, _reporting_on):
        from scripts.portfolio_manager import _send_alerts

        monkeypatch.setenv("TG_BOT_TOKEN", "tg-token")
        monkeypatch.setenv("TG_CHAT_ID", "tg-chat")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "legacy-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "legacy-chat")
        calls = _record_send(monkeypatch)

        _send_alerts("x")

        assert calls.get("token") == "tg-token", (
            "the names the composes set must take precedence"
        )