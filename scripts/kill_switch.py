"""Portfolio-level kill switch supervisor (FIA-style, above the engine).

doctorboyz order 2026-11-16 (via Hermes): the 3 in-engine risk layers
(per-trade SL · 3-loss CB cooldown · DD-freeze in live_trader.py) protect
one engine process; the missing FIA layer is an INDEPENDENT supervisor.
This is it — a new file only, zero changes to metty/ broky/ shared/
docker/mt5/ Dockerfile (the paper farm builds from the same tree).

ISSUE-010 phase 2 (2026-11-17, doctorboyz order via Hermes): ชั้นพิทักษ์เงิน
ต้องไม่พึ่ง bridge ที่มันปกป้อง. Production evidence: supervisor bridge
polling was the disturber of the engines' single shared terminal handle —
"stream has been closed" on all three engines (P1 11 / P2 9 / P3 8 since
08:00) and account_info via the supervisor failed 18/18; `docker stop
kill-switch` stopped the errors dead (0 in 7 minutes). Since then equity
and status come from the ENGINE DB (each engine stamps accounts.
current_equity + equity_ts every cycle — metty additive write):
  · equity            → accounts.current_equity (engine's own row)
  · dd_from_peak      → vs max(kill-state peak, accounts.peak_equity)
  · connectivity      → freshness of equity_ts (catches a dead ENGINE,
                        not just a dead bridge — better than any ping)
  · order_rate        → engine DB (as before)
The bridge is left for exactly two jobs: the tick_mid cross-check feeding
price_dev, and flatten when a trigger actually fires.

Triggers (env-tunable, defaults in brackets): daily_loss [3.0% vs start
of UTC day] · dd_from_peak [10.0% from high-water peak] · order_rate
[>20 trades/hour/account] · price_dev [>1.0% vs cross-account median] ·
connectivity [>10 min without a fresh engine equity stamp].

Group policies: DEMO/CENT = flatten + docker stop engine container + TG;
REAL = bridge flatten ONLY + persistent block + URGENT TG — no code
path stops/restarts/recreates a REAL container (Real-A iron rule).

KILL_SWITCH_DRY_RUN=1 (compose default) evaluates triggers and alerts
but never acts — the VPS dry-run period exercises the whole monitoring
path, handle-safety included, before arming.

THE BRIDGE LESSON ("bridge หนึ่งใบ = terminal handle เดียว"): the RPyC
bridge server's exposed_initialize()/exposed_shutdown() call the
PROCESS-GLOBAL mt5.initialize()/mt5.shutdown() — a cycling client that
disconnects via MT5Bridge kills the engine's handle (it took down
fetcher+engine once). HandleSafeBridge therefore NEVER calls either
method: it rides the engine's handle and drops only the socket.
TestHandleSafety fails if any supervisor operation ever calls them.

State (KILL_STATE_PATH JSON): per-account peak, UTC-day baseline,
last-good-data and fired-trigger records — trigger dedup survives
restarts; recovery is manual only (--reset <ACCOUNT>, runbook in
ψ/outbox/kill_switch_handoff_2026-11-16.md).

Env keys: KILL_SWITCH_DRY_RUN, KILL_POLL_SECONDS, KILL_STATE_PATH,
KILL_DEMO_ACCOUNTS, KILL_CENT_ACCOUNTS, KILL_REAL_ACCOUNTS,
KILL_DAILY_LOSS_PCT, KILL_DD_PCT, KILL_ORDER_RATE_PER_HOUR,
KILL_PRICE_DEV_PCT, KILL_CONNECTIVITY_MINUTES, per-account
KILL_DB_PATH_<name> / KILL_ENGINE_CONTAINER_<name>, repo-convention
MT5_BRIDGE_<name>_HOST/_PORT, TG_BOT_TOKEN | TELEGRAM_BOT_TOKEN and
TG_CHAT_ID | TELEGRAM_CHAT_ID.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import socket
import sqlite3
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rpyc  # noqa: E402  (runtime dep of the engine image already)

logger = logging.getLogger("kill_switch")

# MT5 order constants — mirror metty/bridge/client.py (the server contract).
TRADE_ACTION_DEAL = 1
ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1
TRADE_RETCODE_DONE = 10009
ORDER_TIME_GTC = 0
ORDER_FILLING_FOK = 0
MAX_DEVIATION_POINTS = 100  # ISSUE-047 value used by the engine's close path

# Price deviation needs a robust cross-account reference: with fewer than
# three reporting accounts the median is just someone else's feed.
MIN_REFERENCE_ACCOUNTS = 3

# Symbol candidates, mirroring metty/bridge/client.py SYMBOL_ALIASES —
# Exness serves XAUUSDm / XAUUSD / XAUUSDc depending on account type.
SYMBOL_CANDIDATES = ["XAUUSDm", "XAUUSD", "XAUUSD.i", "XAUUSDb", "XAUUSDc"]

TRIGGER_DAILY_LOSS = "daily_loss"
TRIGGER_DD_PEAK = "dd_from_peak"
TRIGGER_ORDER_RATE = "order_rate"
TRIGGER_PRICE_DEV = "price_dev"
TRIGGER_CONNECTIVITY = "connectivity"

GROUP_DEMO = "demo"
GROUP_CENT = "cent"
GROUP_REAL = "real"


# ─── Config ─────────────────────────────────────────────────────────────


def _env_float(key: str, default: str) -> float:
    raw = os.environ.get(key, default)
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key}={raw!r} is not a number (fail-closed boot)") from exc


def _env_int(key: str, default: str) -> int:
    raw = os.environ.get(key, default)
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key}={raw!r} is not an integer (fail-closed boot)") from exc


def _parse_account_list(raw: str) -> list[str]:
    names = [n.strip().upper() for n in (raw or "").split(",") if n.strip()]
    if len(names) != len(set(names)):
        raise ValueError(f"duplicate account in {raw!r} (fail-closed boot)")
    return names


def _env_dry_run() -> bool:
    """KILL_SWITCH_DRY_RUN: default ON (alert-only). Only an explicit 0
    arms the supervisor — unknown values fail the boot, never guess."""
    raw = os.environ.get("KILL_SWITCH_DRY_RUN", "1").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"KILL_SWITCH_DRY_RUN={raw!r} not understood (fail-closed boot)")


@dataclass(frozen=True)
class AccountTarget:
    name: str
    group: str                      # demo | cent | real
    bridge_host: str
    bridge_port: int
    db_path: Optional[str]          # engine DB (read-only mount) or None
    engine_container: Optional[str] # None → never stopped (real group)

    @property
    def stoppable(self) -> bool:
        """Only non-real groups may ever be stopped — and only when the
        compose actually wired a container name for the account."""
        return self.group != GROUP_REAL and self.engine_container is not None


@dataclass(frozen=True)
class KillConfig:
    dry_run: bool
    poll_seconds: int
    state_path: str
    daily_loss_pct: float
    dd_pct: float
    order_rate_per_hour: int
    price_dev_pct: float
    connectivity_minutes: int
    targets: tuple[AccountTarget, ...]

    @classmethod
    def from_env(cls) -> "KillConfig":
        groups = {
            GROUP_DEMO: _parse_account_list(os.environ.get("KILL_DEMO_ACCOUNTS", "B,C,D")),
            GROUP_CENT: _parse_account_list(os.environ.get("KILL_CENT_ACCOUNTS", "P1,P2,P3")),
            GROUP_REAL: _parse_account_list(os.environ.get("KILL_REAL_ACCOUNTS", "A")),
        }
        seen: set[str] = set()
        targets: list[AccountTarget] = []
        for group, names in groups.items():
            for name in names:
                if name in seen:
                    raise ValueError(
                        f"account {name} appears in more than one kill group "
                        "(fail-closed boot)"
                    )
                seen.add(name)
                host = os.environ.get(f"MT5_BRIDGE_{name}_HOST", f"mt5{name.lower()}")
                port = int(os.environ.get(f"MT5_BRIDGE_{name}_PORT", "8001"))
                # REAL group: KILL_ENGINE_CONTAINER_<name> is refused even if
                # set — there must be no way to wire a stop path for real money.
                container = os.environ.get(f"KILL_ENGINE_CONTAINER_{name}")
                if group == GROUP_REAL and container:
                    raise ValueError(
                        f"KILL_ENGINE_CONTAINER_{name} is set for a REAL-group "
                        "account — real containers are never stopped (fail-closed boot)"
                    )
                targets.append(AccountTarget(
                    name=name,
                    group=group,
                    bridge_host=host,
                    bridge_port=port,
                    db_path=os.environ.get(f"KILL_DB_PATH_{name}") or None,
                    engine_container=container,
                ))
        return cls(
            dry_run=_env_dry_run(),
            poll_seconds=max(_env_int("KILL_POLL_SECONDS", "30"), 5),
            state_path=os.environ.get(
                "KILL_STATE_PATH", "data/kill_switch_state.json"
            ),
            daily_loss_pct=_env_float("KILL_DAILY_LOSS_PCT", "3.0"),
            dd_pct=_env_float("KILL_DD_PCT", "10.0"),
            order_rate_per_hour=_env_int("KILL_ORDER_RATE_PER_HOUR", "20"),
            price_dev_pct=_env_float("KILL_PRICE_DEV_PCT", "1.0"),
            connectivity_minutes=_env_int("KILL_CONNECTIVITY_MINUTES", "10"),
            targets=tuple(targets),
        )


# ─── Handle-safe bridge client ──────────────────────────────────────────


# Retry policy for the remaining bridge reads (ISSUE-010 phase 1 keepers):
# transient blips get ONE retry after a jittered delay — never a hammering
# loop, and never a retry on order_send (an ambiguous order must never be
# re-sent). Jitter de-synchronizes reconnects across accounts.
RETRY_JITTER_RANGE = (0.2, 0.8)


def _sleep_retry_jitter() -> None:
    time.sleep(random.uniform(*RETRY_JITTER_RANGE))


def _describe_remote(obj) -> str:
    """What the bridge ACTUALLY returned, for the read-failure log
    (ISSUE-013): type(netref) on a netref can report the generic wrapper
    class, which told the operator nothing. repr() materializes over the
    wire and shows the remote object's real type and content — guarded,
    because a broken bridge can raise on repr too."""
    try:
        type_name = type(obj).__name__
    except Exception:
        type_name = "unknown-type"
    try:
        snippet = repr(obj)[:120]
    except Exception as exc:
        snippet = f"<repr failed: {exc}>"
    return f"type={type_name} repr={snippet!r}"


class HandleSafeBridge:
    """Read/flatten client for ONE MT5 bridge.

    NEVER calls exposed_initialize / exposed_shutdown — both mutate the
    process-global terminal handle owned by the engine's connection (the
    repo's "one bridge = one handle" lesson; see module docstring). The
    socket is dropped with conn.close() only. Netrefs are materialized to
    local dicts exactly once per response — no per-key netref round-trips
    in loops (same contract as metty/bridge/client.py).

    ISSUE-010 phase 2: equity/status NO LONGER comes from here — the
    supervisor reads the engine DB. This client is left with exactly the
    tick cross-check (price_dev) and the flatten action.
    """

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self._conn: Optional[rpyc.Connection] = None

    def _connect(self) -> rpyc.Connection:
        if self._conn is None:
            self._conn = rpyc.connect(
                self.host, self.port, config={"sync_request_timeout": 30}
            )
        return self._conn

    def close(self) -> None:
        """Drop the socket. Deliberately does NOT call root.shutdown()."""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def __enter__(self) -> "HandleSafeBridge":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- reads (retry-once + jitter, ISSUE-010 phase 1 keeper) ----------

    def positions(self) -> Optional[list[dict]]:
        """All open positions as local dicts, or None on bridge failure.

        Returns [] when the terminal reports no positions — None vs []
        is the bridge-unhealthy distinction (same contract as
        fetch_deal_history_sync in metty/bridge/client.py). A transient
        read failure gets ONE jittered retry: flatten is a money-safety
        action, and aborting it on a blip leaves positions open."""
        for attempt in (1, 2):
            try:
                netref_list = self._connect().root.positions_get()
                if netref_list is None:
                    return []
                if isinstance(netref_list, (str, bytes)) or \
                        not hasattr(netref_list, "__iter__"):
                    # non-list answer (a str IS iterable — catch it by type,
                    # not just by protocol) = THIS read failed for THIS
                    # bridge (server _to_dict fallthrough / error shape) —
                    # log what actually came back (ISSUE-013), drop the
                    # socket, treat as read-failed. Never dict() it.
                    logger.warning(
                        "[KS] bridge %s:%s positions_get non-list response "
                        "(%s) — treating as read-failed",
                        self.host, self.port, _describe_remote(netref_list),
                    )
                    self.close()
                    return None
                # one materialization pass — dict() per item, then local only
                return [dict(p) for p in netref_list]
            except Exception as exc:
                self.close()
                if attempt == 1:
                    _sleep_retry_jitter()
                    continue
                logger.warning("[KS] bridge %s:%s positions_get failed "
                               "after one retry: %s",
                               self.host, self.port, exc)
                return None
        return None  # unreachable — for type-checkers

    def tick_mid(self, symbol: str) -> Optional[float]:
        """(bid+ask)/2 for a symbol, or None if the terminal has no tick.

        Transient failure → ONE jittered retry (the read that feeds the
        price_dev cross-check must not die on a blip, and must not hammer
        the bridge either). A non-dict answer is logged with its real
        type/repr (ISSUE-013) and treated as read-failed."""
        for attempt in (1, 2):
            try:
                netref = self._connect().root.symbol_info_tick(symbol)
                if netref is None:
                    return None
                if not hasattr(netref, "keys"):
                    logger.warning(
                        "[KS] bridge %s:%s symbol_info_tick(%s) non-dict "
                        "response (%s) — treating as read-failed",
                        self.host, self.port, symbol,
                        _describe_remote(netref),
                    )
                    self.close()
                    return None
                tick = dict(netref)  # materialize ONCE
                bid, ask = float(tick.get("bid") or 0), float(tick.get("ask") or 0)
                if bid <= 0 or ask <= 0:
                    return None
                return (bid + ask) / 2.0
            except Exception:
                self.close()
                if attempt == 1:
                    _sleep_retry_jitter()
                    continue
                return None
        return None  # unreachable — for type-checkers

    # -- flatten --------------------------------------------------------

    def flatten(self) -> Optional[list[dict]]:
        """Close every open position on this terminal.

        Mirrors the engine's own close path (metty/bridge/client.py
        close_position): opposite-deal order_send with the position ticket,
        FOK filling, no SL/TP. Returns one result dict per position.

        Fail-loud contract (retro งาน 4 follow-up, 2026-11-17): positions()
        returns None on bridge failure — flatten() must NOT treat that as
        "nothing to close" (the old `if not positions:` silently returned
        [] and callers reported a clean '0 closed, 0 failed' while real
        positions could still be open). None = read-failed → flatten NOT
        confirmed, the account is NOT declared flat; [] = the terminal
        genuinely holds no positions (really flat).
        """
        positions = self.positions()
        if positions is None:
            logger.error("[KS] bridge %s:%s flatten ABORTED — positions "
                         "unreadable; CANNOT confirm the account is flat "
                         "(manual check required)", self.host, self.port)
            return None
        if not positions:
            return []
        results: list[dict] = []
        for pos in positions:  # local dicts — safe to loop
            results.append(self._close_one(pos))
        return results

    def _close_one(self, pos: dict) -> dict:
        ticket = pos.get("ticket")
        symbol = pos.get("symbol") or "XAUUSD"
        volume = float(pos.get("volume") or 0)
        pos_type = pos.get("type", 1)  # 0=BUY position → close with SELL
        close_type = ORDER_TYPE_SELL if int(pos_type) == 0 else ORDER_TYPE_BUY
        try:
            tick = self._connect().root.symbol_info_tick(symbol)
            tick = dict(tick) if tick is not None else {}
            price = float(tick.get("bid") or 0) if close_type == ORDER_TYPE_SELL \
                else float(tick.get("ask") or 0)
            if price <= 0:
                return {"ticket": ticket, "success": False,
                        "error": "no usable tick for close"}
            result = self._connect().root.order_send(
                TRADE_ACTION_DEAL, symbol, volume, close_type, price,
                0.0, 0.0, MAX_DEVIATION_POINTS, 234000,
                f"kill-switch-close-{ticket}", ORDER_TIME_GTC,
                ORDER_FILLING_FOK, ticket,
            )
            result = dict(result) if result is not None else {}
            ok = result.get("retcode") == TRADE_RETCODE_DONE
            return {"ticket": ticket, "success": ok,
                    "retcode": result.get("retcode")}
        except Exception as exc:
            logger.error("[KS] close position %s failed: %s", ticket, exc)
            self.close()
            return {"ticket": ticket, "success": False, "error": str(exc)}


def probe_tcp(host: str, port: int, timeout: float = 5.0) -> bool:
    """Plain TCP connect — bridge liveness WITHOUT an RPyC handshake.

    A ThreadedServer listener accepting+dropping a connection is harmless;
    this probe never speaks the protocol, so it can never touch the
    terminal handle (connectivity trigger must be the safest call we make).
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ─── Engine DB readers (read-only) ──────────────────────────────────────


def count_trades_last_hour(db_path: str, account_name: str) -> Optional[int]:
    """Trades the engine opened in the trailing 60 minutes, from its own
    SQLite DB (mounted :ro, opened mode=ro — WAL readers never block the
    engine writer). None = DB unreadable, trigger skipped, never guessed.
    """
    if not db_path:
        return None
    uri = f"file:{db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            row = conn.execute(
                "SELECT id FROM accounts WHERE name = ?", (account_name,)
            ).fetchone()
            if row is None:
                return None  # account not enrolled in this DB — skip
            count = conn.execute(
                "SELECT COUNT(*) FROM live_trades "
                "WHERE account_id = ? AND timestamp >= datetime('now', '-1 hour')",
                (row[0],),
            ).fetchone()[0]
            return int(count)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.warning("[KS] %s order-rate read failed (%s): %s",
                       account_name, db_path, exc)
        return None


def read_account_snapshot(db_path: str, account_name: str) -> Optional[dict]:
    """The engine's per-cycle equity snapshot row (ISSUE-010 phase 2) —
    the supervisor's ONLY equity/heartbeat view of the account:

        current_equity  what the engine last saw as its equity
        equity_ts       when the engine stamped it (the heartbeat)
        peak_equity     engine-side high-water mark (seed for dd_from_peak)

    Read-only mount, mode=ro — WAL readers never block the engine writer.
    None = DB unreadable or account not enrolled (or the engine DB is
    pre-phase-2 and has no snapshot columns): the trigger layer skips,
    never invents a reading.
    """
    if not db_path:
        return None
    uri = f"file:{db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            row = conn.execute(
                "SELECT current_equity, equity_ts, peak_equity "
                "FROM accounts WHERE name = ?",
                (account_name,),
            ).fetchone()
            if row is None:
                return None
            return {"equity": row[0], "equity_ts": row[1],
                    "peak_equity": row[2]}
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.warning("[KS] %s equity snapshot read failed (%s): %s",
                       account_name, db_path, exc)
        return None


def _equity_age_minutes(ts_raw, now: datetime) -> Optional[float]:
    """Age of an engine equity stamp in minutes, or None when the stamp
    is missing/unparseable (an unprovable heartbeat is NOT a fresh one)."""
    if not ts_raw:
        return None
    try:
        ts = datetime.fromisoformat(str(ts_raw))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now - ts).total_seconds() / 60.0


def snapshot_schema_problem(db_path: str) -> Optional[str]:
    """None = OK to arm against this DB. Else a human reason this DB cannot
    serve phase-2 equity reads.

    Cross-context review HIGH (2026-11-17), reproduced empirically: an
    ARMED phase-2 supervisor pointed at a PRE-phase-2 engine DB reads
    'no such column: current_equity' on every account, sees every engine
    as dead, and flattens LIVE positions on the first cycle over a
    deploy-order mistake. The runbook says engines-before-kill-switch;
    this guard makes the wrong order a LOUD refusal instead of a kill
    (same fail-closed boot convention as _env_dry_run). A DB file that
    does not exist yet is NOT a schema problem (engine simply not
    started — its connectivity semantics are real), and an unreadable DB
    is left to alarm at runtime rather than block boot."""
    if not db_path:
        return None
    if not Path(db_path).exists():
        return None  # engine not started yet — not a schema problem
    uri = f"file:{db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            cols = {row[1] for row in
                    conn.execute("PRAGMA table_info(accounts)")}
        finally:
            conn.close()
    except sqlite3.Error:
        return None  # unreadable right now ≠ proven missing schema
    if "current_equity" not in cols or "equity_ts" not in cols:
        return (
            f"{db_path}: accounts table lacks current_equity/equity_ts — "
            "the engine image predates phase 2; rebuild the engines first "
            "(deploy order: engines BEFORE kill-switch)"
        )
    return None


# ─── Docker stop (Engine API over the unix socket, stdlib only) ──────────


class DockerStopClient:
    """Stops engine containers via the Docker Engine API.

    The engine image ships no docker CLI and the Dockerfile is untouchable
    (paper-farm shares it), so this speaks HTTP over /var/run/docker.sock
    with the standard library only. Only ONE method exists — stop. There
    is intentionally no start/restart/remove: those belong to the human.
    """

    def __init__(self, sock_path: str = "/var/run/docker.sock", timeout: float = 15.0):
        self.sock_path = sock_path
        self.timeout = timeout

    def stop(self, container: str, t: int = 30) -> dict:
        import http.client
        import urllib.parse

        class _UnixHTTPConnection(http.client.HTTPConnection):
            def __init__(self, path, timeout_):
                super().__init__("localhost", timeout=timeout_)
                self._unix_path = path

            def connect(self):  # pragma: no cover - exercised via mocks
                import socket as _socket
                s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
                s.settimeout(self.timeout)
                s.connect(self._unix_path)
                self.sock = s

        path = f"/containers/{urllib.parse.quote(container, safe='')}/stop?t={t}"
        try:
            conn = _UnixHTTPConnection(self.sock_path, self.timeout)
            conn.request("POST", path)
            resp = conn.getresponse()
            resp.read()
            conn.close()
            if resp.status in (204, 304):   # stopped, or already stopped
                return {"success": True, "status": resp.status}
            return {"success": False, "status": resp.status,
                    "error": f"docker API HTTP {resp.status}"}
        except Exception as exc:
            return {"success": False, "error": str(exc)}


# ─── Persistent state ────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class KillState:
    """JSON-file state: per-account peak / day baseline / fired triggers.

    The trigger records ARE the dedup: a trigger present in state never
    fires again (across restarts too). Recovery is manual (--reset).
    """

    def __init__(self, path: str):
        self.path = Path(path)
        self.data: dict = {"version": 1, "accounts": {}}

    def load(self) -> "KillState":
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("accounts"), dict):
                self.data = raw
            else:
                raise ValueError("bad state shape")
        except FileNotFoundError:
            pass  # first boot
        except Exception as exc:
            # Corrupt state = lost baselines. Fail loud, start fresh:
            # a kill switch that cannot trust its state still monitors.
            logger.error("[KS] state %s unreadable (%s) — starting fresh",
                         self.path, exc)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return self

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)  # atomic — crash never truncates state

    def account(self, name: str) -> dict:
        return self.data["accounts"].setdefault(name, {})

    def has_trigger(self, name: str, trigger: str) -> bool:
        return trigger in (self.account(name).get("triggers") or {})

    def record_trigger(self, name: str, trigger: str, reason: str,
                       details: dict) -> None:
        acct = self.account(name)
        acct.setdefault("triggers", {})[trigger] = {
            "fired_at": _now_iso(), "reason": reason, "details": details,
        }

    def clear_account(self, name: str) -> None:
        """Manual recovery (--reset): forget triggers + baselines."""
        self.data["accounts"].pop(name, None)


# ─── Trigger evaluation ──────────────────────────────────────────────────


@dataclass(frozen=True)
class Trigger:
    type: str
    reason: str
    details: dict = field(default_factory=dict)


@dataclass
class Observation:
    """One account's reading for one cycle.

    equity/equity_ts/db_peak come from the ENGINE DB (the engine stamps
    them every cycle); mid is the bridge tick cross-check — the only
    bridge read left. data_ok means a FRESH usable equity arrived (the
    stamp is inside the connectivity window), not just any row value."""
    target: AccountTarget
    equity: Optional[float] = None      # accounts.current_equity (engine stamp)
    equity_ts: Optional[str] = None    # ISO UTC of the engine stamp
    db_peak: Optional[float] = None    # accounts.peak_equity (engine high-water)
    mid: Optional[float] = None
    tcp_ok: bool = False
    data_ok: bool = False              # fresh usable equity arrived this cycle
    opened_last_hour: Optional[int] = None


def _observe(target: AccountTarget, now: datetime,
             connectivity_minutes: int) -> Observation:
    """Read one account: engine DB first (equity/peak/heartbeat), then the
    bridge tick cross-check.

    ISSUE-010 phase 2: equity/status come from the engine's own DB row —
    the supervisor must not depend on the bridge it protects (supervisor
    polling disturbed the engines' single shared terminal handle:
    "stream has been closed" ×28, account_info via supervisor 18/18
    failed, VPS 2026-11-17). A dead bridge does NOT blind the supervisor:
    the DB is a volume mount, so the last-known equity and its heartbeat
    are still read. The bridge is touched only for the price_dev tick."""
    obs = Observation(target=target)
    obs.tcp_ok = probe_tcp(target.bridge_host, target.bridge_port)

    snap = read_account_snapshot(target.db_path or "", target.name)
    if snap is not None:
        try:
            equity = float(snap.get("equity") or 0)
        except (TypeError, ValueError):
            equity = 0.0
        if equity > 0:
            obs.equity = equity
        obs.equity_ts = snap.get("equity_ts") or None
        try:
            obs.db_peak = float(snap.get("peak_equity") or 0) or None
        except (TypeError, ValueError):
            obs.db_peak = None
        # fresh = the engine stamped inside the connectivity window; a
        # stale stamp keeps the last-known equity for the alert context
        # but is NOT usable data — the engine is dead, connectivity fires.
        age = _equity_age_minutes(obs.equity_ts, now)
        obs.data_ok = obs.equity is not None and age is not None \
            and age <= connectivity_minutes

    if not obs.tcp_ok:
        return obs  # bridge unreachable — tick skipped, DB already read
    with HandleSafeBridge(target.bridge_host, target.bridge_port) as bridge:
        # engine-seen price = this bridge's own tick for its symbol variant
        symbols = [os.environ.get(f"MT5_SYMBOL_{target.name}", "")] \
            + SYMBOL_CANDIDATES
        for symbol in [s for s in symbols if s]:
            obs.mid = bridge.tick_mid(symbol)
            if obs.mid is not None:
                break
    return obs


def evaluate_triggers(
    config: KillConfig,
    state: KillState,
    obs_by_name: dict[str, Observation],
    now: datetime,
) -> list[tuple[Observation, Trigger]]:
    """All five triggers, evaluated independently. Missing data SKIPS a
    trigger (never invents a reading) — the connectivity trigger is the
    one that catches "data never arrives"."""
    fired: list[tuple[Observation, Trigger]] = []
    mids = [o.mid for o in obs_by_name.values() if o.mid is not None]
    reference = statistics.median(mids) if len(mids) >= MIN_REFERENCE_ACCOUNTS else None

    for name, obs in obs_by_name.items():
        acct = state.account(name)

        # ── baselines (only from usable data) ──
        if obs.data_ok:
            today = now.strftime("%Y-%m-%d")
            day = acct.get("day") or {}
            if day.get("date") != today:
                acct["day"] = {"date": today, "start_equity": obs.equity}
                logger.info("[KS] %s day baseline set: %.2f", name, obs.equity)
            # high-water = max of the supervisor's own memory, the
            # engine-side peak (accounts.peak_equity, maintained by
            # portfolio_manager) and the current equity — never lowered.
            peak = max(
                float(acct.get("peak_equity") or 0.0),
                float(obs.db_peak or 0.0),
                obs.equity,
            )
            acct["peak_equity"] = peak
            # the heartbeat anchor is the ENGINE's own stamp time
            acct["last_good_data"] = obs.equity_ts or _now_iso()
        else:
            peak = float(acct.get("peak_equity") or 0.0)

        if state.has_trigger(name, TRIGGER_DAILY_LOSS) and \
           state.has_trigger(name, TRIGGER_DD_PEAK) and \
           state.has_trigger(name, TRIGGER_ORDER_RATE) and \
           state.has_trigger(name, TRIGGER_PRICE_DEV) and \
           state.has_trigger(name, TRIGGER_CONNECTIVITY):
            continue  # every trigger already fired — nothing new to check

        # ── daily loss ──
        day = acct.get("day") or {}
        start_equity = float(day.get("start_equity") or 0)
        if obs.data_ok and start_equity > 0:
            loss_pct = (start_equity - obs.equity) / start_equity * 100.0
            if loss_pct >= config.daily_loss_pct and \
               not state.has_trigger(name, TRIGGER_DAILY_LOSS):
                fired.append((obs, Trigger(
                    TRIGGER_DAILY_LOSS,
                    f"daily loss {loss_pct:.2f}% >= {config.daily_loss_pct}% "
                    f"(start {start_equity:.2f} → equity {obs.equity:.2f})",
                    {"loss_pct": round(loss_pct, 2), "start": start_equity,
                     "equity": obs.equity},
                )))

        # ── drawdown from high-water peak ──
        if obs.data_ok and peak > 0:
            dd_pct = (peak - obs.equity) / peak * 100.0
            if dd_pct >= config.dd_pct and not state.has_trigger(name, TRIGGER_DD_PEAK):
                fired.append((obs, Trigger(
                    TRIGGER_DD_PEAK,
                    f"drawdown {dd_pct:.2f}% from peak {peak:.2f} "
                    f">= {config.dd_pct}% (equity {obs.equity:.2f})",
                    {"dd_pct": round(dd_pct, 2), "peak": peak,
                     "equity": obs.equity},
                )))

        # ── order-rate anomaly (engine runaway loop) ──
        if not state.has_trigger(name, TRIGGER_ORDER_RATE):
            obs.opened_last_hour = count_trades_last_hour(
                obs.target.db_path or "", name)
            if obs.opened_last_hour is not None and \
               obs.opened_last_hour > config.order_rate_per_hour:
                fired.append((obs, Trigger(
                    TRIGGER_ORDER_RATE,
                    f"{obs.opened_last_hour} trades opened in the last hour "
                    f"> {config.order_rate_per_hour} — engine loop suspected",
                    {"opened_last_hour": obs.opened_last_hour},
                )))

        # ── price deviation vs cross-account median ──
        if obs.mid is not None and reference is not None and \
           not state.has_trigger(name, TRIGGER_PRICE_DEV):
            dev_pct = abs(obs.mid - reference) / reference * 100.0
            if dev_pct > config.price_dev_pct:
                fired.append((obs, Trigger(
                    TRIGGER_PRICE_DEV,
                    f"price {obs.mid:.2f} deviates {dev_pct:.2f}% from "
                    f"cross-account median {reference:.2f} "
                    f"> {config.price_dev_pct}%",
                    {"mid": obs.mid, "reference": reference,
                     "dev_pct": round(dev_pct, 2)},
                )))

        # ── connectivity: no usable data for the window ──
        if not state.has_trigger(name, TRIGGER_CONNECTIVITY):
            conn_trigger = _connectivity_trigger(config, state, obs, name, now)
            if conn_trigger is not None:
                fired.append((obs, conn_trigger))

    return fired


def _connectivity_trigger(
    config: KillConfig,
    state: KillState,
    obs: Observation,
    name: str,
    now: datetime,
) -> Optional[Trigger]:
    """Fires when the ENGINE stopped stamping equity for the whole window
    (ISSUE-010 phase 2: the heartbeat is accounts.equity_ts, which catches
    a dead ENGINE — not just a dead bridge, which is all a ping could see).

    "Never had a stamp" anchors at the supervisor boot time — the trigger
    arms `connectivity_minutes` after boot instead of firing instantly
    (a supervisor that kills on its own startup lag would be broken)."""
    acct = state.account(name)
    last_raw = obs.equity_ts or acct.get("last_good_data")
    last_ok: Optional[datetime] = None
    if last_raw:
        try:
            last_ok = datetime.fromisoformat(last_raw)
        except ValueError:
            last_ok = None
    if last_ok is not None and last_ok.tzinfo is None:
        last_ok = last_ok.replace(tzinfo=timezone.utc)
    if last_ok is None:
        last_ok = getattr(state, "_boot_time", now)
    silent_minutes = (now - last_ok).total_seconds() / 60.0
    if silent_minutes > config.connectivity_minutes:
        return Trigger(
            TRIGGER_CONNECTIVITY,
            f"no fresh equity from engine DB for {silent_minutes:.1f} min "
            f"> {config.connectivity_minutes} min "
            f"(tcp={'ok' if obs.tcp_ok else 'DOWN'})",
            {"silent_minutes": round(silent_minutes, 1),
             "tcp_ok": obs.tcp_ok},
        )
    return None


# ─── Supervisor ──────────────────────────────────────────────────────────


def _send_tg(text: str) -> None:
    """Telegram alert, best-effort. Same reader contract as
    scripts/portfolio_manager.py: TG_* first, legacy TELEGRAM_* fallback.
    Kill alerts are NOT gated on REPORTING_ENABLED — a kill switch that
    stays silent about its own triggers is broken by definition.
    """
    token = os.environ.get("TG_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TG_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        logger.info("[KS] Telegram not configured — alert skipped:\n%s", text)
        return
    try:
        from metty.notify.telegram_bot import TelegramNotifier
        TelegramNotifier(token, chat_id).send(text)
    except Exception as exc:
        logger.warning("[KS] Telegram send failed: %s", exc)


class KillSwitchSupervisor:
    def __init__(
        self,
        config: KillConfig,
        state: KillState,
        docker: Optional[DockerStopClient] = None,
    ):
        self.config = config
        self.state = state
        self.docker = docker or DockerStopClient()
        self._boot_time = datetime.now(timezone.utc)
        # connectivity "never had data" anchor (see evaluate_triggers)
        self.state._boot_time = self._boot_time
        # Fail-closed boot (cross-context review HIGH, 2026-11-17): an
        # armed supervisor that CANNOT read engine equity is a guaranteed
        # false mass-flatten waiting for one deploy-order mistake. DRY RUN
        # boots through the gap on purpose — it only alerts, and those
        # alerts are exactly how the operator notices the gap.
        if not config.dry_run:
            wired = [t for t in config.targets if t.db_path]
            if not wired:
                raise ValueError(
                    "ARMED kill switch with no KILL_DB_PATH_* wired — the "
                    "equity source is the engine DB (ISSUE-010 phase 2); "
                    "refusing to arm a supervisor whose every account would "
                    "read as a dead engine (fail-closed boot)"
                )
            for target in wired:
                problem = snapshot_schema_problem(target.db_path)
                if problem is not None:
                    raise ValueError(
                        "ARMED kill switch refusing to start — " + problem
                    )
        # hourly success-rate metric (ISSUE-010 phase 1 keeper): one log
        # line per account per hour — the operator's first answer to
        # "is the supervisor actually seeing this account?"
        self._hourly_key: Optional[str] = None
        self._hourly: dict[str, dict[str, int]] = {}

    # -- hourly success-rate metric -------------------------------------

    def _record_hourly(self, obs_by_name: dict[str, Observation],
                       now: datetime) -> None:
        """Count this cycle's read outcomes; on hour rollover flush ONE
        log line per account. DB reads = the equity snapshot read; tick =
        the only bridge read left. A tick the cycle never attempted
        (bridge TCP down) is not counted as a tick failure."""
        hour_key = now.strftime("%Y-%m-%dT%H")
        if self._hourly_key is None:
            self._hourly_key = hour_key
        elif hour_key != self._hourly_key:
            self._flush_hourly()
            self._hourly_key = hour_key
        for name, obs in obs_by_name.items():
            bucket = self._hourly.setdefault(
                name, {"db_ok": 0, "db_fail": 0, "tick_ok": 0, "tick_fail": 0}
            )
            if obs.equity is not None:
                bucket["db_ok"] += 1
            else:
                bucket["db_fail"] += 1
            if obs.mid is not None:
                bucket["tick_ok"] += 1
            elif obs.tcp_ok:
                bucket["tick_fail"] += 1

    def _flush_hourly(self) -> None:
        def rate(ok: int, total: int) -> str:
            if total == 0:
                return "n/a"
            return f"{ok * 100.0 / total:.1f}%"

        for name in sorted(self._hourly):
            b = self._hourly[name]
            db_total = b["db_ok"] + b["db_fail"]
            tick_total = b["tick_ok"] + b["tick_fail"]
            logger.info(
                "[KS] hourly %s: db %d/%d (%s) tick %d/%d (%s)",
                name, b["db_ok"], db_total, rate(b["db_ok"], db_total),
                b["tick_ok"], tick_total, rate(b["tick_ok"], tick_total),
            )
        self._hourly = {}

    # -- actions --------------------------------------------------------

    def _act(self, obs: Observation) -> dict:
        """Enforce the group policy for an account whose trigger(s) just
        fired. Runs ONCE per account per cycle (not per trigger — a
        double trigger must not double-stop the engine container).
        Returns action summary for the alert. DRY RUN does nothing."""
        target = obs.target
        if self.config.dry_run:
            return {"dry_run": True, "actions": [],
                    "note": "KILL_SWITCH_DRY_RUN=1 — alert only, nothing stopped"}
        actions: list[str] = []
        with HandleSafeBridge(target.bridge_host, target.bridge_port) as bridge:
            results = bridge.flatten()
            if results is None:
                # read-failed — NEVER report a clean flatten here: real
                # positions may still be open behind the broken read
                logger.error("[KS] %s flatten READ-FAILED — account NOT "
                             "confirmed flat", target.name)
                actions.append(
                    "flatten: READ-FAILED — bridge cannot report positions; "
                    "account NOT confirmed flat (manual check required)")
            else:
                closed = sum(1 for r in results if r.get("success"))
                failed = len(results) - closed
                actions.append(f"flatten: {closed} closed, {failed} failed")
        if target.stoppable:
            stop = self.docker.stop(target.engine_container)
            actions.append(
                f"docker stop {target.engine_container}: "
                + ("ok" if stop.get("success") else f"FAILED {stop}"))
        elif target.group == GROUP_REAL:
            actions.append("REAL group: container untouched (iron rule), "
                           "account marked BLOCKED — manual intervention required")
        return {"dry_run": False, "actions": actions, "note": ""}

    def _enforce_blocked(self, obs_by_name: dict[str, Observation]) -> None:
        """Armed mode only: accounts with ANY fired trigger stay enforced —
        any position they (re)open gets flattened again, silently (the
        alert fired once already; this is the block, not the alarm)."""
        if self.config.dry_run:
            return
        for name, obs in obs_by_name.items():
            acct = self.state.account(name)
            if not (acct.get("triggers") or {}):
                continue
            with HandleSafeBridge(obs.target.bridge_host,
                                  obs.target.bridge_port) as bridge:
                results = bridge.flatten()
                if results is None:
                    # read-failed — cannot verify the account is flat; say
                    # so instead of silently passing (the block stays, the
                    # VERIFICATION failed — the operator must look)
                    logger.warning("[KS] %s BLOCK-enforcement flatten "
                                   "read-failed — CANNOT verify the account "
                                   "is flat (manual check required)", name)
                    continue
                closed = sum(1 for r in results if r.get("success"))
            if closed:
                logger.info("[KS] %s BLOCK-enforcement flattened %d new "
                            "position(s)", name, closed)

    # -- cycle ----------------------------------------------------------

    def cycle(self, now: Optional[datetime] = None) -> list[dict]:
        """One poll cycle: observe everything, fire new triggers, enforce
        blocked accounts, persist state, alert.

        Actions run once per ACCOUNT (all its triggers in one policy pass);
        alerts go out as one message per account summarizing every trigger
        that fired this cycle."""
        now = now or datetime.now(timezone.utc)
        obs_by_name = {
            t.name: _observe(t, now, self.config.connectivity_minutes)
            for t in self.config.targets
        }
        self._record_hourly(obs_by_name, now)
        fired = evaluate_triggers(self.config, self.state, obs_by_name, now)

        # group triggers per account: state first, then one policy pass
        grouped: dict[str, tuple[Observation, list[Trigger]]] = {}
        for obs, trigger in fired:
            grouped.setdefault(obs.target.name, (obs, []))[1].append(trigger)

        alerts: list[dict] = []
        for name, (obs, triggers) in grouped.items():
            for trigger in triggers:
                self.state.record_trigger(name, trigger.type, trigger.reason,
                                           trigger.details)
            # state BEFORE actions — a crash mid-action never re-fires
            self.state.save()
            action = self._act(obs)
            alerts.append({
                "account": name, "group": obs.target.group,
                "triggers": [t.type for t in triggers],
                "reasons": [t.reason for t in triggers],
                **action,
            })
            urgent = obs.target.group == GROUP_REAL
            header = ("🚨 KILL SWITCH — URGENT (REAL MONEY)"
                      if urgent else "🛑 KILL SWITCH")
            if self.config.dry_run:
                header = "[DRY-RUN] " + header
            msg = [header,
                   f"Account: {name} (group {obs.target.group})"]
            for trigger in triggers:
                msg.append(f"Trigger: {trigger.type} — {trigger.reason}")
            if action["actions"]:
                msg.append("Actions: " + "; ".join(action["actions"]))
            if action["note"]:
                msg.append(action["note"])
            _send_tg("\n".join(msg))
            logger.error("[KS] TRIGGER %s %s | %s", name,
                         [t.type for t in triggers], action)

        self._enforce_blocked(obs_by_name)
        self.state.save()
        return alerts

    def run_forever(self) -> None:  # pragma: no cover - infinite loop
        logger.info("[KS] supervisor starting: dry_run=%s poll=%ss accounts=%s",
                    self.config.dry_run, self.config.poll_seconds,
                    ",".join(t.name for t in self.config.targets))
        while True:
            started = time.monotonic()
            try:
                self.cycle()
            except Exception as exc:  # supervisor must never die on one
                logger.exception("[KS] cycle failed: %s", exc)
            elapsed = time.monotonic() - started
            time.sleep(max(self.config.poll_seconds - elapsed, 1.0))


# ─── CLI ─────────────────────────────────────────────────────────────────


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Portfolio kill switch supervisor")
    parser.add_argument("--once", action="store_true",
                        help="run a single poll cycle and exit (cron/testing)")
    parser.add_argument("--reset", metavar="ACCOUNT",
                        help="MANUAL recovery: clear an account's kill state")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true",
                        default=None,
                        help="force dry-run for this run (overrides env)")
    parser.add_argument("--no-dry-run", dest="dry_run", action="store_false",
                        help="ARM for this run (overrides env)")
    parser.add_argument("--state", default=None,
                        help="state file path (default KILL_STATE_PATH)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    config = KillConfig.from_env()
    if args.dry_run is not None:
        # frozen dataclass — rebuild with the override
        config = KillConfig(
            dry_run=args.dry_run, poll_seconds=config.poll_seconds,
            state_path=args.state or config.state_path,
            daily_loss_pct=config.daily_loss_pct, dd_pct=config.dd_pct,
            order_rate_per_hour=config.order_rate_per_hour,
            price_dev_pct=config.price_dev_pct,
            connectivity_minutes=config.connectivity_minutes,
            targets=config.targets,
        )
    state = KillState(config.state_path).load()

    if args.reset:
        state.clear_account(args.reset.strip().upper())
        state.save()
        logger.info("[KS] manual reset: kill state for %s cleared — "
                    "supervisor will re-evaluate from fresh baselines",
                    args.reset.strip().upper())
        return 0

    supervisor = KillSwitchSupervisor(config, state)
    if args.once:
        alerts = supervisor.cycle()
        logger.info("[KS] once-cycle done: %d trigger(s) fired%s",
                    len(alerts), " (dry-run)" if config.dry_run else "")
        return 0
    supervisor.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
