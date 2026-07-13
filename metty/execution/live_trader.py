"""Live trader — generates signals from MT5 data and executes trades via the bridge.

Each cycle:
1. Fetch candles from MT5 bridge
2. Generate signal via Broky's weighted score system
3. Check risk (circuit breaker, calendar, existing position)
4. Calculate SL/TP/lots from ATR
5. Send order to MT5 (or log in dry-run mode)
6. Monitor open positions for exits
7. Log trade to SQLite
"""

from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from broky.data.calendar import fetch_calendar, should_avoid_trading
from broky.indicators.atr import calculate_atr
from broky.risk.circuit_breaker import CircuitBreaker
from broky.risk.drawdown_protection import DrawdownProtector, get_drawdown_config, get_buy_min_confidence
from broky.risk.trade_blocker import TradeBlocker, BlockInput
from metty.core.account_registry import get_display_name, get_account_config, get_bridge_config
from broky.risk.position_sizing import (
    calculate_position_size,
    calculate_stop_loss,
    calculate_take_profit,
)
from broky.risk.sizing import SIZING_METHODS, fixed_fraction_size, kelly_size, risk_per_trade_size, volatility_adjusted_size
from broky.signals.generator import generate_signal
from metty.core.db import (
    close_live_trade,
    get_latest_signal_id,
    get_open_trades,
    init_db,
    insert_live_trade,
    insert_rejected_signal,
    reconcile_closed_positions,
)
from shared.events import Event, EventBus, EventType
from shared.logging_utils import log_trade, log_signal, log_position, log_circuit_break
from shared.models import Signal, SignalType, TradingMode

logger = logging.getLogger(__name__)

# Account IDs in the database
ACCOUNT_IDS = {"A": 1, "B": 2, "C": 3, "D": 4}

# Risk per trade by account (conservative for demo)
ACCOUNT_RISK = {"A": 0.01, "B": 0.02, "C": 0.02, "D": 0.02}

# Contract size: 1 lot XAUUSD = 100 oz
CONTRACT_SIZE = 100.0

# ML filter circuit breaker: stop trading after N consecutive ML failures
ML_MAX_CONSECUTIVE_FAILS = 5


@dataclass
class RiskConfig:
    risk_per_trade: float = 0.02
    atr_multiplier: float = 2.5    # Account B default (was 2.0)
    risk_reward_ratio: float = 2.5  # Account B default
    min_confidence: float = 0.45    # Account B default
    max_holding_bars: int = 36  # 3 hours on M5
    cooldown_bars: int = 12  # 1 hour cooldown after exit
    spread_buffer: float = 2.0
    consecutive_loss_limit: int = 3
    daily_loss_limit_pct: float = 0.05
    bar_seconds: int = 300  # M5 = 300s, M1 = 60s
    sizing_method: str = "risk_per_trade"  # risk_per_trade, kelly, volatility_adjusted, fixed_fraction
    # Partial TP (Option C): close at TP1, open scale-in position
    partial_tp_enabled: bool = False  # Feature flag — must be explicitly enabled
    tp1_ratio: float = 0.5   # TP1 at 50% of TP distance from entry
    rr_scale_in: float = 2.5  # RR ratio for the scale-in position
    # ISSUE-052: trailing TP D 0.20/0.10 + 24h time stop — ported from backtest
    # (scripts/backtest_entry_trailing_block.py). Previously live had only SL/TP/3h max_holding.
    trailing_tp_enabled: bool = True   # Feature flag — matches system report section 2
    trailing_activation_pct: float = 0.20  # arm trailing after +0.20% from entry
    trailing_trail_pct: float = 0.10       # trail 0.10% below peak (BUY) / above trough (SELL)
    time_stop_bars: int = 288              # 24h on M5 (was max_holding_bars=36 = 3h)


class LiveTrader:
    """Live trading loop: fetch candles → generate signal → execute trade.

    Each account runs one position at a time. The trader checks:
    - Circuit breaker (consecutive losses, daily loss)
    - Calendar avoidance (high-impact news)
    - Existing positions (no double-entry)

    Usage:
        trader = LiveTrader(account="B", db_path="data/oracle.db")
        trader.run_once()  # Single cycle
        trader.run(interval=300, max_cycles=0)  # Continuous loop
    """

    def __init__(
        self,
        account: str = "B",
        db_path: Optional[Path] = None,
        data_dir: Optional[Path] = None,
        dry_run: bool = False,
        risk_config: Optional[RiskConfig] = None,
        event_bus: Optional[EventBus] = None,
        notifier: Optional["TelegramNotifier"] = None,
    ):
        self.account = account.upper()
        # ISSUE M3: fail loud on unknown account — silent fallback to account_id=3
        # routed Real-A trades to demo "C" in DB → kill switch reacted to demo PnL.
        if self.account not in ACCOUNT_IDS:
            raise ValueError(
                f"Unknown account '{self.account}' — must be one of {list(ACCOUNT_IDS)}. "
                f"Check ACCOUNT_NAME env var."
            )
        self.display_name = get_display_name(self.account)
        self.db_path = db_path
        self.data_dir = data_dir or Path("data/xau-data")
        self.dry_run = dry_run
        self.learning_mode = os.environ.get("LEARNING_MODE", "0") == "1"
        # ISSUE C3: LEARNING_MODE on Real-A silently bypasses CB/cooldown/calendar/ML-veto.
        # Hard-block — never allow learning mode on real money.
        if self.account == "A" and self.learning_mode and not self.dry_run:
            raise RuntimeError(
                "LEARNING_MODE=1 is forbidden on account 'A' (Real-A real money) — "
                "it bypasses CircuitBreaker/cooldown/calendar/ML-veto. "
                "Unset LEARNING_MODE or use a demo account."
            )
        # ISSUE H1: self.symbol was never set → _get_deal_history raised AttributeError,
        # caught silently → reconciliation skipped → exit prices fell back to entry (breakeven).
        # Use get_symbol_map so MT5_SYMBOL_{account} env override is respected.
        from metty.core.account_registry import get_symbol_map
        self.symbol = get_symbol_map().get(self.account, "XAUUSD")
        per_account_limits = {
            "A": int(os.environ.get("MAX_POSITIONS_A", os.environ.get("MAX_POSITIONS_PER_ACCOUNT", "5"))),
            "B": int(os.environ.get("MAX_POSITIONS_B", os.environ.get("MAX_POSITIONS_PER_ACCOUNT", "5"))),
            "C": int(os.environ.get("MAX_POSITIONS_C", os.environ.get("MAX_POSITIONS_PER_ACCOUNT", "5"))),
            "D": int(os.environ.get("MAX_POSITIONS_D", os.environ.get("MAX_POSITIONS_PER_ACCOUNT", "5"))),
        }
        self.max_positions = per_account_limits.get(self.account, int(os.environ.get("MAX_POSITIONS_PER_ACCOUNT", "5")))
        # Dynamic position limit: max positions scale with equity
        self._equity_per_position = float(os.environ.get(
            f"EQUITY_PER_POSITION_{self.account}",
            os.environ.get("EQUITY_PER_POSITION", "200"),
        ))
        self._max_positions_cap = int(os.environ.get(
            f"MAX_POSITIONS_CAP_{self.account}",
            os.environ.get("MAX_POSITIONS_CAP", "5"),
        ))
        # Min positions floor (env MIN_POSITIONS_{account}, default 1).
        # Real-A sets MIN_POSITIONS_A=2 to prevent position-limit block when
        # equity drops to $200-400 range (35+ signals blocked on 2026-07-09).
        # Formula: min(cap, max(min_positions, calculated)) — cap is hard ceiling.
        self._min_positions = max(1, int(os.environ.get(
            f"MIN_POSITIONS_{self.account}",
            os.environ.get("MIN_POSITIONS", "1"),
        )))
        # ISSUE M3: previously `ACCOUNT_IDS.get(self.account, 3)` silently fell back to 3 (C)
        # if account name was typo'd. Now __init__ raises loud for unknown accounts, so this
        # is guaranteed to be a known account — but assert anyway to be defensive.
        self.account_id = ACCOUNT_IDS[self.account]
        # ISSUE-067: previously hardcoded ACCOUNT_RISK.get(self.account, 0.02) — env override
        # RISK_PER_TRADE_{account} was silently ignored. Now env wins, then registry, then hardcoded.
        _env_risk = os.environ.get(f"RISK_PER_TRADE_{self.account}")
        if _env_risk is not None:
            _risk_per_trade = float(_env_risk)
        else:
            try:
                from metty.core.account_registry import get_account_config
                _risk_per_trade = float(get_account_config(self.account).risk_per_trade)
            except Exception:
                _risk_per_trade = ACCOUNT_RISK.get(self.account, 0.02)
        self.risk = risk_config or RiskConfig(risk_per_trade=_risk_per_trade)
        # Per-account strategy overrides via env vars (for testing different configs)
        # ISSUE-069: env default was "2.0" but RiskConfig.atr_multiplier default is 2.5
        # (account_registry default 2.5). When env unset, per_account_atr would override
        # RiskConfig's 2.5 down to 2.0 — making SL 20% tighter than intended. Now match
        # the registry default so env override is opt-IN, not silent shrink.
        per_account_atr = {
            "A": float(os.environ.get("ATR_MULTIPLIER_A", os.environ.get("ATR_MULTIPLIER", "2.5"))),
            "B": float(os.environ.get("ATR_MULTIPLIER_B", os.environ.get("ATR_MULTIPLIER", "2.5"))),
            "C": float(os.environ.get("ATR_MULTIPLIER_C", os.environ.get("ATR_MULTIPLIER", "2.5"))),
            "D": float(os.environ.get("ATR_MULTIPLIER_D", os.environ.get("ATR_MULTIPLIER", "2.5"))),
        }
        per_account_rr = {
            "A": float(os.environ.get("RR_RATIO_A", os.environ.get("RR_RATIO", "2.5"))),
            "B": float(os.environ.get("RR_RATIO_B", os.environ.get("RR_RATIO", "2.5"))),
            "C": float(os.environ.get("RR_RATIO_C", os.environ.get("RR_RATIO", "2.5"))),
            "D": float(os.environ.get("RR_RATIO_D", os.environ.get("RR_RATIO", "2.5"))),
        }
        per_account_conf = {
            "A": float(os.environ.get("MIN_CONFIDENCE_A", os.environ.get("MIN_CONFIDENCE", "0.45"))),
            "B": float(os.environ.get("MIN_CONFIDENCE_B", os.environ.get("MIN_CONFIDENCE", "0.45"))),
            "C": float(os.environ.get("MIN_CONFIDENCE_C", os.environ.get("MIN_CONFIDENCE", "0.45"))),
            "D": float(os.environ.get("MIN_CONFIDENCE_D", os.environ.get("MIN_CONFIDENCE", "0.45"))),
        }
        if not risk_config:
            self.risk.risk_reward_ratio = per_account_rr.get(self.account, self.risk.risk_reward_ratio)
            self.risk.min_confidence = per_account_conf.get(self.account, self.risk.min_confidence)
        # Fix #3 bugfix (2026-07-13): ATR_MULTIPLIER_A env must ALWAYS win, even
        # when oracle-engine passes a risk_config (it does — registry default
        # atr_multiplier=2.5). Previously this line sat inside `if not risk_config`
        # so env was silently skipped → first new trade recorded 2.5 not 2.0.
        # Env is explicit user intent → overrides risk_config. See
        # tests/test_atr_env_override_risk_config_causal.py.
        self.risk.atr_multiplier = per_account_atr.get(self.account, self.risk.atr_multiplier)
        # Partial TP overrides per account
        per_account_ptp = {
            "A": os.environ.get("PARTIAL_TP_ENABLED_A", os.environ.get("PARTIAL_TP_ENABLED", "0")) == "1",
            "B": os.environ.get("PARTIAL_TP_ENABLED_B", os.environ.get("PARTIAL_TP_ENABLED", "0")) == "1",
            "C": os.environ.get("PARTIAL_TP_ENABLED_C", os.environ.get("PARTIAL_TP_ENABLED", "0")) == "1",
            "D": os.environ.get("PARTIAL_TP_ENABLED_D", os.environ.get("PARTIAL_TP_ENABLED", "0")) == "1",
        }
        per_account_tp1r = {
            "A": float(os.environ.get("TP1_RATIO_A", os.environ.get("TP1_RATIO", "0.5"))),
            "B": float(os.environ.get("TP1_RATIO_B", os.environ.get("TP1_RATIO", "0.5"))),
            "C": float(os.environ.get("TP1_RATIO_C", os.environ.get("TP1_RATIO", "0.5"))),
            "D": float(os.environ.get("TP1_RATIO_D", os.environ.get("TP1_RATIO", "0.5"))),
        }
        per_account_rrsi = {
            "A": float(os.environ.get("RR_SCALE_IN_A", os.environ.get("RR_SCALE_IN", "2.5"))),
            "B": float(os.environ.get("RR_SCALE_IN_B", os.environ.get("RR_SCALE_IN", "2.5"))),
            "C": float(os.environ.get("RR_SCALE_IN_C", os.environ.get("RR_SCALE_IN", "2.5"))),
            "D": float(os.environ.get("RR_SCALE_IN_D", os.environ.get("RR_SCALE_IN", "2.5"))),
        }
        # Fix #1 (2026-07-12): trailing TP params env-configurable per account.
        # Defaults (0.20/0.10) are too tight for XAUUSD — choke winners before
        # they reach 2.5R TP, inverting win:loss from design 2.5:1 to actual 0.82:1.
        # Wider values let winners breathe. See learning 2026-07-12_real-a-post-deploy-3fixes-check.
        per_account_trail_act = {
            "A": float(os.environ.get("TRAILING_ACTIVATION_PCT_A", os.environ.get("TRAILING_ACTIVATION_PCT", "0.20"))),
            "B": float(os.environ.get("TRAILING_ACTIVATION_PCT_B", os.environ.get("TRAILING_ACTIVATION_PCT", "0.20"))),
            "C": float(os.environ.get("TRAILING_ACTIVATION_PCT_C", os.environ.get("TRAILING_ACTIVATION_PCT", "0.20"))),
            "D": float(os.environ.get("TRAILING_ACTIVATION_PCT_D", os.environ.get("TRAILING_ACTIVATION_PCT", "0.20"))),
        }
        per_account_trail_trail = {
            "A": float(os.environ.get("TRAILING_TRAIL_PCT_A", os.environ.get("TRAILING_TRAIL_PCT", "0.10"))),
            "B": float(os.environ.get("TRAILING_TRAIL_PCT_B", os.environ.get("TRAILING_TRAIL_PCT", "0.10"))),
            "C": float(os.environ.get("TRAILING_TRAIL_PCT_C", os.environ.get("TRAILING_TRAIL_PCT", "0.10"))),
            "D": float(os.environ.get("TRAILING_TRAIL_PCT_D", os.environ.get("TRAILING_TRAIL_PCT", "0.10"))),
        }
        self.risk.partial_tp_enabled = per_account_ptp.get(self.account, self.risk.partial_tp_enabled)
        self.risk.tp1_ratio = per_account_tp1r.get(self.account, self.risk.tp1_ratio)
        self.risk.rr_scale_in = per_account_rrsi.get(self.account, self.risk.rr_scale_in)
        self.risk.trailing_activation_pct = per_account_trail_act.get(self.account, self.risk.trailing_activation_pct)
        self.risk.trailing_trail_pct = per_account_trail_trail.get(self.account, self.risk.trailing_trail_pct)
        # Override sizing method from env if set
        env_sizing = os.environ.get("POSITION_SIZING_METHOD", "").strip()
        if env_sizing and env_sizing in SIZING_METHODS:
            self.risk.sizing_method = env_sizing
        self._sizing_fn = SIZING_METHODS[self.risk.sizing_method]
        self.circuit_breaker = CircuitBreaker(
            consecutive_loss_limit=self.risk.consecutive_loss_limit,
            daily_loss_limit_pct=self.risk.daily_loss_limit_pct,
        )
        # ISSUE C1: TradeBlocker (gap-filler) — enforces hard_max_lots, risk_pct_sanity,
        # sl_too_tight, sl_too_wide, margin_safety, daily/weekly trade count limits.
        # Not wired before → live path had no protection against misconfigured SL/lots.
        self._trade_blocker = TradeBlocker(
            daily_trade_count_limit=int(os.environ.get("TRADE_BLOCKER_DAILY_LIMIT", "20")),
            weekly_trade_count_limit=int(os.environ.get("TRADE_BLOCKER_WEEKLY_LIMIT", "80")),
            hard_max_lots=float(os.environ.get("TRADE_BLOCKER_HARD_MAX_LOTS", "0.50")),
            max_risk_pct=0.05,
            margin_safety_factor=float(os.environ.get("TRADE_BLOCKER_MARGIN_SAFETY", "0.80")),
        )
        # Drawdown protection (stricter for real accounts)
        dd_config = get_drawdown_config(self.account)
        # ISSUE-070: initial_equity default "500" bricks small accounts (Real-A may start
        # at $100 or be topped up to a different amount). Use registry initial_balance as
        # the sane default; env override still wins for one-off testing.
        _env_init_eq = os.environ.get(f"INITIAL_EQUITY_{self.account}")
        if _env_init_eq is not None:
            _initial_equity = float(_env_init_eq)
        else:
            try:
                _initial_equity = float(get_account_config(self.account).initial_balance)
            except Exception:
                _initial_equity = 500.0
        self._drawdown_protector = DrawdownProtector(
            initial_equity=_initial_equity,
            daily_limit_pct=dd_config["daily_limit_pct"],
            weekly_limit_pct=dd_config["weekly_limit_pct"],
            account_limit_pct=dd_config["account_limit_pct"],
            cooldown_hours=dd_config["cooldown_hours"],
        )
        # BUY confidence filter (stricter for real accounts)
        self._buy_min_confidence = float(os.environ.get(
            f"BUY_MIN_CONFIDENCE_{self.account}",
            str(get_buy_min_confidence(self.account)),
        ))
        self._calendar_cache: list = []
        self._calendar_cache_time: float = 0
        self._sentiment_cache: dict = {}
        self._sentiment_cache_time: float = 0
        self._mfe_mae_state: dict[int, dict] = {}  # trade_id -> {mfe, mae, entry_price}
        self._last_exit_time: Optional[datetime] = None
        self._notifier = notifier
        self._last_d1_trend: Optional[str] = None
        self._last_h4_trend: Optional[str] = None
        self._cycle_count: int = 0
        self.strategy_id = f"swing-{self.account}"

        # Account role labels for logging clarity
        self._account_label = {
            "A": "V4-DD-Real",
            "B": "V4-Pro500",
            "C": "V6-Pro500",
        }.get(self.account, self.account)
        self.event_bus = event_bus
        # ML filter — risk-scale position size based on P(LOSS) prediction
        # Two modes:
        #   - Single model: ML_MODEL_DIR_{account} / ML_MODEL_DIR → TradeOutcomePredictor
        #   - Ensemble:     ML_ENSEMBLE_MODE={or|and|avg|max} → EnsemblePredictor
        #     (uses ML_ENSEMBLE_MODEL_DIRS colon-separated list + ML_ENSEMBLE_THRESH)
        # Ensemble is opt-in per container. NEVER enabled on Account A (IRON LAW).
        self._ml_enabled = os.environ.get("ML_FILTER_ENABLED", "0") == "1"
        self._ml_predictor = None
        self._ml_fail_count: int = 0  # consecutive ML prediction failures
        if self._ml_enabled:
            try:
                # Per-account ensemble enable takes precedence over global.
                # This lets oracle-engine-train run ensemble on B/D while C
                # stays on V4 single-model (C ensemble is OOS-overfit).
                # NEVER enabled on Account A (IRON LAW — A lives in oracle-engine).
                ensemble_mode = (
                    os.environ.get(f"ML_ENSEMBLE_MODE_{self.account}", "").strip().lower()
                    or os.environ.get("ML_ENSEMBLE_MODE", "").strip().lower()
                )
                if ensemble_mode:
                    from broky.ml.ensemble_predictor import EnsemblePredictor
                    per_account_thresh = (
                        os.environ.get(f"ML_ENSEMBLE_THRESH_{self.account}", "").strip()
                        or os.environ.get("ML_ENSEMBLE_THRESH", "0.50").strip()
                    )
                    self._ml_predictor = EnsemblePredictor(
                        mode=ensemble_mode,
                        loss_threshold=float(per_account_thresh),
                    )
                    logger.info("[%s] ML ensemble mode=%s dirs=%s thresh=%s",
                                self.display_name, ensemble_mode,
                                os.environ.get("ML_ENSEMBLE_MODEL_DIRS", ""),
                                per_account_thresh)
                else:
                    from broky.ml.trade_outcome_predictor import TradeOutcomePredictor
                    # Per-account model dir: real accounts can pin to stable model
                    per_account_dir = os.environ.get(f"ML_MODEL_DIR_{self.account}", "")
                    ml_dir = per_account_dir or os.environ.get("ML_MODEL_DIR", "data/models/trade_outcome_v4")
                    logger.info("[%s] ML model dir: %s (per_account=%s)", self.display_name, ml_dir, per_account_dir)
                    self._ml_predictor = TradeOutcomePredictor(
                        model_dir=ml_dir,
                        loss_threshold=float(os.environ.get("ML_LOSS_THRESHOLD", "0.65")),
                    )
                logger.info("[Swing:%s] ML filter enabled: %s", self.display_name,
                           "models loaded" if self._ml_predictor.enabled else "no models")
                # Health check: verify ML predictor can actually produce predictions
                if self._ml_predictor.enabled:
                    healthy, reason = self._ml_predictor.health_check()
                    if not healthy:
                        logger.critical("[Swing:%s] ML filter UNHEALTHY: %s — disabling", self.display_name, reason)
                        self._ml_enabled = False
                        self._ml_predictor = None
                    else:
                        logger.info("[Swing:%s] ML filter health check passed: %s", self.display_name, reason)
            except Exception as e:
                logger.warning("[Swing:%s] ML filter init failed: %s", self.display_name, e)
                self._ml_enabled = False

    def _get_calendar(self) -> list:
        now = time.time()
        if now - self._calendar_cache_time > 3600:
            try:
                self._calendar_cache = fetch_calendar(days_ahead=2, filter_currencies={"USD"})
            except Exception as e:
                logger.warning("Calendar fetch failed: %s", e)
                self._calendar_cache = []
            self._calendar_cache_time = now
        return self._calendar_cache

    def _get_sentiment(self) -> dict:
        """Fetch real sentiment data with 15-minute cache."""
        now = time.time()
        if now - self._sentiment_cache_time > 900:  # 15 min cache
            try:
                from metty.execution.live_collector import fetch_live_sentiment
                self._sentiment_cache = fetch_live_sentiment() or {}
            except Exception as e:
                logger.warning("[Swing:%s] Sentiment fetch failed: %s", self.display_name, e)
                self._sentiment_cache = {}
            self._sentiment_cache_time = now
        return self._sentiment_cache

    def _get_current_spread(self) -> float | None:
        """Get current spread from MT5 bridge. Returns None if unavailable.

        Callers should skip the cycle if spread is None — using 0.0 would
        calculate stop-loss too tight, risking premature SL hits.
        """
        try:
            from metty.bridge.client import MT5Bridge
            from metty.core.account_registry import get_account_config
            from metty.core.models import AccountConfig

            cfg = get_account_config(self.account)
            config = AccountConfig(
                name=cfg.name,
                broker_login=cfg.broker_login,
                broker_server=cfg.broker_server,
                balance=cfg.initial_balance,
                leverage=cfg.leverage,
                bridge_host=cfg.bridge_host,
                bridge_port=cfg.bridge_internal_port,
                signal_group=cfg.signal_group,
            )
            bridge = MT5Bridge(config)
            spread = bridge.get_spread_sync(cfg.symbol)
            if spread is not None and spread >= 0:
                return float(spread)
        except Exception as e:
            logger.debug("[%s] Spread fetch failed: %s", self.display_name, e)
        return None

    def _get_calendar_context(self) -> tuple[int | None, str | None, str | None]:
        """Get minutes to next event, event type, and impact level."""
        calendar = self._get_calendar()
        if not calendar:
            return None, None, None
        try:
            now_utc = datetime.now(timezone.utc)
            for event in calendar:
                # ISSUE-066: CalendarEvent is a dataclass with fields datetime/event/impact,
                # NOT a dict with date/title/impact. Old code called event.get("date"/"title")
                # → AttributeError swallowed by bare except → always returned None.
                # Support both dataclass and dict shapes (defensive).
                if isinstance(event, dict):
                    event_time = event.get("date") or event.get("datetime")
                    event_name = event.get("title") or event.get("event", "")
                    event_impact = event.get("impact", "")
                else:
                    event_time = getattr(event, "datetime", None)
                    event_name = getattr(event, "event", "") or ""
                    event_impact = getattr(event, "impact", "") or ""
                if not event_time:
                    continue
                if isinstance(event_time, str):
                    event_time = datetime.fromisoformat(event_time.replace("Z", "+00:00"))
                minutes_left = int((event_time - now_utc).total_seconds() / 60)
                if minutes_left > 0:
                    return minutes_left, event_name, event_impact
            return None, None, None
        except Exception as e:
            logger.debug("[%s] _get_calendar_context failed: %s", self.display_name, e)
            return None, None, None

    def _record_rejection(self, signal: Signal, reason: str, session: str = "",
                          d1_trend: str = "", candles: dict | None = None) -> None:
        """Record a rejected signal for survivorship bias analysis."""
        try:
            import json
            ts_str = signal.timestamp.isoformat() if hasattr(signal.timestamp, 'isoformat') else str(signal.timestamp)
            indicator_json = json.dumps(signal.indicators) if signal.indicators else None
            insert_rejected_signal(
                account_id=self.account_id,
                timestamp=ts_str,
                direction=signal.signal_type.value,
                confidence=signal.confidence,
                price=signal.price,
                rejection_reason=reason,
                trading_mode=TradingMode.SWING.value,
                strategy_id=self.strategy_id,
                regime=signal.regime,
                session=session,
                d1_trend=d1_trend,
                signal_json=indicator_json,
                db_path=self.db_path,
            )
        except Exception as e:
            logger.warning("[Swing:%s] Failed to record rejection: %s", self.display_name, e)

    def _fetch_candles(self) -> Optional[dict[str, pd.DataFrame]]:
        """Fetch candle data from MT5 bridge, fall back to CSV."""
        try:
            from metty.bridge.client import MT5Bridge
            from metty.core.account_registry import get_bridge_config
            from broky.data.resampler import resample_timeframe
            from metty.execution.historical_collector import _normalize_columns

            try:
                config = get_bridge_config(self.account)
            except ValueError:
                logger.warning("Unknown account: %s", self.account)
                return None

            bridge = MT5Bridge(config)
            candles = {}

            for tf in ["M5", "H1", "H4", "D1"]:
                df = bridge.fetch_candles_sync("XAUUSD", tf, 500)
                if not df.empty:
                    candles[tf] = _normalize_columns(df)

            if not candles:
                return self._fetch_candles_csv()

            # Resample M5 for higher TFs if missing
            if "M5" in candles:
                m5 = candles["M5"]
                for tf in ["H1", "H4", "D1"]:
                    if tf not in candles:
                        try:
                            candles[tf] = _normalize_columns(
                                resample_timeframe(m5.reset_index(), tf)
                            )
                        except Exception:
                            pass

            return candles

        except Exception as e:
            logger.warning("MT5 bridge fetch failed: %s", e)
            return self._fetch_candles_csv()

    def _fetch_candles_csv(self) -> Optional[dict[str, pd.DataFrame]]:
        """Fallback: load candles from CSV."""
        try:
            from broky.data.loader import load_timeframe
            from broky.data.resampler import resample_timeframe
            from metty.execution.historical_collector import _normalize_columns, WINDOW_SIZE

            m5_raw = load_timeframe(self.data_dir, "M5").tail(WINDOW_SIZE)
            if m5_raw.empty:
                return None

            candles = {
                "M5": _normalize_columns(m5_raw),
                "H1": _normalize_columns(resample_timeframe(m5_raw, "H1")),
                "H4": _normalize_columns(resample_timeframe(m5_raw, "H4")),
                "D1": _normalize_columns(resample_timeframe(m5_raw, "D1")),
            }
            return candles
        except Exception as e:
            logger.error("CSV fallback failed: %s", e)
            return None

    def _generate_signal(self, candles: dict[str, pd.DataFrame]) -> Optional[Signal]:
        """Generate a trading signal from candle data."""
        m5 = candles.get("M5")
        if m5 is None or len(m5) < 50:
            return None

        # Determine D1 trend from EMA 50/200
        d1 = candles.get("D1")
        d1_trend = self._determine_d1_trend(d1)
        d1_trend_strength = self._compute_d1_trend_strength(d1)
        price_momentum_24h = self._compute_price_momentum_24h(d1)

        # Determine H4 trend from EMA 10/50 (faster override for D1)
        h4 = candles.get("H4")
        h4_trend = self._compute_h4_trend(h4)

        # Detect trend flips and send Telegram alert
        self._check_trend_flips(d1_trend, h4_trend)

        try:
            signal = generate_signal(
                close=m5["close"],
                high=m5["high"],
                low=m5["low"],
                volume=m5["volume"],
                current_price=float(m5["close"].iloc[-1]),
                timestamp=m5.index[-1].to_pydatetime().replace(tzinfo=timezone.utc)
                if hasattr(m5.index[-1], "to_pydatetime")
                else datetime.now(timezone.utc),
                d1_trend=d1_trend,
                h4_trend=h4_trend,
                d1_trend_strength=d1_trend_strength,
                price_momentum_24h=price_momentum_24h,
                min_confidence=self.risk.min_confidence,
                learning_mode=self.learning_mode,
            )
            return signal
        except Exception as e:
            logger.error("Signal generation failed: %s", e)
            return None

    def _determine_d1_trend(self, d1: Optional[pd.DataFrame]) -> str:
        if d1 is None or len(d1) < 200:
            return "unknown"
        close = d1["close"]
        ema50 = close.ewm(span=50, adjust=False).mean()
        ema200 = close.ewm(span=200, adjust=False).mean()
        if pd.isna(ema200.iloc[-1]):
            return "unknown"
        return "bullish" if ema50.iloc[-1] > ema200.iloc[-1] else "bearish"

    def _compute_h4_trend(self, h4: Optional[pd.DataFrame]) -> Optional[str]:
        """Compute H4 trend using EMA 10/50 crossover (faster than D1 EMA 50/200).

        Fix #2 (2026-07-12): drop the last (incomplete) H4 bar before EMA.
        `broky.data.resampler.resample_timeframe` includes the in-progress bin
        as the last row, so EMA10/50 on `iloc[-1]` reads the intra-bar close
        that oscillates with every M5 tick. On a choppy H4 (EMA10 ≈ EMA50) this
        flipped the label 19+ times in 8h on Real-A (07-10) and green-lit two
        counter-trend SELLs that hit SL. H4_USE_CLOSED_BAR_ONLY=1 (default) drops
        the last row so the label reflects the last CLOSED H4 bar; =0 keeps the
        legacy noisy behavior for rollback. See
        tests/test_h4_trend_incomplete_bar_causal.py.
        """
        if h4 is None or len(h4) < 50:
            return None
        try:
            use_closed_only = os.environ.get("H4_USE_CLOSED_BAR_ONLY", "1") in ("1", "true", "True")
            src = h4.iloc[:-1] if use_closed_only and len(h4) > 50 else h4
            ema10 = src["close"].ewm(span=10, adjust=False).mean()
            ema50 = src["close"].ewm(span=50, adjust=False).mean()
            if pd.isna(ema10.iloc[-1]) or pd.isna(ema50.iloc[-1]):
                return None
            return "bullish" if ema10.iloc[-1] > ema50.iloc[-1] else "bearish"
        except Exception:
            return None

    def _check_trend_flips(self, d1_trend: str, h4_trend: Optional[str]) -> None:
        """Detect D1/H4 trend changes and send Telegram alert."""
        if self._notifier is None:
            return
        if not self._notifier.enabled:
            return

        now = datetime.now(timezone.utc).strftime("%H:%M")
        alerts = []

        # ISSUE-065: capture OLD values BEFORE updating. Previously _last_d1_trend was
        # updated at L577-578 BEFORE the EventBus publish at L590 which compared
        # `d1_trend != self._last_d1_trend` (always False) → TREND_FLIP events never fired
        # and old_direction in the payload was already new.
        old_d1 = self._last_d1_trend
        old_h4 = self._last_h4_trend

        if old_d1 is not None and d1_trend != "unknown" and d1_trend != old_d1:
            direction = "🟢 BULLISH" if d1_trend == "bullish" else "🔴 BEARISH"
            alerts.append(
                f"<b>D1 Trend Flip</b> {now}\n"
                f"Account {self.account}: {old_d1} → {direction}"
            )

        if old_h4 is not None and h4_trend and h4_trend != "unknown" and h4_trend != old_h4:
            direction = "🟢 BULLISH" if h4_trend == "bullish" else "🔴 BEARISH"
            alerts.append(
                f"<b>H4 Trend Flip</b> {now}\n"
                f"Account {self.account}: {old_h4} → {direction}"
            )

        # Update tracking
        if d1_trend != "unknown":
            self._last_d1_trend = d1_trend
        if h4_trend and h4_trend != "unknown":
            self._last_h4_trend = h4_trend

        # Send alerts (deduplicate across accounts via notifier's rate limit)
        for msg in alerts:
            try:
                self._notifier.send(msg)
            except Exception:
                pass

        # Also emit TREND_FLIP events via EventBus for programmatic consumers.
        # ISSUE-065: compare against captured OLD value (not the just-updated one).
        if self.event_bus:
            if old_d1 is not None and d1_trend != "unknown" and d1_trend != old_d1:
                self.event_bus.publish(Event(
                    type=EventType.TREND_FLIP,
                    data={
                        "timeframe": "D1",
                        "direction": d1_trend,
                        "old_direction": old_d1,
                        "symbol": "XAUUSD",
                        "account": self.account,
                    },
                ))
            if old_h4 is not None and h4_trend and h4_trend != "unknown" and h4_trend != old_h4:
                self.event_bus.publish(Event(
                    type=EventType.TREND_FLIP,
                    data={
                        "timeframe": "H4",
                        "direction": h4_trend,
                        "old_direction": old_h4,
                        "symbol": "XAUUSD",
                        "account": self.account,
                    },
                ))

    def _compute_d1_trend_strength(self, d1: Optional[pd.DataFrame]) -> Optional[float]:
        """Compute normalized D1 trend strength from EMA 50/200 spread.

        Returns 0.0 (flat) to ~1.0 (very strong trend).
        EMA50-EMA200 spread divided by price gives a normalized measure.
        """
        if d1 is None or len(d1) < 200:
            return None
        try:
            close = d1["close"]
            ema50 = close.ewm(span=50, adjust=False).mean()
            ema200 = close.ewm(span=200, adjust=False).mean()
            if pd.isna(ema200.iloc[-1]) or pd.isna(ema50.iloc[-1]):
                return None
            spread = abs(ema50.iloc[-1] - ema200.iloc[-1])
            price = close.iloc[-1]
            if price <= 0:
                return None
            # Normalize: 0.5% spread = moderate, 2%+ = very strong
            strength = spread / price / 0.02
            return float(max(0.0, min(1.0, strength)))
        except Exception:
            return None

    def _compute_price_momentum_24h(self, d1: Optional[pd.DataFrame]) -> Optional[float]:
        """Compute 24h price momentum as ratio of change.

        Returns negative for falling price, positive for rising.
        e.g. -0.015 = price dropped 1.5% in ~24h.
        Uses last 2 D1 candles as proxy for 24-48h window.
        """
        if d1 is None or len(d1) < 2:
            return None
        try:
            close = d1["close"]
            prev = close.iloc[-2]
            curr = close.iloc[-1]
            if pd.isna(prev) or pd.isna(curr) or prev <= 0:
                return None
            return float((curr - prev) / prev)
        except Exception:
            return None

    def _classify_session(self, timestamp: datetime) -> str:
        hour = timestamp.hour
        if 13 <= hour < 16:
            return "overlap"
        if 8 <= hour < 16:
            return "london"
        if 13 <= hour < 22:
            return "ny"
        return "asian"

    def _get_equity(self) -> float | None:
        """Get current account equity from MT5. Returns None if unavailable.

        Callers should skip the cycle if equity is None — using a stale
        hardcoded value (500.0) would produce incorrect position sizing
        and drawdown protection.
        """
        try:
            from metty.bridge.client import MT5Bridge
            from metty.core.account_registry import get_account_config
            from metty.core.models import AccountConfig

            cfg = get_account_config(self.account)
            config = AccountConfig(
                name=cfg.name,
                broker_login=cfg.broker_login,
                broker_server=cfg.broker_server,
                balance=cfg.initial_balance,
                leverage=cfg.leverage,
                bridge_host=cfg.bridge_host,
                bridge_port=cfg.bridge_internal_port,
                signal_group=cfg.signal_group,
            )
            info = MT5Bridge(config).fetch_account_info_sync()
            return info.equity if info else None
        except Exception as e:
            logger.debug("[%s] Equity fetch failed: %s", self.display_name, e)
            return None

    def _get_free_margin(self) -> float | None:
        """Get current account free margin from MT5 (accounts for used margin of open
        positions). Returns None if unavailable.

        ISSUE-060: the locally-computed `free_margin = max(equity - new_trade_margin, 0)`
        ignored used margin of already-open positions. With dynamic_max up to 5 positions,
        4 open positions' used margin was uncounted → TradeBlocker could approve a 5th
        trade that exceeds real free margin → broker reject or margin call.
        """
        try:
            from metty.bridge.client import MT5Bridge
            from metty.core.account_registry import get_account_config
            from metty.core.models import AccountConfig

            cfg = get_account_config(self.account)
            config = AccountConfig(
                name=cfg.name,
                broker_login=cfg.broker_login,
                broker_server=cfg.broker_server,
                balance=cfg.initial_balance,
                leverage=cfg.leverage,
                bridge_host=cfg.bridge_host,
                bridge_port=cfg.bridge_internal_port,
                signal_group=cfg.signal_group,
            )
            info = MT5Bridge(config).fetch_account_info_sync()
            return info.free_margin if info else None
        except Exception as e:
            logger.debug("[%s] Free margin fetch failed: %s", self.display_name, e)
            return None

    def _calculate_max_positions(self, equity: float) -> int:
        """Calculate max simultaneous positions dynamically based on equity and risk.

        Formula: min(cap, max(min_positions, floor(equity / equity_per_position)))

        - `min_positions` (env MIN_POSITIONS_{account}, default 1) is the floor —
          protects against position-limit block when equity drops.
        - `cap` (env MAX_POSITIONS_CAP_{account}, default 5) is the hard ceiling —
          cap always wins when cap < floor.

        Examples (equity_per_position=200, cap=5, min_positions=1 default):
          $199 → 1 position  (small account, conservative)
          $400 → 2 positions
          $1000+ → 5 positions (capped)
        Examples (Real-A: min_positions=2):
          $78  → 2 positions (floor protects against 1-position block)
          $200 → 2 positions
          $400 → 2 positions
          $600 → 3 positions (calculated exceeds floor)
        """
        if equity <= 0:
            return min(self._max_positions_cap, self._min_positions)
        calculated = int(equity // self._equity_per_position)
        result = min(self._max_positions_cap, max(self._min_positions, calculated))
        logger.debug(
            "[%s] Dynamic max_positions: equity=$%.2f / $%.0f = %d, cap=%d, min=%d → %d",
            self.display_name, equity, self._equity_per_position,
            calculated, self._max_positions_cap, self._min_positions, result,
        )
        return result

    def _check_existing_position(self) -> bool:
        """Check if there's an open position for this account.

        Uses MT5 as the PRIMARY source of truth — only returns True
        if MT5 actually has an open position. DB trades that MT5 no
        longer has are reconciled with actual exit prices from deal
        history (or inferred from SL/TP as fallback).
        """
        # Check MT5 for open positions (source of truth)
        try:
            import rpyc
            from metty.core.account_registry import get_account_config
            cfg = get_account_config(self.account)

            conn = rpyc.connect(cfg.bridge_host, cfg.bridge_internal_port, config={"sync_request_timeout": 10})
            # ISSUE-078 (2026-07-07): MT5 terminal in Wine requires initialize() before
            # any query. live_collector's MT5Bridge wrapper calls initialize()+shutdown()
            # each cycle, leaving the terminal in shutdown state. Without initialize()
            # here, positions_get returns None (looks like bridge down) when actually
            # the bridge is fine — race condition where timing decides if trades happen.
            try:
                if not conn.root.initialize():
                    logger.warning(
                        "[%s] MT5 initialize failed (last_error=%s) — treating as bridge down",
                        self.display_name, conn.root.last_error(),
                    )
                    return len(get_open_trades(self.account_id, self.db_path)) > 0
                positions_raw = conn.root.positions_get(symbol=cfg.symbol)
            finally:
                try:
                    conn.root.shutdown()
                except Exception:
                    pass
                conn.close()

            # SWING-RECONCILE-1: distinguish bridge-down from bridge-OK-no-positions.
            # positions_get returns None when MT5/bridge is disconnected (cannot reach
            # broker) vs [] when broker is reachable and has no open positions.
            # Treating None as "no position" triggers reconcile on bridge-down →
            # _get_deal_history also returns [] (swallowed) → reconcile falls back
            # to entry_price → PnL=0 false close → DB thinks 0 open → opens new
            # position while old still in MT5 → runaway loop. Skip reconcile when
            # bridge is down; hold off new trades until bridge recovers.
            if positions_raw is None:
                open_trades = get_open_trades(self.account_id, self.db_path)
                logger.warning(
                    "[%s] MT5 bridge returned None (disconnected) — skipping reconcile, "
                    "holding off new trades (%d open in DB)",
                    self.display_name, len(open_trades),
                )
                return len(open_trades) > 0

            has_mt5_position = len(positions_raw) > 0

            if not has_mt5_position:
                # MT5 says no position — reconcile DB trades with MT5 state
                open_trades = get_open_trades(self.account_id, self.db_path)
                if open_trades:
                    logger.warning(
                        "[%s] %d open DB trades but no MT5 position — reconciling",
                        self.display_name, len(open_trades),
                    )
                    # Try to get deal history for accurate exit prices.
                    # ISSUE-061: if deal history fetch fails (bridge timeout/RPyC flip),
                    # skip reconciliation entirely rather than falling back to breakeven
                    # inference — which would mark real-loss trades as PnL=0 and pollute
                    # DP/CB counters. Retry next cycle when bridge is healthy.
                    deals = self._get_deal_history(days_back=7)
                    if deals is None:
                        logger.warning(
                            "[%s] Deal history unavailable — skipping reconciliation (will retry next cycle)",
                            self.display_name,
                        )
                        # MT5 says no position, but we can't reconcile safely.
                        # Return True so run_once treats it as "existing position open"
                        # and holds off opening new trades until we can reconcile.
                        return len(open_trades) > 0
                    # Convert RPyC positions (may be raw list of dicts already)
                    positions_list = []
                    if positions_raw:
                        positions_list = [
                            p if isinstance(p, dict) else dict(p)
                            for p in positions_raw
                        ]
                    closed = reconcile_closed_positions(
                        self.account_id, open_trades,
                        positions_list, deals,
                        self.db_path,
                    )
                    if closed:
                        logger.info("[%s] Reconciled %d closed positions", self.display_name, closed)
                        # Sync drawdown protector with DB — reconciliation-closed
                        # trades are invisible to in-memory PnL tracking
                        self._drawdown_protector.sync_pnl_from_db(
                            self.account_id, self.db_path,
                        )

            return has_mt5_position
        except Exception as e:
            logger.warning("[%s] MT5 position check failed: %s — falling back to DB", self.display_name, e)
            # Fallback to DB only if MT5 is unreachable
            open_trades = get_open_trades(self.account_id, self.db_path)
            return len(open_trades) > 0

    def _get_deal_history(self, days_back: int = 7) -> Optional[list[dict]]:
        """Fetch MT5 deal history for reconciliation.

        Returns:
            list[dict] — on success (may be empty if no deals in window)
            None — on bridge failure (timeout, RPyC flip, container restart)

        ISSUE-061: previously swallowed all errors → returned [] → caller couldn't
        distinguish "bridge worked, no deals" from "bridge failed". On bridge failure,
        reconcile_closed_positions with empty deals fell back to breakeven (PnL=0)
        for trades that MT5 actually closed at a loss → DP/CB counters polluted.
        Now returns None on failure so caller skips reconciliation entirely.
        """
        try:
            from metty.bridge.client import MT5Bridge
            deals = MT5Bridge(get_bridge_config(self.account)).fetch_deal_history_sync(self.symbol, days_back=days_back)
            # Propagate None (bridge failure) — caller's `if deals is None` guard
            # relies on this to skip reconcile. `deals or []` would swallow None
            # and cause the runaway-loop bug (false close → open new → repeat).
            return deals
        except Exception as e:
            logger.warning("[%s] Deal history fetch failed — skipping reconciliation: %s", self.display_name, e)
            return None

    def _reconcile_external_close(
        self,
        ticket: int | None,
        direction: str,
        entry_price: float,
        sl: float,
        tp: float,
    ) -> Optional[dict]:
        """Find the broker-side closing deal for a position already gone from MT5.

        Used by `_monitor_positions` and `_execute_tp1_close` when
        `_close_mt5_position_with_fill` returns (False, None) because MT5
        `positions_get(ticket)` returned empty (broker already closed the
        position via SL/TP/manual). Without this, the trader would treat
        "position gone" as "MT5 close failed" and leave DB is_open=1 forever
        — a ghost trade that blocks all new entries (ISSUE-077, 2026-07-06).

        Matching strategies (in order of reliability):
          0. deal.order == ticket AND deal.type == closing_type
             (MT5 links closing deals to the open order ticket)
          1. deal.position_id == ticket (when bridge exposes this field)
          2. deal.type == closing_type AND price within 0.5% of entry
             (price-proximity fallback for broker-closed deals where
             order/position_id are not propagated)

        Args:
            ticket: position ticket from DB
            direction: trade direction ("BUY" or "SELL")
            entry_price: trade entry price (for proximity matching)
            sl: stop loss (used to derive exit_reason on price match)
            tp: take profit (used to derive exit_reason on price match)

        Returns:
            dict {"exit_price": float, "exit_reason": str} if a closing deal
            is found, else None. exit_reason is derived from the deal's
            `reason` field (DEAL_REASON_SL=4 → "stop_loss",
            DEAL_REASON_TP=5 → "take_profit", DEAL_REASON_SO=6 →
            "margin_call") or its comment pattern, falling back to
            "closed_by_mt5".
        """
        if not ticket:
            return None
        deals = self._get_deal_history(days_back=7)
        # Bridge failure → don't false-close (caller keeps retry behavior)
        if deals is None:
            return None
        if not deals:
            return None

        ticket_int = int(ticket)
        # Closing direction: BUY position closed by SELL deal (type=1), vice versa
        closing_type = 1 if direction.upper() == "BUY" else 0
        opening_type = 0 if direction.upper() == "BUY" else 1

        # ISSUE-080 (2026-07-08): find the OPEN deal's time so Strategy 2
        # (price proximity) can exclude closing deals that happened BEFORE
        # this trade opened. Without this guard, when two trades cluster in
        # a price neighborhood, Strategy 2 picks the earlier trade's SL/TP
        # close as the "closest" deal to this trade's entry — recording the
        # wrong exit_price, pnl, and exit_reason. The OPEN deal has
        # deal.order == position ticket AND deal.type == opening_type.
        open_deal_time: float | None = None
        for deal in deals:
            deal_order = deal.get("order")
            if deal_order is None:
                continue
            try:
                if int(deal_order) != ticket_int:
                    continue
            except (TypeError, ValueError):
                continue
            if deal.get("type", -1) != opening_type:
                continue
            deal_time = deal.get("time")
            if deal_time is None:
                continue
            try:
                open_deal_time = float(deal_time)
            except (TypeError, ValueError):
                continue
            break

        # Map MT5 deal reason codes to our exit_reason strings
        def _reason_from_deal(deal: dict) -> str:
            reason = deal.get("reason")
            comment = (deal.get("comment") or "").lower()
            if reason == 4 or "[sl" in comment or "sl " in comment:
                return "stop_loss"
            if reason == 5 or "[tp" in comment or "tp " in comment:
                return "take_profit"
            if reason == 6:
                return "margin_call"
            return "closed_by_mt5"

        # Strategy 0: deal.order == ticket AND closing direction
        for deal in deals:
            deal_order = deal.get("order")
            if deal_order is None:
                continue
            try:
                if int(deal_order) != ticket_int:
                    continue
            except (TypeError, ValueError):
                continue
            if deal.get("type", -1) == closing_type:
                price = float(deal.get("price", 0) or 0)
                if price > 0:
                    return {"exit_price": round(price, 2), "exit_reason": _reason_from_deal(deal)}

        # Strategy 1: deal.position_id == ticket (if bridge exposes it)
        for deal in deals:
            pos_id = deal.get("position_id")
            if pos_id is None:
                continue
            try:
                if int(pos_id) != ticket_int:
                    continue
            except (TypeError, ValueError):
                continue
            if deal.get("type", -1) == closing_type:
                price = float(deal.get("price", 0) or 0)
                if price > 0:
                    return {"exit_price": round(price, 2), "exit_reason": _reason_from_deal(deal)}

        # Strategy 2: closing direction + price within 0.5% of entry + AFTER open
        # ISSUE-080 (2026-07-08): time guard — a closing deal that happened
        # BEFORE this trade opened cannot be this trade's close. Without this
        # guard, when two trades cluster in a price neighborhood, Strategy 2
        # picks the earlier trade's SL/TP close as the "closest" deal to this
        # trade's entry — recording the wrong exit_price, pnl, exit_reason.
        best: dict | None = None
        best_diff = float("inf")
        max_diff = abs(entry_price) * 0.005
        for deal in deals:
            if deal.get("type", -1) != closing_type:
                continue
            # Time guard: closing deal must come AFTER the open deal
            if open_deal_time is not None:
                deal_time = deal.get("time")
                if deal_time is None:
                    continue
                try:
                    if float(deal_time) <= open_deal_time:
                        continue
                except (TypeError, ValueError):
                    continue
            price = float(deal.get("price", 0) or 0)
            if price <= 0:
                continue
            diff = abs(price - entry_price)
            if diff < max_diff and diff < best_diff:
                best_diff = diff
                best = deal
        if best is not None:
            return {
                "exit_price": round(float(best.get("price", 0)), 2),
                "exit_reason": _reason_from_deal(best),
            }

        return None

    def _check_cooldown(self) -> bool:
        """Check if we're still in cooldown after last exit."""
        if self._last_exit_time is None:
            return False
        elapsed = (datetime.now(timezone.utc) - self._last_exit_time).total_seconds()
        cooldown_seconds = self.risk.cooldown_bars * self.risk.bar_seconds
        return elapsed < cooldown_seconds

    def _calculate_lots(self, equity: float, price: float, sl: float, atr: float) -> float:
        """Calculate position size using the configured sizing method."""
        if self.risk.sizing_method == "risk_per_trade":
            return risk_per_trade_size(
                equity, self.risk.risk_per_trade, price, sl, CONTRACT_SIZE,
            )
        elif self.risk.sizing_method == "kelly":
            # Use last 50 closed trades for Kelly estimation
            from metty.core.db import get_closed_trades
            closed = get_closed_trades(self.account_id, self.db_path, limit=50)
            if len(closed) < 10:
                return risk_per_trade_size(
                    equity, self.risk.risk_per_trade, price, sl, CONTRACT_SIZE,
                )
            wins = [t for t in closed if t.get("pnl", 0) > 0]
            losses = [t for t in closed if t.get("pnl", 0) <= 0]
            win_rate = len(wins) / len(closed) if closed else 0.5
            avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 1.0
            avg_loss = abs(sum(t["pnl"] for t in losses) / len(losses)) if losses else 1.0
            return kelly_size(
                equity, win_rate, avg_win, avg_loss, price, sl, CONTRACT_SIZE,
            )
        elif self.risk.sizing_method == "volatility_adjusted":
            return volatility_adjusted_size(
                equity, self.risk.risk_per_trade, price, sl, atr, CONTRACT_SIZE,
            )
        elif self.risk.sizing_method == "fixed_fraction":
            return fixed_fraction_size(0.01)
        else:
            return risk_per_trade_size(
                equity, self.risk.risk_per_trade, price, sl, CONTRACT_SIZE,
            )

    def _close_mt5_position(self, ticket: int) -> bool:
        """Close a position in MT5 by ticket. Returns True on success, False on failure.

        ISSUE-048: extracted helper so callers can close in MT5 FIRST and only update DB
        after success. Previously _monitor_positions and _execute_tp1_close wrote
        close_live_trade + CB.record + DP.record BEFORE bridge.close_position, so an
        MT5 close failure left an orphan position whose real PnL was never reconciled.
        """
        ok, _ = self._close_mt5_position_with_fill(ticket)
        return ok

    def _close_mt5_position_with_fill(self, ticket: int) -> tuple[bool, Optional[float]]:
        """Close a position in MT5 and return (success, fill_price).

        ISSUE-063: close_position returns bool only; callers that need the actual close
        fill price (for PnL) fell back to the theoretical SL/TP/tp1 price. This variant
        uses close_position_with_fill so DB exit_price reflects actual broker fill.
        """
        if not ticket or self.dry_run:
            return True, None  # nothing to close — treat as success so DB path proceeds
        try:
            import asyncio
            from metty.bridge.client import MT5Bridge
            # ISSUE-064: use account_registry as single source of truth for bridge config
            # (was hardcoded port_map + fallback host bypassing registry).
            bridge = MT5Bridge(get_bridge_config(self.account))

            async def _close():
                if not await bridge.connect():
                    return False, None
                ok, fill = await bridge.close_position_with_fill(ticket)
                await bridge.disconnect()
                return ok, fill

            return asyncio.run(_close())
        except Exception as e:
            logger.warning("Failed to close position %s in MT5: %s", ticket, e)
            return False, None

    def _monitor_positions(self, candles: dict[str, pd.DataFrame]) -> list[dict]:
        """Check open trades for exit conditions (SL/TP hit, max holding)."""
        open_trades = get_open_trades(self.account_id, self.db_path)
        closed = []

        if not open_trades or "M5" not in candles:
            return closed

        m5 = candles["M5"]
        current_price = float(m5["close"].iloc[-1])
        now_str = datetime.now(timezone.utc).isoformat()

        for trade in open_trades:
            direction = trade["direction"]
            entry_price = trade["entry_price"]
            sl = trade["stop_loss"]
            tp = trade["take_profit"]
            lot_size = trade["lot_size"]
            trade_id = trade["id"]

            # Update MFE/MAE tracking
            m5_high = float(m5["high"].iloc[-1])
            m5_low = float(m5["low"].iloc[-1])
            mfe_mae = self._mfe_mae_state.get(trade_id, {"mfe": 0, "mae": 0, "entry_price": entry_price})
            if direction == "BUY":
                favorable = m5_high - entry_price
                adverse = entry_price - m5_low
            else:
                favorable = entry_price - m5_low
                adverse = m5_high - entry_price
            mfe_mae["mfe"] = max(mfe_mae["mfe"], favorable)
            mfe_mae["mae"] = max(mfe_mae["mae"], adverse)
            self._mfe_mae_state[trade_id] = mfe_mae

            # === Partial TP (Option C): detect TP1 hit ===
            tp_level = trade.get("tp_level", 1) or 1
            tp1_price = trade.get("tp1_price")
            if (
                self.risk.partial_tp_enabled
                and tp_level == 1
                and tp1_price
                and tp1_price > 0
            ):
                tp1_hit = False
                if direction == "BUY" and current_price >= tp1_price:
                    tp1_hit = True
                elif direction == "SELL" and current_price <= tp1_price:
                    tp1_hit = True

                if tp1_hit:
                    closed.extend(self._execute_tp1_close(trade, tp1_price, mfe_mae, now_str))
                    continue  # Trade closed + scale-in opened, skip normal exit

            exit_reason = None
            exit_price = current_price

            # ISSUE-052: Trailing TP D 0.20/0.10 — ported from backtest.
            # peak (BUY) = entry + mfe ; trough (SELL) = entry - mfe.
            # arm once gain_pct >= activation_pct; trail_level = peak * (1 - trail_pct/100).
            trailing_enabled = (
                self.risk.trailing_tp_enabled
                and entry_price > 0
                and mfe_mae.get("mfe", 0) > 0
            )
            trailing_armed = False
            trailing_level = None
            if trailing_enabled:
                gain_pct = mfe_mae["mfe"] / entry_price * 100.0
                if gain_pct >= self.risk.trailing_activation_pct:
                    trailing_armed = True
                    if direction == "BUY":
                        peak = entry_price + mfe_mae["mfe"]
                        trailing_level = peak * (1 - self.risk.trailing_trail_pct / 100.0)
                    else:  # SELL
                        trough = entry_price - mfe_mae["mfe"]
                        trailing_level = trough * (1 + self.risk.trailing_trail_pct / 100.0)

            # Check SL/TP + trailing TP (order: SL floor → trailing → Far TP)
            if direction == "BUY":
                if sl > 0 and current_price <= sl:
                    exit_reason = "stop_loss"
                    exit_price = sl
                elif trailing_armed and trailing_level is not None and current_price <= trailing_level:
                    exit_reason = "trailing_tp"
                    exit_price = round(trailing_level, 2)
                elif tp > 0 and current_price >= tp:
                    exit_reason = "take_profit"
                    exit_price = tp
            elif direction == "SELL":
                if sl > 0 and current_price >= sl:
                    exit_reason = "stop_loss"
                    exit_price = sl
                elif trailing_armed and trailing_level is not None and current_price >= trailing_level:
                    exit_reason = "trailing_tp"
                    exit_price = round(trailing_level, 2)
                elif tp > 0 and current_price <= tp:
                    exit_reason = "take_profit"
                    exit_price = tp

            # Check max holding time
            if exit_reason is None:
                entry_time = pd.Timestamp(trade["timestamp"])
                bars_held = 0
                try:
                    if entry_time in m5.index:
                        bars_held = len(m5[m5.index > entry_time])
                    else:
                        bars_held = len(m5[m5.index > entry_time.tz_localize(None)])
                except Exception:
                    pass

                # ISSUE-052: 24h time stop (288 M5 bars) per system report section 2.
                # Was max_holding_bars=36 (3h) — now uses time_stop_bars (default 288).
                time_stop = self.risk.time_stop_bars if self.risk.time_stop_bars > 0 else self.risk.max_holding_bars
                if time_stop > 0 and bars_held >= time_stop:
                    exit_reason = "max_holding"

            if exit_reason:
                # Calculate PnL
                if direction == "BUY":
                    pnl = (exit_price - entry_price) * lot_size * CONTRACT_SIZE
                    pnl_pct = (exit_price - entry_price) / entry_price * 100
                else:
                    pnl = (entry_price - exit_price) * lot_size * CONTRACT_SIZE
                    pnl_pct = (entry_price - exit_price) / entry_price * 100

                # Close in DB with MFE/MAE data
                mfe_mae = self._mfe_mae_state.pop(trade_id, {"mfe": 0, "mae": 0, "entry_price": entry_price})
                mfe = mfe_mae.get("mfe", 0)
                mae = mfe_mae.get("mae", 0)
                mfe_pct = round(mfe / entry_price * 100, 4) if entry_price > 0 else 0
                mae_pct = round(mae / entry_price * 100, 4) if entry_price > 0 else 0

                # Get exit context (regime/trend at exit time)
                exit_d1_trend = self._last_d1_trend
                exit_h4_trend = self._last_h4_trend
                exit_regime = exit_d1_trend if exit_d1_trend and exit_d1_trend not in ("neutral", "unknown") else None

                # ISSUE-048: close in MT5 FIRST, only update DB/CB/DP after success.
                # Old order wrote close_live_trade + CB.record + DP.record BEFORE bridge.close_position;
                # if MT5 close failed (caught + logged), DB said is_open=0 but MT5 still held the position.
                # The orphan's real PnL was never reconciled back → kill switch flew blind.
                # ISSUE-063: capture actual close fill price; recompute PnL from it so DB
                # exit_price + pnl reflect reality (not theoretical SL/TP/tp1 level).
                # ISSUE-077 (2026-07-06): when MT5 says "position not found" (broker already
                # closed it via SL/TP), _close_mt5_position_with_fill returns (False, None).
                # Treating that as "MT5 close failed" left ghost trades that blocked all new
                # entries. Reconcile from deal history instead — use the broker's actual fill
                # price + reason so DB PnL is real.
                actual_fill = None
                if trade.get("ticket") and not self.dry_run:
                    mt5_ok, actual_fill = self._close_mt5_position_with_fill(trade["ticket"])
                    if not mt5_ok:
                        external = self._reconcile_external_close(
                            trade.get("ticket"), direction, entry_price, sl, tp,
                        )
                        if external is not None:
                            actual_fill = external["exit_price"]
                            if external.get("exit_reason"):
                                exit_reason = external["exit_reason"]
                            logger.info(
                                "[%s] Reconciled external close for ticket %s — exit=$%.2f reason=%s",
                                self.display_name, trade["ticket"], actual_fill, exit_reason,
                            )
                            # fall through to DB close with actual_fill
                        else:
                            logger.warning(
                                "[%s] MT5 close failed for ticket %s — leaving DB open, will retry next cycle",
                                self.display_name, trade["ticket"],
                            )
                            continue  # skip DB close + CB/DP update; retry next cycle
                    if actual_fill is not None and actual_fill > 0:
                        exit_price = round(actual_fill, 2)
                        # Recompute PnL with actual fill
                        if direction == "BUY":
                            pnl = (exit_price - entry_price) * lot_size * CONTRACT_SIZE
                            pnl_pct = (exit_price - entry_price) / entry_price * 100
                        else:
                            pnl = (entry_price - exit_price) * lot_size * CONTRACT_SIZE
                            pnl_pct = (entry_price - exit_price) / entry_price * 100

                close_live_trade(
                    trade_id=trade["id"],
                    exit_price=exit_price,
                    exit_time=now_str,
                    pnl=round(pnl, 2),
                    pnl_pct=round(pnl_pct, 4),
                    exit_reason=exit_reason,
                    mfe=round(mfe, 2),
                    mae=round(mae, 2),
                    mfe_pct=mfe_pct,
                    mae_pct=mae_pct,
                    exit_regime=exit_regime,
                    exit_d1_trend=exit_d1_trend,
                    exit_h4_trend=exit_h4_trend,
                    tp1_price=trade.get("tp1_price"),
                    db_path=self.db_path,
                )

                # Update circuit breaker
                if pnl > 0:
                    self.circuit_breaker.record_win(pnl)
                else:
                    self.circuit_breaker.record_loss(pnl)

                # Update drawdown protection
                equity = self._get_equity()
                if equity is not None:
                    self._drawdown_protector.record_pnl(round(pnl, 2), equity)
                else:
                    logger.warning("[%s] Equity unavailable after close — skipping drawdown update", self.display_name)

                self._last_exit_time = datetime.now(timezone.utc)

                closed.append({
                    "trade_id": trade["id"],
                    "direction": direction,
                    "exit_reason": exit_reason,
                    "exit_price": exit_price,
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl_pct, 4),
                })

                log_trade(logger, "CLOSED", account=self.account, direction=direction,
                         price=exit_price, pnl=pnl, ticket=str(trade.get("ticket", "")),
                         reason=exit_reason)

                if self.event_bus:
                    self.event_bus.publish(Event(
                        type=EventType.TRADE_CLOSED,
                        data={
                            "direction": direction, "symbol": trade.get("symbol", "XAUUSD"),
                            "entry_price": entry_price, "exit_price": exit_price,
                            "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 4),
                            "exit_reason": exit_reason, "account": self.account,
                            "trading_mode": trade.get("trading_mode", "swing"),
                        },
                    ))

        return closed

    def _execute_tp1_close(
        self,
        trade: dict,
        tp1_price: float,
        mfe_mae: dict,
        now_str: str,
    ) -> list[dict]:
        """Close position 1 at TP1, open scale-in position 2 (Option C).

        Flow:
        1. Close position 1 at TP1 price → take profit
        2. Open position 2 at current price with new SL based on rr_scale_in
        3. Position 2's TP = original final TP
        """
        closed = []
        direction = trade["direction"]
        entry_price = trade["entry_price"]
        sl = trade["stop_loss"]
        tp = trade["take_profit"]
        lot_size = trade["lot_size"]
        trade_id = trade["id"]

        # 1. Close position 1 at TP1
        exit_price = tp1_price
        if direction == "BUY":
            pnl = (exit_price - entry_price) * lot_size * CONTRACT_SIZE
            pnl_pct = (exit_price - entry_price) / entry_price * 100
        else:
            pnl = (entry_price - exit_price) * lot_size * CONTRACT_SIZE
            pnl_pct = (entry_price - exit_price) / entry_price * 100

        mfe = mfe_mae.get("mfe", 0)
        mae = mfe_mae.get("mae", 0)
        mfe_pct = round(mfe / entry_price * 100, 4) if entry_price > 0 else 0
        mae_pct = round(mae / entry_price * 100, 4) if entry_price > 0 else 0

        exit_d1_trend = self._last_d1_trend
        exit_h4_trend = self._last_h4_trend
        exit_regime = exit_d1_trend if exit_d1_trend and exit_d1_trend not in ("neutral", "unknown") else None

        # ISSUE-048: close in MT5 FIRST, only update DB/CB/DP after success.
        # ISSUE-063: capture actual close fill price; use it as exit_price so DB PnL
        # reflects reality (not theoretical tp1_price which can slip on market close).
        # ISSUE-077 (2026-07-06): reconcile externally-closed positions via deal history
        # — broker may have already hit SL/TP before we tried to close at TP1.
        actual_fill = None
        if trade.get("ticket") and not self.dry_run:
            mt5_ok, actual_fill = self._close_mt5_position_with_fill(trade["ticket"])
            if not mt5_ok:
                external = self._reconcile_external_close(
                    trade.get("ticket"), direction, entry_price, sl, tp,
                )
                if external is not None:
                    actual_fill = external["exit_price"]
                    logger.info(
                        "[Swing:%s] Reconciled external close for ticket %s at TP1 — exit=$%.2f",
                        self.display_name, trade["ticket"], actual_fill,
                    )
                    # fall through to DB close with actual_fill
                else:
                    logger.warning(
                        "[Swing:%s] MT5 TP1 close failed for ticket %s — leaving DB open, will retry next cycle",
                        self.display_name, trade["ticket"],
                    )
                    return closed  # do NOT close in DB; retry next cycle
            if actual_fill is not None and actual_fill > 0:
                exit_price = round(actual_fill, 2)
                # Recompute PnL with actual fill
                if direction == "BUY":
                    pnl = (exit_price - entry_price) * lot_size * CONTRACT_SIZE
                    pnl_pct = (exit_price - entry_price) / entry_price * 100
                else:
                    pnl = (entry_price - exit_price) * lot_size * CONTRACT_SIZE
                    pnl_pct = (entry_price - exit_price) / entry_price * 100

        close_live_trade(
            trade_id=trade["id"],
            exit_price=exit_price,
            exit_time=now_str,
            pnl=round(pnl, 2),
            pnl_pct=round(pnl_pct, 4),
            exit_reason="tp1_hit",
            mfe=round(mfe, 2),
            mae=round(mae, 2),
            mfe_pct=mfe_pct,
            mae_pct=mae_pct,
            exit_regime=exit_regime,
            exit_d1_trend=exit_d1_trend,
            exit_h4_trend=exit_h4_trend,
            tp1_price=tp1_price,
            tp_level=1,
            remaining_lots=0,
            db_path=self.db_path,
        )

        # Update circuit breaker for position 1 profit
        if pnl > 0:
            self.circuit_breaker.record_win(pnl)
        else:
            self.circuit_breaker.record_loss(pnl)

        self._last_exit_time = datetime.now(timezone.utc)

        # Update drawdown protection for TP1 close
        equity = self._get_equity()
        if equity is not None:
            self._drawdown_protector.record_pnl(round(pnl, 2), equity)
        else:
            logger.warning("[%s] Equity unavailable at TP1 close — skipping drawdown update", self.display_name)

        # Clean up MFE/MAE state for position 1
        self._mfe_mae_state.pop(trade_id, None)

        closed.append({
            "trade_id": trade["id"],
            "direction": direction,
            "exit_reason": "tp1_hit",
            "exit_price": exit_price,
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 4),
        })

        log_trade(logger, "TP1_CLOSED", account=self.account, direction=direction,
                  price=exit_price, pnl=pnl, ticket=str(trade.get("ticket", "")),
                  reason="tp1_hit")

        if self.event_bus:
            self.event_bus.publish(Event(
                type=EventType.TRADE_CLOSED,
                data={
                    "direction": direction, "symbol": trade.get("symbol", "XAUUSD"),
                    "entry_price": entry_price, "exit_price": exit_price,
                    "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 4),
                    "exit_reason": "tp1_hit", "account": self.account,
                    "trading_mode": trade.get("trading_mode", "swing"),
                },
            ))

        # 2. Open scale-in position (position 2) at current price
        # SL = tp1_price ± (remaining_distance / rr_scale_in)
        # TP = original final TP
        remaining_distance = abs(tp - tp1_price)
        if direction == "BUY":
            new_sl = tp1_price - remaining_distance / self.risk.rr_scale_in
        else:
            new_sl = tp1_price + remaining_distance / self.risk.rr_scale_in

        # Use same lot size (Exness minimum = 0.01)
        new_lots = lot_size
        current_price = tp1_price  # Approximate entry for scale-in

        # ISSUE H5: previously scale-in bypassed ALL risk gates (DP/CB/TradeBlocker/ML).
        # Position 1 already closed at TP1 (taking profit is safe), but the scale-in is a
        # BRAND-NEW order with a brand-new SL → must pass the same gate stack as run_once.
        # If any gate blocks, skip the scale-in (keep TP1 profit) and log.
        can_scale_in = True
        scale_in_block_reason = ""
        if not self.dry_run:
            # Sync PnL + DP check
            self._drawdown_protector.sync_pnl_from_db(self.account_id, self.db_path)
            scale_equity = self._get_equity()
            if scale_equity is None:
                can_scale_in = False
                scale_in_block_reason = "equity unavailable"
            else:
                dp_ok, dp_r = self._drawdown_protector.check(scale_equity)
                if not dp_ok:
                    can_scale_in = False
                    scale_in_block_reason = f"drawdown:{dp_r}"
                elif not self.learning_mode:
                    cb_ok, cb_r = self.circuit_breaker.can_open_trade()
                    if not cb_ok:
                        can_scale_in = False
                        scale_in_block_reason = f"circuit_breaker:{cb_r}"
                    else:
                        # TradeBlocker gate for scale-in
                        new_sl_dist_pct = abs(current_price - new_sl) / current_price * 100.0
                        leverage = int(os.environ.get(f"MT5_LEVERAGE_{self.account}", os.environ.get("MT5_LEVERAGE", "100")))
                        margin_req = new_lots * 100 * current_price / max(leverage, 1)
                        # ISSUE-060: use real MT5 free_margin for scale-in too.
                        mt5_fm = self._get_free_margin()
                        free_margin = mt5_fm if mt5_fm is not None else max(scale_equity - margin_req, 0.0)
                        open_trades_now = get_open_trades(self.account_id, self.db_path)
                        tb_v = self._trade_blocker.check(BlockInput(
                            open_positions=len(open_trades_now),
                            max_positions=self._calculate_max_positions(scale_equity),
                            daily_trades_today=self._drawdown_protector.state.daily_trades,
                            weekly_trades_this_week=self._drawdown_protector.state.weekly_trades,
                            lots=new_lots,
                            risk_pct=self.risk.risk_per_trade,
                            sl_distance_pct=new_sl_dist_pct,
                            equity=scale_equity,
                            margin_required=margin_req,
                            free_margin=free_margin,
                            learning_mode=self.learning_mode,
                        ))
                        if tb_v.blocked:
                            can_scale_in = False
                            scale_in_block_reason = f"trade_blocker:{tb_v.block_name}"
        if not can_scale_in:
            logger.info(
                "[Swing:%s] Scale-in at TP1 BLOCKED by risk gate — keeping TP1 profit. reason=%s",
                self.display_name, scale_in_block_reason,
            )
            if self._notifier and self._notifier.enabled:
                try:
                    self._notifier.send(
                        f"ℹ️ [{self.display_name}] Scale-in skipped at TP1\n"
                        f"Reason: {scale_in_block_reason}\n"
                        f"Position 1 closed at TP1 — profit kept."
                    )
                except Exception:
                    pass
            return closed  # TP1 closed; no scale-in

        # Open scale-in in MT5 first (if not dry run)
        ticket = None
        scale_in_fill_price = current_price  # default to tp1_price estimate
        scale_in_fill_lots = new_lots
        if not self.dry_run:
            try:
                from metty.bridge.client import MT5Bridge
                # ISSUE-064: use account_registry for bridge config (was hardcoded port_map).
                bridge = MT5Bridge(get_bridge_config(self.account))

                async def _open():
                    if not await bridge.connect():
                        return None
                    result = await bridge.send_order("XAUUSD", direction, new_lots, new_sl, tp)
                    await bridge.disconnect()
                    return result

                import asyncio
                order_result = asyncio.run(_open())
                # ISSUE-049 + ISSUE-055: must treat order_result=None / success=False /
                # ticket<=0 all as failure, otherwise a ghost row with ticket=None/0 is
                # inserted and reconciled as breakeven later.
                if not order_result or not order_result.success or not order_result.ticket or int(order_result.ticket) <= 0:
                    err = order_result.error if order_result else "bridge connect failed"
                    logger.error("[Swing:%s] Scale-in order FAILED at TP1: %s", self.display_name, err)
                    return closed  # Don't insert scale-in if MT5 order failed
                ticket = order_result.ticket
                # ISSUE-062: use actual fill price as scale-in entry, not tp1_price estimate.
                if order_result.price is not None and float(order_result.price) > 0:
                    scale_in_fill_price = round(float(order_result.price), 2)
                # ISSUE-057: use actual fill volume if reported.
                if order_result.volume is not None and float(order_result.volume) > 0:
                    scale_in_fill_lots = float(order_result.volume)
            except Exception as e:
                logger.error("[Swing:%s] Scale-in MT5 error at TP1: %s", self.display_name, e)
                return closed  # Don't insert scale-in if MT5 errored

        # Insert scale-in trade in DB
        scale_in_id = insert_live_trade(
            account_id=self.account_id,
            timestamp=now_str,
            direction=direction,
            entry_price=scale_in_fill_price,
            stop_loss=round(new_sl, 2),
            take_profit=tp,
            lot_size=scale_in_fill_lots,
            confidence=trade.get("confidence", 0),
            regime=trade.get("regime", "unknown") or "unknown",
            session=trade.get("session", "unknown") or "unknown",
            d1_trend=exit_d1_trend or "unknown",
            reason=f"scale-in from trade #{trade_id} (tp1_hit)",
            ticket=ticket,
            symbol=trade.get("symbol", "XAUUSD"),
            trading_mode=trade.get("trading_mode", "swing"),
            strategy_id=self.strategy_id,
            tp1_price=tp1_price,
            tp_level=2,
            parent_trade_id=trade_id,
            atr_multiplier=self.risk.atr_multiplier,
            rr_ratio=self.risk.risk_reward_ratio,
            min_confidence_threshold=self.risk.min_confidence,
            db_path=self.db_path,
        )

        # Initialize MFE/MAE tracking for scale-in position
        self._mfe_mae_state[scale_in_id] = {
            "mfe": 0, "mae": 0, "entry_price": scale_in_fill_price,
        }

        log_trade(logger, "SCALE_IN", account=self.account, direction=direction,
                  price=scale_in_fill_price, lots=scale_in_fill_lots, sl=new_sl, tp=tp,
                  reason=f"scale-in from #{trade_id}")
        # ISSUE-059: scale-in is a NEW trade open → count it for anti-churn.
        self._drawdown_protector.record_trade_open()

        if self.event_bus:
            self.event_bus.publish(Event(
                type=EventType.TRADE_OPENED,
                data={
                    "direction": direction, "symbol": "XAUUSD",
                    "price": scale_in_fill_price, "sl": new_sl, "tp": tp,
                    "lots": scale_in_fill_lots, "confidence": 0,
                    "regime": exit_regime or "unknown", "reason": "scale-in",
                    "account": self.account, "trading_mode": "swing",
                    "parent_trade_id": trade_id, "tp_level": 2,
                },
            ))

        return closed

    def run_once(self) -> dict:
        """Run a single trading cycle.

        Returns dict with: action, signal, trade details, etc.
        """
        init_db(self.db_path)
        self._cycle_count += 1

        # 1. Fetch candles
        candles = self._fetch_candles()
        if not candles or "M5" not in candles:
            return {"action": "skip", "reason": "no candle data"}

        m5 = candles["M5"]

        # 2. Monitor existing positions first
        closed = self._monitor_positions(candles)

        # 3. Generate signal
        signal = self._generate_signal(candles)
        if signal is None:
            return {"action": "skip", "reason": "signal generation failed"}

        price = signal.price
        session = self._classify_session(
            m5.index[-1].to_pydatetime().replace(tzinfo=timezone.utc)
            if hasattr(m5.index[-1], "to_pydatetime")
            else datetime.now(timezone.utc)
        )
        d1_trend = self._determine_d1_trend(candles.get("D1"))
        h4_trend = self._compute_h4_trend(candles.get("H4"))

        # 4. Risk checks
        if signal.signal_type == SignalType.HOLD:
            return {
                "action": "hold",
                "reason": f"no signal (conf={signal.confidence:.2f})",
                "signal": signal,
            }

        # 4a. BUY confidence filter — require higher confidence for BUY on real accounts
        if signal.signal_type == SignalType.BUY and signal.confidence < self._buy_min_confidence:
            self._record_rejection(signal, f"buy_low_confidence:{signal.confidence:.2f}<{self._buy_min_confidence}", session, d1_trend, candles)
            return {
                "action": "hold",
                "reason": f"BUY confidence too low: {signal.confidence:.2f} < {self._buy_min_confidence}",
                "signal": signal,
            }

        # 4a1. Counter-trend rejection gate (CLAUDE.md "ไม่แทงสวนเทรนด์" iron rule).
        # trend_alignment == -1 means the signal is counter-trend WITHOUT reversal
        # evidence (no OB/OS + divergence + HH/LL price-structure confirmation).
        # Reject hard; learning_mode bypasses so we still collect outcomes for ML.
        _trend_alignment = signal.indicators.get("trend_alignment") if signal.indicators else None
        _has_reversal = signal.indicators.get("has_reversal") if signal.indicators else None
        if (
            _trend_alignment == -1
            and not self.learning_mode
            and signal.signal_type != SignalType.HOLD
        ):
            self._record_rejection(
                signal,
                f"counter_trend_no_reversal:{signal.signal_type.value}_vs_{d1_trend}_d1",
                session, d1_trend, candles,
            )
            return {
                "action": "hold",
                "reason": (
                    f"counter-trend {signal.signal_type.value} vs {d1_trend} D1 "
                    f"without reversal evidence (no HH/LL + OB/OS + divergence) — blocked"
                ),
                "signal": signal,
            }

        # 4a2. Existing position check + reconciliation — must run BEFORE DP check.
        # ISSUE C6: previously DP.check ran BEFORE reconciliation. A losing trade that MT5
        # closed between cycles would reconcile AFTER the gate approved → next trade went
        # through while over the daily limit. Now reconcile first, then DP sees the real loss.
        if self._check_existing_position():
            self._record_rejection(signal, "existing_position", session, d1_trend, candles)
            return {
                "action": "hold",
                "reason": "position already open",
                "signal": signal,
            }

        # 4a3. Drawdown protection check (after reconciliation, before any risk-sensitive ops)
        # sync_pnl_from_db picks up any reconciled losses so DP sees real PnL.
        self._drawdown_protector.sync_pnl_from_db(self.account_id, self.db_path)
        equity = self._get_equity()
        if equity is None:
            logger.warning("[%s] Equity unavailable — skipping cycle (MT5 may be disconnected)", self.display_name)
            return {"action": "skip", "reason": "equity unavailable (MT5 disconnected?)"}
        # ISSUE H2: previously `equity if equity else self._get_equity() or self.initial_balance`
        # referenced self.initial_balance (never set) → AttributeError on equity=0.
        # Now equity is guaranteed non-None here, use it directly.
        current_equity = equity
        dd_can_trade, dd_reason = self._drawdown_protector.check(equity)
        if not dd_can_trade:
            log_circuit_break(logger, "DRAWDOWN_BLOCK", account=self.account, reason=dd_reason)
            self._record_rejection(signal, f"drawdown:{dd_reason}", session, d1_trend, candles)
            if self._notifier and self._notifier.enabled:
                try:
                    self._notifier.send(
                        f"<b>🛑 DRAWDOWN PROTECTION</b> Account {self.account}\n"
                        f"Reason: {dd_reason}\n"
                        f"Equity: ${equity:.2f}"
                    )
                except Exception:
                    pass
            return {
                "action": "hold",
                "reason": f"drawdown protection: {dd_reason}",
                "signal": signal,
            }

        # 4b. Position limit check (always enforced, even in learning mode)
        # Dynamic max_positions based on current equity and risk (1% per position)
        open_trades = get_open_trades(self.account_id, self.db_path)
        dynamic_max = self._calculate_max_positions(current_equity)
        if len(open_trades) >= dynamic_max:
            log_position(logger, "LIMIT", account=self.account, count=len(open_trades), max=dynamic_max)
            self._record_rejection(signal, "position_limit", session, d1_trend, candles)
            return {
                "action": "hold",
                "reason": f"position limit ({len(open_trades)}/{dynamic_max}, equity=${current_equity:.0f})",
                "signal": signal,
            }

        # 4c. Learning mode: bypass remaining risk checks for data collection
        # (DP + position_limit always enforced above even in learning mode — survival gates)
        if not self.learning_mode:
            can_trade, cb_reason = self.circuit_breaker.can_open_trade()
            if not can_trade:
                log_circuit_break(logger, "BLOCKED", account=self.account, reason=cb_reason)
                self._record_rejection(signal, f"circuit_breaker:{cb_reason}", session, d1_trend, candles)
                if self.event_bus:
                    self.event_bus.publish(Event(
                        type=EventType.CIRCUIT_BREAKER_TRIGGERED,
                        data={
                            "account": self.account, "reason": cb_reason,
                            "consecutive_losses": self.circuit_breaker.state.consecutive_losses,
                            "daily_loss_pct": self.circuit_breaker.state.daily_loss_pct,
                        },
                    ))
                return {
                    "action": "hold",
                    "reason": f"circuit breaker: {cb_reason}",
                    "signal": signal,
                }

            if self._check_cooldown():
                self._record_rejection(signal, "cooldown", session, d1_trend, candles)
                return {
                    "action": "hold",
                    "reason": "cooldown after last exit",
                    "signal": signal,
                }

            calendar = self._get_calendar()
            if should_avoid_trading(calendar):
                self._record_rejection(signal, "calendar_avoid", session, d1_trend, candles)
                return {
                    "action": "hold",
                    "reason": "high-impact news nearby",
                    "signal": signal,
                }

        # 5. ML filter — risk-scale position size based on P(LOSS) prediction
        ml_risk_multiplier = 1.0
        ml_loss_proba = None
        ml_model_used = None
        ml_risk_reason = None
        _live_spread = self._get_current_spread()
        if _live_spread is None:
            logger.warning("[%s] Spread unavailable — skipping cycle (MT5 may be disconnected)", self.display_name)
            return {"action": "skip", "reason": "spread unavailable (MT5 disconnected?)", "signal": signal}

        # Circuit breaker: if ML filter has failed too many times, stop trading
        if self._ml_enabled and self._ml_fail_count >= ML_MAX_CONSECUTIVE_FAILS:
            logger.critical(
                "[Swing:%s] ML filter failed %d times consecutively — circuit breaker: holding", self.display_name, self._ml_fail_count,
            )
            self._record_rejection(signal, f"ml_filter_circuit_break:{self._ml_fail_count}_fails", session, d1_trend, candles)
            return {"action": "hold", "reason": f"ML circuit breaker ({self._ml_fail_count} consecutive failures)", "signal": signal}

        if self._ml_enabled and self._ml_predictor is not None:
            try:
                from broky.ml.trade_outcome_predictor import compute_features_from_candles

                sentiment_data = self._get_sentiment()
                # ISSUE-068: _get_current_spread returns spread in POINTS (e.g., 20 for
                # $0.20 on XAUUSD with point=0.01), but compute_features_from_candles
                # expects PRICE units (e.g., 0.20). Training data was recorded in price
                # units → live passing 20.0 was 100x out of distribution. Convert by
                # multiplying by point (0.01 for XAUUSD).
                _ml_spread_price = _live_spread * 0.01 if _live_spread else 0.0
                ml_features = compute_features_from_candles(
                    candles, str(signal.signal_type.value),
                    spread=_ml_spread_price,
                    d1_trend=d1_trend or "neutral",
                    h4_trend=h4_trend or "unknown",
                    session=session,
                    sentiment=sentiment_data,
                )
                # Regime from features (derived from ADX), NOT d1_trend
                regime = ml_features.get("regime", "trending")
                ml_risk_multiplier, ml_reason, ml_loss_proba, ml_model_used = self._ml_predictor.get_risk_multiplier(
                    ml_features, regime, str(signal.signal_type.value),
                )
                # ML filter succeeded — reset failure counter
                self._ml_fail_count = 0

                if ml_risk_multiplier == 0:
                    if self.learning_mode:
                        # In learning mode, never block — allow trade with min lot
                        # to collect diverse outcomes for ML retraining
                        logger.info("[Swing:%s] ML would block, but learning mode: allowing min-lot trade (%s)", self.display_name, ml_reason)
                        ml_risk_multiplier = 0.01  # minimal position for data collection
                    else:
                        logger.info("[Swing:%s] ML filter blocked trade: %s", self.display_name, ml_reason)
                        self._record_rejection(signal, f"ml_filter:{ml_reason}", session, d1_trend, candles)
                        return {"action": "hold", "reason": ml_reason, "signal": signal}
                elif ml_risk_multiplier < 1.0:
                    logger.info("[Swing:%s] ML risk-scaling: %s", self.display_name, ml_reason)
                else:
                    logger.info("[Swing:%s] ML filter pass: %s", self.display_name, ml_reason)

            except Exception as e:
                self._ml_fail_count += 1
                logger.error(
                    "[Swing:%s] ML filter crashed (fail %d/%d): %s — proceeding WITHOUT ML protection", self.display_name, self._ml_fail_count, ML_MAX_CONSECUTIVE_FAILS, e,
                )
                # ML filter is down — trade proceeds at full size (1.0) with no ML scaling
                # Circuit breaker above will stop trading after ML_MAX_CONSECUTIVE_FAILS

        # 6. Calculate SL/TP/lots
        try:
            atr_series = calculate_atr(m5["high"], m5["low"], m5["close"], period=14)
            atr_val = float(atr_series.iloc[-1]) if not pd.isna(atr_series.iloc[-1]) else 5.0
            # ISSUE-058: ATR can be 0.0 (flat session, all bars equal) which passes the
            # not-pd.isna check. SL becomes only spread_buffer wide → inside spread on
            # tight accounts → instant stop-out. Fallback to sane 5.0 when 0.
            if atr_val <= 0:
                atr_val = 5.0
        except Exception:
            atr_val = 5.0

        direction = signal.signal_type.value
        sl = calculate_stop_loss(
            price, atr_val, direction,
            self.risk.atr_multiplier, self.risk.spread_buffer,
        )
        tp = calculate_take_profit(
            price, sl, direction, self.risk.risk_reward_ratio,
        )

        # TP1 = tp1_ratio of TP distance (for partial TP tracking)
        tp_distance = abs(tp - price)
        if direction == "BUY":
            tp1_price = round(price + tp_distance * self.risk.tp1_ratio, 2)
        else:
            tp1_price = round(price - tp_distance * self.risk.tp1_ratio, 2)

        # Use equity already fetched for drawdown check (avoid extra bridge call)
        lots = self._calculate_lots(equity, price, sl, atr_val)
        lots *= ml_risk_multiplier  # ML risk-scaling
        # ISSUE-051: re-round to 0.01 multiple + re-clamp UPPER bound to hard_max_lots.
        # _calculate_lots already rounded + clamped to [0.01, 10.0], but `*= multiplier` can
        # produce non-0.01-multiples (0.05*0.3=0.015 → MT5 rejects) or exceed hard cap when
        # multiplier > 1.0 (10.0*1.5=15.0). Do NOT clamp lower bound here — the `< 0.01` check
        # below preserves the "ML scales down below min lot → skip trade" behavior.
        hard_max_lots = float(os.environ.get("TRADE_BLOCKER_HARD_MAX_LOTS", "0.50"))
        lots = min(hard_max_lots, math.floor(lots * 100) / 100.0)
        if lots < 0.01:
            if self.learning_mode:
                # Force minimum lot to ensure trade executes for data collection
                logger.info("[Swing:%s] ML would skip (lot=%.4f), but learning mode: forcing min lot 0.01", self.account, lots)
                lots = 0.01
            else:
                logger.info("[Swing:%s] ML risk-scaling: lot_size=%.4f < 0.01, skipping", self.account, lots)
                self._record_rejection(signal, f"ml_lot_too_small:{lots:.4f}", session, d1_trend, candles)
                return {"action": "hold", "reason": f"ML risk: lot too small ({lots:.4f})", "signal": signal}

        # 6b. TradeBlocker — final hard safety gate (gap-filler).
        # ISSUE C1: previously not wired into live path → misconfigured SL/lots/risk_pct
        # could pass DP+CB. Now enforce: hard_max_lots, risk_pct_sanity, sl_too_tight,
        # sl_too_wide, margin_safety, daily/weekly trade count.
        sl_distance_pct = abs(price - sl) / price * 100.0
        # Rough margin estimate — Exness 1:100 default; env override for 1:500 etc.
        leverage = int(os.environ.get(f"MT5_LEVERAGE_{self.account}", os.environ.get("MT5_LEVERAGE", "100")))
        contract_size = 100  # XAUUSD: 1 lot = 100 oz
        margin_required = lots * contract_size * price / max(leverage, 1)
        # ISSUE-060: use real MT5 free_margin (accounts for used margin of open positions).
        # Falls back to local estimate only if MT5 unavailable.
        mt5_free_margin = self._get_free_margin()
        if mt5_free_margin is not None:
            free_margin = mt5_free_margin
        else:
            free_margin = max(current_equity - margin_required, 0.0)
        daily_trades_now = self._drawdown_protector.state.daily_trades
        weekly_trades_now = self._drawdown_protector.state.weekly_trades
        tb_verdict = self._trade_blocker.check(BlockInput(
            open_positions=len(open_trades),
            max_positions=dynamic_max,
            daily_trades_today=daily_trades_now,
            weekly_trades_this_week=weekly_trades_now,
            lots=lots,
            risk_pct=self.risk.risk_per_trade,
            sl_distance_pct=sl_distance_pct,
            equity=current_equity,
            margin_required=margin_required,
            free_margin=free_margin,
            learning_mode=self.learning_mode,
        ))
        if tb_verdict.blocked:
            log_circuit_break(logger, "TRADE_BLOCKER", account=self.account, reason=tb_verdict.reason)
            self._record_rejection(signal, f"trade_blocker:{tb_verdict.block_name}", session, d1_trend, candles)
            if self._notifier and self._notifier.enabled:
                try:
                    self._notifier.send(
                        f"<b>⛔ TRADE BLOCKER</b> Account {self.account}\n"
                        f"Check: {tb_verdict.block_name}\n"
                        f"Reason: {tb_verdict.reason}\n"
                        f"lots={lots} sl_dist={sl_distance_pct:.2f}% risk={self.risk.risk_per_trade:.2%}"
                    )
                except Exception:
                    pass
            return {
                "action": "hold",
                "reason": f"trade_blocker:{tb_verdict.block_name}:{tb_verdict.reason}",
                "signal": signal,
            }

        # 7. Execute or dry-run
        ts_str = (
            m5.index[-1].isoformat()
            if hasattr(m5.index[-1], "isoformat")
            else str(m5.index[-1])
        )

        # Build indicator scores JSON for debugging/feature importance
        # Include h4_trend so it's available in features_json during backfill
        indicator_scores_json = None
        if signal.indicators:
            import json
            scores = dict(signal.indicators)
            if h4_trend and h4_trend != "unknown":
                scores["h4_trend"] = h4_trend
            indicator_scores_json = json.dumps(scores)

        # Calendar context
        minutes_to_next, next_event_type, next_event_impact = self._get_calendar_context()

        # Link trade to latest collector snapshot for ML training
        ref_signal_id = get_latest_signal_id(self.account_id, self.db_path)

        if self.dry_run:
            trade_id = insert_live_trade(
                account_id=self.account_id,
                timestamp=ts_str,
                direction=direction,
                entry_price=price,
                stop_loss=sl,
                take_profit=tp,
                lot_size=lots,
                confidence=signal.confidence,
                regime=signal.regime or "unknown",
                session=session,
                d1_trend=d1_trend,
                reason=signal.reason,
                ticket=None,
                trading_mode=TradingMode.SWING.value,
                strategy_id=self.strategy_id,
                signal_id=ref_signal_id,
                atr_at_entry=atr_val,
                spread_at_entry=_live_spread if _live_spread > 0 else None,
                ml_risk_multiplier=ml_risk_multiplier,
                ml_risk_reason=ml_risk_reason,
                ml_loss_proba=ml_loss_proba,
                ml_model_used=ml_model_used,
                minutes_to_next_event=minutes_to_next,
                next_event_type=next_event_type,
                next_event_impact=next_event_impact,
                indicator_scores_json=indicator_scores_json,
                tp1_price=tp1_price,
                atr_multiplier=self.risk.atr_multiplier,
                rr_ratio=self.risk.risk_reward_ratio,
                min_confidence_threshold=self.risk.min_confidence,
                db_path=self.db_path,
            )
            log_trade(logger, "OPENED", account=self.account, direction=direction,
                     price=price, lots=lots, sl=sl, tp=tp, tp1=tp1_price,
                     confidence=signal.confidence, reason=signal.reason)
            # ISSUE-059: count trade OPEN for anti-churn (was counted on close only).
            self._drawdown_protector.record_trade_open()
            if self.event_bus:
                self.event_bus.publish(Event(
                    type=EventType.TRADE_OPENED,
                    data={
                        "direction": direction, "symbol": signal.symbol, "price": price,
                        "sl": sl, "tp": tp, "tp1": tp1_price, "lots": lots, "confidence": signal.confidence,
                        "regime": signal.regime or "unknown", "reason": signal.reason,
                        "account": self.account, "trading_mode": "swing", "dry_run": True,
                    },
                ))
            return {
                "action": "dry_run",
                "direction": direction,
                "price": price,
                "sl": sl,
                "tp": tp,
                "tp1": tp1_price,
                "lots": lots,
                "confidence": signal.confidence,
                "regime": signal.regime,
                "trade_id": trade_id,
            }

        # Live execution
        try:
            from metty.bridge.client import MT5Bridge
            # ISSUE-064: use account_registry for bridge config (was hardcoded port_map).
            bridge = MT5Bridge(get_bridge_config(self.account))

            # ISSUE-053: SL/TP were computed from signal.price (M5 close) but MT5 fills at
            # ask (BUY) / bid (SELL). Absolute SL/TP sent with the order are then ~spread
            # closer to fill than intended. Recompute SL/TP from an estimated fill price
            # using current spread (points → price via *0.01 for XAUUSD) so the risk
            # distance from actual fill is correct.
            spread_price = (_live_spread or 0) * 0.01  # points → price units
            if direction == "BUY":
                est_fill_price = price + spread_price / 2.0  # ask ≈ mid + half-spread
            else:
                est_fill_price = price - spread_price / 2.0  # bid ≈ mid - half-spread
            sl_for_order = calculate_stop_loss(
                est_fill_price, atr_val, direction,
                self.risk.atr_multiplier, self.risk.spread_buffer,
            )
            tp_for_order = calculate_take_profit(
                est_fill_price, sl_for_order, direction, self.risk.risk_reward_ratio,
            )
            # TP1 also recomputed from est_fill_price so DB tp1 matches entry basis
            tp_distance_est = abs(tp_for_order - est_fill_price)
            if direction == "BUY":
                tp1_price_for_order = round(est_fill_price + tp_distance_est * self.risk.tp1_ratio, 2)
            else:
                tp1_price_for_order = round(est_fill_price - tp_distance_est * self.risk.tp1_ratio, 2)

            async def _execute():
                if not await bridge.connect():
                    return None
                result = await bridge.send_order("XAUUSD", direction, lots, sl_for_order, tp_for_order)
                await bridge.disconnect()
                return result

            import asyncio
            order_result = asyncio.run(_execute())

            # ISSUE C2: previously a failed/None order_result still inserted a DB row
            # with ticket=None → ghost position. _check_existing_position then reconciled
            # it as breakeven (PnL=0) → polluted DP/CB counters and analytics.
            # Now: skip DB insert entirely on failure, log + notify, return early.
            if not order_result or not order_result.success:
                err = order_result.error if order_result else "bridge connect failed"
                logger.error(
                    "[Swing:%s] Order FAILED — no DB row inserted. err=%s dir=%s lots=%s sl=%s tp=%s",
                    self.display_name, err, direction, lots, sl, tp,
                )
                if self._notifier:
                    try:
                        self._notifier.send(
                            f"⚠️ [{self.display_name}] Order rejected: {err}\n"
                            f"dir={direction} lots={lots} — no DB row written."
                        )
                    except Exception:
                        pass
                # Record rejected signal for analytics (no ticket, no ghost)
                try:
                    insert_rejected_signal(
                        account_id=self.account_id,
                        timestamp=ts_str,
                        direction=direction,
                        confidence=signal.confidence,
                        price=price,
                        rejection_reason=f"order_send: {err}",
                        trading_mode=TradingMode.SWING.value,
                        strategy_id=self.strategy_id,
                        regime=signal.regime or "unknown",
                        session=session,
                        d1_trend=d1_trend,
                        db_path=self.db_path,
                    )
                except Exception:
                    pass
                return {"action": "order_failed", "reason": err}

            # Order succeeded — use ACTUAL fill price (C4: previously used signal.price
            # which is M5 close, biased by spread+slippage → PnL wrong → kill switch late).
            # ISSUE-055: ticket must be > 0. Bridge returns success=True on retcode==DONE
            # with ticket=result.get("order"). If order=0 or None on a DONE retcode (broker
            # quirk), DB would insert a ghost row with ticket=0/None. Treat as failure.
            ticket = order_result.ticket
            if not ticket or int(ticket) <= 0:
                err = f"order succeeded retcode=DONE but ticket invalid ({ticket})"
                logger.error("[%s] %s — no DB row inserted.", self.display_name, err)
                if self._notifier:
                    try:
                        self._notifier.send(
                            f"⚠️ [{self.display_name}] Order ghost ticket: {ticket}\n"
                            f"dir={direction} lots={lots} — no DB row written."
                        )
                    except Exception:
                        pass
                return {"action": "order_failed", "reason": err}

            # ISSUE-056: use `is not None` instead of truthiness. order_result.price==0.0
            # (broker failed to populate on DONE) would fall back to signal.price via
            # `if order_result.price else price`. Treat 0.0/None both as fallback.
            if order_result.price is not None and float(order_result.price) > 0:
                fill_price = float(order_result.price)
            else:
                fill_price = est_fill_price  # better estimate than signal.price
                logger.warning(
                    "[%s] order_result.price missing/zero — using est_fill_price %s (signal.price=%s)",
                    self.display_name, fill_price, price,
                )
            if abs(fill_price - price) > 0.01:
                logger.info(
                    "[%s] Fill price %s differs from signal.price %s (spread+slip)",
                    self.display_name, fill_price, price,
                )
            # ISSUE-054: recompute SL/TP from ACTUAL fill price so DB tuple is consistent
            # (entry, SL, TP all on the same basis). _monitor_positions uses stored SL/TP
            # for exit detection + exit_price=sl → wrong SL = wrong PnL = wrong kill switch.
            entry_for_db = fill_price
            sl_for_db = calculate_stop_loss(
                fill_price, atr_val, direction,
                self.risk.atr_multiplier, self.risk.spread_buffer,
            )
            tp_for_db = calculate_take_profit(
                fill_price, sl_for_db, direction, self.risk.risk_reward_ratio,
            )
            tp_distance_fill = abs(tp_for_db - fill_price)
            if direction == "BUY":
                tp1_price_for_db = round(fill_price + tp_distance_fill * self.risk.tp1_ratio, 2)
            else:
                tp1_price_for_db = round(fill_price - tp_distance_fill * self.risk.tp1_ratio, 2)

            # ISSUE-057: use actual fill volume from broker if reported. On partial fills
            # (FOK rejection → broker partial, thin liquidity), DB lot_size > actual filled
            # → PnL overstated → kill switch late. Fall back to requested lots only if
            # broker didn't report a positive volume.
            if order_result.volume is not None and float(order_result.volume) > 0:
                fill_lots = float(order_result.volume)
                if abs(fill_lots - lots) > 0.001:
                    logger.info(
                        "[%s] Fill volume %s differs from requested %s (partial fill)",
                        self.display_name, fill_lots, lots,
                    )
            else:
                fill_lots = lots

            trade_id = insert_live_trade(
                account_id=self.account_id,
                timestamp=ts_str,
                direction=direction,
                entry_price=entry_for_db,
                stop_loss=sl_for_db,
                take_profit=tp_for_db,
                lot_size=fill_lots,
                confidence=signal.confidence,
                regime=signal.regime or "unknown",
                session=session,
                d1_trend=d1_trend,
                reason=signal.reason,
                ticket=ticket,
                trading_mode=TradingMode.SWING.value,
                strategy_id=self.strategy_id,
                signal_id=ref_signal_id,
                atr_at_entry=atr_val,
                spread_at_entry=_live_spread if _live_spread > 0 else None,
                ml_risk_multiplier=ml_risk_multiplier,
                ml_risk_reason=ml_risk_reason,
                ml_loss_proba=ml_loss_proba,
                ml_model_used=ml_model_used,
                minutes_to_next_event=minutes_to_next,
                next_event_type=next_event_type,
                next_event_impact=next_event_impact,
                indicator_scores_json=indicator_scores_json,
                tp1_price=tp1_price_for_db,
                atr_multiplier=self.risk.atr_multiplier,
                rr_ratio=self.risk.risk_reward_ratio,
                min_confidence_threshold=self.risk.min_confidence,
                db_path=self.db_path,
            )

            # Order succeeded (failure path returns early above) — log + emit event.
            log_trade(logger, "FILLED", account=self.account, direction=direction,
                     price=entry_for_db, lots=fill_lots, sl=sl_for_db, tp=tp_for_db, ticket=ticket)
            # ISSUE-059: count trade OPEN for anti-churn (was counted on close only).
            self._drawdown_protector.record_trade_open()
            if self.event_bus:
                self.event_bus.publish(Event(
                    type=EventType.TRADE_OPENED,
                    data={
                        "direction": direction, "symbol": signal.symbol, "price": entry_for_db,
                        "sl": sl_for_db, "tp": tp_for_db, "lots": fill_lots, "confidence": signal.confidence,
                        "regime": signal.regime or "unknown", "reason": signal.reason,
                        "account": self.account, "trading_mode": "swing", "ticket": ticket,
                    },
                ))

            return {
                "action": "executed",
                "direction": direction,
                "price": entry_for_db,
                "sl": sl_for_db,
                "tp": tp_for_db,
                "tp1": tp1_price_for_db,
                "lots": fill_lots,
                "confidence": signal.confidence,
                "regime": signal.regime,
                "ticket": ticket,
                "trade_id": trade_id,
                "order_result": order_result,
            }

        except Exception as e:
            logger.error("Order execution error: %s", e)
            return {"action": "error", "reason": str(e), "signal": signal}

    def run(self, interval: int = 300, max_cycles: int = 0) -> dict:
        """Run continuous trading loop.

        Args:
            interval: Seconds between cycles (default 300 = 5min for M5).
            max_cycles: Max cycles (0 = infinite).

        Returns:
            Dict with stats.
        """
        cycle = 0
        trades_opened = 0
        trades_closed = 0
        errors = 0
        holds = 0

        mode = "DRY-RUN" if self.dry_run else "LIVE"
        label = self._account_label
        logger.info(
            "Starting %s trader (interval=%ds, account=%s [%s], mode=%s)",
            mode, interval, self.account, label, mode,
        )

        while max_cycles == 0 or cycle < max_cycles:
            cycle += 1
            try:
                result = self.run_once()
                action = result.get("action", "unknown")

                if action in ("executed", "dry_run"):
                    trades_opened += 1
                elif action == "hold":
                    holds += 1
                elif action in ("order_failed", "error"):
                    errors += 1

                # Count closed trades from monitoring
                # (they're logged inside _monitor_positions)

            except Exception as e:
                logger.error("Trading cycle %d failed: %s", cycle, e)
                errors += 1

            logger.info(
                "Cycle %d complete (opened=%d, holds=%d, errors=%d, dd_blocked=%s)",
                cycle, trades_opened, holds, errors,
                self._drawdown_protector.is_blocked,
            )

            if max_cycles > 0 and cycle >= max_cycles:
                break

            logger.info("Sleeping %d seconds until next cycle...", interval)
            time.sleep(interval)

        return {
            "cycles": cycle,
            "trades_opened": trades_opened,
            "holds": holds,
            "errors": errors,
        }