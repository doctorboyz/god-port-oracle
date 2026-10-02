"""Causal tests for the persistent PF monitor (ISSUE-094).

The monitor replaces a session-only Claude CronCreate job that died with
every session. These tests lock the behavioral contract against fixture
sqlite DBs — no live VPS reads/writes during development:

    - cutoff filter (mixing pre-RR-fix trades fakes the PF — learning 2026-09-24)
    - PF/WR math (gross_loss=0 → PF None, rendered as —)
    - anomaly rules: PF<0.5 / no-trade>24h (weekend-suppressed) / balance -5%
    - Telegram gating: only on anomaly, REPORTING_ENABLED master switch,
      missing token → quiet, --always heartbeat, --no-telegram
    - missing DB → graceful exit 0 (a cron job must never crash)
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import pf_monitor  # noqa: E402

SCHEMA = """
CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE live_trades (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    pnl REAL,
    exit_time TEXT,
    is_open INTEGER NOT NULL DEFAULT 1
);
"""


def _make_db(path: Path, rows: list[tuple]) -> None:
    """rows: (account, timestamp, pnl, exit_time, is_open)."""
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)
    for i, name in enumerate(["B", "C", "D"]):
        conn.execute("INSERT INTO accounts (id, name) VALUES (?, ?)", (i + 1, name))
    for r in rows:
        conn.execute(
            "INSERT INTO live_trades (account_id, timestamp, pnl, exit_time, is_open) "
            "VALUES ((SELECT id FROM accounts WHERE name=?), ?, ?, ?, ?)",
            r,
        )
    conn.commit()
    conn.close()


@pytest.fixture()
def db(tmp_path):
    def build(rows):
        p = tmp_path / "oracle_train.db"
        if p.exists():
            p.unlink()
        _make_db(p, rows)
        return str(p)
    return build


class TestCollectStats:
    def test_cutoff_excludes_pre_fix_trades(self, db):
        """Pre-cutoff trades must not leak into the stats (PF-faking class)."""
        path = db([
            ("B", "2026-09-23T18:59", 100.0, "2026-09-23T19:30", 0),  # pre-cutoff
            ("B", "2026-09-23T19:01", -10.0, "2026-09-23T20:00", 0),  # post-cutoff
        ])
        stats = pf_monitor.collect_stats(path, ["B"], "2026-09-23T19:00")
        assert stats["B"]["trades"] == 1
        assert stats["B"]["pnl_sum"] == -10.0

    def test_pf_math_and_zero_loss(self, db):
        """PF = gross_win/gross_loss; no losses → None (rendered as —)."""
        path = db([
            ("B", "2026-09-24T01:00", 30.0, "2026-09-24T02:00", 0),
            ("B", "2026-09-24T03:00", -10.0, "2026-09-24T04:00", 0),
            ("C", "2026-09-24T01:00", 15.0, "2026-09-24T02:00", 0),
            ("C", "2026-09-24T03:00", 25.0, "2026-09-24T04:00", 0),
            ("D", "2026-09-24T03:00", None, None, 1),  # open trade: not in closed stats
        ])
        stats = pf_monitor.collect_stats(path, ["B", "C", "D"], "2026-09-23T19:00")
        assert stats["B"]["pf"] == 3.0
        assert stats["B"]["wr"] == 0.5
        assert stats["C"]["pf"] is None  # no losses
        assert stats["D"]["trades"] == 0
        assert stats["D"]["open"] == 1


class TestAnomalies:
    def test_low_pf_trips_alert(self):
        stats = {"B": {"trades": 10, "wins": 1, "pnl_sum": -50.0, "gross_win": 10.0,
                       "gross_loss": 100.0, "open": 0, "last_trade_ts": None,
                       "wr": 0.1, "pf": 0.1}}
        now = datetime(2026, 10, 2, 5, 0, tzinfo=timezone.utc)  # Friday
        alerts = pf_monitor.evaluate_anomalies(stats, {"B": None}, {}, now)
        assert len(alerts) == 1
        assert "[B]" in alerts[0]
        assert "PF=0.10" in alerts[0]

    def test_healthy_stats_no_alert(self):
        stats = {"B": {"trades": 50, "wins": 30, "pnl_sum": 40.0, "gross_win": 60.0,
                       "gross_loss": 20.0, "open": 0,
                       "last_trade_ts": datetime(2026, 10, 2, 4, 0, tzinfo=timezone.utc),
                       "wr": 0.6, "pf": 3.0}}
        now = datetime(2026, 10, 2, 5, 0, tzinfo=timezone.utc)
        alerts = pf_monitor.evaluate_anomalies(stats, {"B": 1000.0}, {}, now)
        assert alerts == []

    def test_no_trade_over_24h_trips_alert_on_open_market(self):
        stats = {"B": {"trades": 10, "wins": 5, "pnl_sum": 0.0, "gross_win": 50.0,
                       "gross_loss": 50.0, "open": 0,
                       "last_trade_ts": datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc),
                       "wr": 0.5, "pf": 1.0}}
        now = datetime(2026, 10, 2, 5, 0, tzinfo=timezone.utc)  # Friday, 29h later
        alerts = pf_monitor.evaluate_anomalies(stats, {"B": None}, {}, now)
        assert any("ไม่มีไม้ใหม่" in a for a in alerts)

    def test_no_trade_alert_suppressed_on_weekend(self):
        """Sat/Sun the market is closed — no-trade is normal, not an anomaly."""
        stats = {"B": {"trades": 10, "wins": 5, "pnl_sum": 0.0, "gross_win": 50.0,
                       "gross_loss": 50.0, "open": 0,
                       "last_trade_ts": datetime(2026, 10, 2, 5, 0, tzinfo=timezone.utc),
                       "wr": 0.5, "pf": 1.0}}
        sat = datetime(2026, 10, 3, 5, 0, tzinfo=timezone.utc)  # Saturday
        assert pf_monitor.evaluate_anomalies(stats, {"B": None}, {}, sat) == []

    def test_balance_drop_over_5pct_trips_alert(self):
        stats = {"B": {"trades": 50, "wins": 30, "pnl_sum": 40.0, "gross_win": 60.0,
                       "gross_loss": 20.0, "open": 0,
                       "last_trade_ts": datetime(2026, 10, 2, 4, 0, tzinfo=timezone.utc),
                       "wr": 0.6, "pf": 3.0}}
        now = datetime(2026, 10, 2, 5, 0, tzinfo=timezone.utc)
        prev = {"accounts": {"B": {"balance": 1000.0}}}
        alerts = pf_monitor.evaluate_anomalies(stats, {"B": 900.0}, prev, now)
        assert any("balance ลด" in a for a in alerts)
        # A 1% wiggle must NOT alert.
        quiet = pf_monitor.evaluate_anomalies(
            stats, {"B": 990.0}, {"accounts": {"B": {"balance": 1000.0}}}, now)
        assert quiet == []


class TestTelegramGating:
    def test_anomaly_sends_when_enabled(self, monkeypatch, db):
        """Full main() run: anomaly + REPORTING_ENABLED=1 → TelegramNotifier.send called."""
        path = db([
            ("B", "2026-09-24T01:00", 5.0, "2026-09-24T02:00", 0),
            ("B", "2026-09-24T03:00", -100.0, "2026-09-24T04:00", 0),  # PF = 0.05
        ])
        sent = []
        monkeypatch.setattr(
            "metty.notify.telegram_bot.TelegramNotifier.send",
            lambda self, text: sent.append(text) or True,
        )
        monkeypatch.setenv("REPORTING_ENABLED", "1")
        monkeypatch.setenv("TG_BOT_TOKEN", "t")
        monkeypatch.setenv("TG_CHAT_ID", "c")
        rc = pf_monitor.main(["--db-path", path, "--accounts", "B",
                              "--state-file", path + ".state.json"])
        assert rc == 0
        assert len(sent) == 1
        assert "[B]" in sent[0]

    def test_healthy_run_sends_nothing(self, monkeypatch, db):
        path = db([
            ("B", "2026-09-24T01:00", 100.0, "2026-09-24T02:00", 0),
        ])
        sent = []
        monkeypatch.setattr(
            "metty.notify.telegram_bot.TelegramNotifier.send",
            lambda self, text: sent.append(text) or True,
        )
        monkeypatch.setenv("REPORTING_ENABLED", "1")
        monkeypatch.setenv("TG_BOT_TOKEN", "t")
        monkeypatch.setenv("TG_CHAT_ID", "c")
        rc = pf_monitor.main(["--db-path", path, "--accounts", "B",
                              "--now", "2026-09-24T12:00+00:00",
                              "--state-file", path + ".state.json"])
        assert rc == 0
        assert sent == []

    def test_reporting_disabled_sends_nothing_even_on_anomaly(self, monkeypatch, db):
        path = db([
            ("B", "2026-09-24T01:00", 5.0, "2026-09-24T02:00", 0),
            ("B", "2026-09-24T03:00", -100.0, "2026-09-24T04:00", 0),
        ])
        sent = []
        monkeypatch.setattr(
            "metty.notify.telegram_bot.TelegramNotifier.send",
            lambda self, text: sent.append(text) or True,
        )
        monkeypatch.setenv("REPORTING_ENABLED", "0")
        monkeypatch.setenv("TG_BOT_TOKEN", "t")
        monkeypatch.setenv("TG_CHAT_ID", "c")
        rc = pf_monitor.main(["--db-path", path, "--accounts", "B"])
        assert rc == 0
        assert sent == []

    def test_missing_token_sends_nothing_on_anomaly(self, monkeypatch, db):
        path = db([
            ("B", "2026-09-24T01:00", 5.0, "2026-09-24T02:00", 0),
            ("B", "2026-09-24T03:00", -100.0, "2026-09-24T04:00", 0),
        ])
        sent = []
        monkeypatch.setattr(
            "metty.notify.telegram_bot.TelegramNotifier.send",
            lambda self, text: sent.append(text) or True,
        )
        monkeypatch.setenv("REPORTING_ENABLED", "1")
        monkeypatch.delenv("TG_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        rc = pf_monitor.main(["--db-path", path, "--accounts", "B"])
        assert rc == 0
        assert sent == []

    def test_always_flag_sends_heartbeat(self, monkeypatch, db):
        path = db([("B", "2026-09-24T01:00", 100.0, "2026-09-24T02:00", 0)])
        sent = []
        monkeypatch.setattr(
            "metty.notify.telegram_bot.TelegramNotifier.send",
            lambda self, text: sent.append(text) or True,
        )
        monkeypatch.setenv("REPORTING_ENABLED", "1")
        monkeypatch.setenv("TG_BOT_TOKEN", "t")
        monkeypatch.setenv("TG_CHAT_ID", "c")
        rc = pf_monitor.main(["--db-path", path, "--accounts", "B", "--always",
                              "--now", "2026-09-24T12:00+00:00",
                              "--state-file", path + ".state.json"])
        assert rc == 0
        assert len(sent) == 1
        assert "ปกติ" in sent[0]

    def test_no_telegram_flag_suppresses_send(self, monkeypatch, db):
        path = db([
            ("B", "2026-09-24T01:00", 5.0, "2026-09-24T02:00", 0),
            ("B", "2026-09-24T03:00", -100.0, "2026-09-24T04:00", 0),
        ])
        sent = []
        monkeypatch.setattr(
            "metty.notify.telegram_bot.TelegramNotifier.send",
            lambda self, text: sent.append(text) or True,
        )
        monkeypatch.setenv("REPORTING_ENABLED", "1")
        monkeypatch.setenv("TG_BOT_TOKEN", "t")
        monkeypatch.setenv("TG_CHAT_ID", "c")
        rc = pf_monitor.main(["--db-path", path, "--accounts", "B", "--no-telegram"])
        assert rc == 0
        assert sent == []


class TestRobustness:
    def test_missing_db_exits_zero(self, tmp_path):
        rc = pf_monitor.main(["--db-path", str(tmp_path / "nope.db")])
        assert rc == 0

    def test_state_file_written_for_next_run(self, db):
        """The balance-drop rule needs cross-run state — verify it persists."""
        path = db([("B", "2026-09-24T01:00", 100.0, "2026-09-24T02:00", 0)])
        state = path + ".state.json"
        pf_monitor.main(["--db-path", path, "--accounts", "B", "--no-telegram",
                         "--now", "2026-09-24T12:00+00:00",
                         "--state-file", state])
        data = json.loads(Path(state).read_text())
        assert "accounts" in data and "B" in data["accounts"]