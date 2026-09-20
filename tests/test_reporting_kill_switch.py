"""Regression: REPORTING_ENABLED master kill-switch must silence ALL reporting.

Context (2026-09-20): doctorboyz ordered all reporting cut from Hermes and
Telegram while re-planning — "ยังไม่ต้องทำรายงานอะไร ตัดการรายงานออกจาก hermes
และ telegram ทั้งหมดก่อน". REPORTING_ENABLED defaults to OFF, so a fresh
deploy (no env) must not send anything, and ψ/outbox must not gain files
Hermes would read as reports.

These tests pin the gate on both outbound surfaces:
1. broky.performance.reporter.save_vault_report — the ψ/outbox channel
2. scripts.portfolio_manager._send_alerts — the Telegram channel
"""

from pathlib import Path

import pytest


@pytest.fixture()
def _env_reporting_on(monkeypatch):
    monkeypatch.setenv("REPORTING_ENABLED", "1")


@pytest.fixture()
def _env_reporting_off(monkeypatch):
    monkeypatch.delenv("REPORTING_ENABLED", raising=False)


class TestVaultReportKillSwitch:
    def test_disabled_by_default_writes_nothing(self, tmp_path, _env_reporting_off):
        from broky.performance.reporter import save_vault_report

        outbox = tmp_path / "ψ" / "outbox"
        save_vault_report("REPORT BODY", "2026-09-20", psi_root=tmp_path / "ψ")

        # No outbox dir was even created — nothing for Hermes to read.
        assert not outbox.exists()

    def test_enabled_writes_report(self, tmp_path, _env_reporting_on):
        from broky.performance.reporter import save_vault_report

        path = save_vault_report("REPORT BODY", "2026-09-20", psi_root=tmp_path / "ψ")

        assert path.read_text(encoding="utf-8") == "REPORT BODY"

    def test_explicit_zero_also_blocks(self, tmp_path, monkeypatch):
        monkeypatch.setenv("REPORTING_ENABLED", "0")
        from broky.performance.reporter import save_vault_report

        outbox = tmp_path / "ψ" / "outbox"
        save_vault_report("REPORT BODY", "2026-09-20", psi_root=tmp_path / "ψ")

        assert not outbox.exists()


class TestPortfolioManagerAlertKillSwitch:
    """_send_alerts must skip even when a Telegram token IS configured."""

    def test_disabled_skips_with_token_present(self, monkeypatch, _env_reporting_off):
        import sys

        repo_root = Path(__file__).parent.parent
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))

        from scripts.portfolio_manager import _send_alerts

        monkeypatch.setattr(
            "metty.notify.telegram_bot.TelegramNotifier.send",
            lambda self, text: pytest.fail("send() must not be called when reporting disabled"),
        )

        # Must not raise, must not call send().
        _send_alerts("PORTFOLIO ALERT\nP1 FROZEN: test")

    def test_enabled_attempts_send(self, monkeypatch, _env_reporting_on):
        import sys

        repo_root = Path(__file__).parent.parent
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))

        from scripts.portfolio_manager import _send_alerts

        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "test-chat")
        called = {}
        monkeypatch.setattr(
            "metty.notify.telegram_bot.TelegramNotifier",
            lambda token, chat_id: type("Fake", (), {"send": lambda self, text: called.setdefault("sent", text) or True})(),
        )

        _send_alerts("PORTFOLIO ALERT\nP1 FROZEN: test")

        assert called.get("sent")