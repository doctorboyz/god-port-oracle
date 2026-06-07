"""SQLite database schema and connection management for ML data collection."""

import sqlite3
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent.parent.parent / "data" / "oracle.db"

SCHEMA_SQL = """
-- Account configuration
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    broker_login TEXT NOT NULL DEFAULT '',
    broker_server TEXT NOT NULL DEFAULT 'Exness-MT5',
    balance REAL NOT NULL,
    leverage INTEGER NOT NULL,
    bridge_host TEXT NOT NULL DEFAULT 'localhost',
    bridge_port INTEGER NOT NULL,
    signal_group TEXT NOT NULL,
    is_active BOOLEAN DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
);

-- OHLCV candle data
CREATE TABLE IF NOT EXISTS candles (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL,
    symbol TEXT NOT NULL DEFAULT 'XAUUSD',
    timeframe TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    tick_volume INTEGER,
    spread REAL,
    trading_mode TEXT NOT NULL DEFAULT 'swing',
    strategy_id TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (account_id) REFERENCES accounts(id),
    UNIQUE(account_id, symbol, timeframe, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_candles_ts ON candles(account_id, symbol, timeframe, timestamp);

-- Indicator definitions (metadata)
CREATE TABLE IF NOT EXISTS indicator_definitions (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    group_name TEXT NOT NULL,
    description TEXT,
    parameters TEXT,
    unit TEXT,
    normal_range TEXT
);

-- Signal records
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY,
    timestamp TEXT NOT NULL,
    group_name TEXT NOT NULL,
    direction TEXT NOT NULL,
    confidence REAL NOT NULL,
    triggering_indicators TEXT NOT NULL,
    price REAL NOT NULL,
    account_id INTEGER NOT NULL,
    snapshot_id INTEGER,
    trading_mode TEXT NOT NULL DEFAULT 'swing',
    strategy_id TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (account_id) REFERENCES accounts(id)
);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(timestamp);
CREATE INDEX IF NOT EXISTS idx_signals_group ON signals(group_name);

-- Feature snapshots (core ML training data)
CREATE TABLE IF NOT EXISTS feature_snapshots (
    id INTEGER PRIMARY KEY,
    signal_id INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    price REAL NOT NULL,
    timeframe TEXT NOT NULL DEFAULT 'M5',
    session TEXT NOT NULL DEFAULT '',
    d1_trend TEXT NOT NULL DEFAULT '',
    h4_trend TEXT NOT NULL DEFAULT '',
    trading_mode TEXT NOT NULL DEFAULT 'swing',
    strategy_id TEXT NOT NULL DEFAULT '',

    -- Volume group
    obv REAL, obv_slope REAL,
    mfi REAL, mfi_signal TEXT,
    vwap_offset_pct REAL,
    volume_roc REAL,
    ad_line REAL, ad_line_slope REAL,
    cmf REAL,

    -- OB/OS group
    rsi REAL,
    stoch_k REAL, stoch_d REAL,
    williams_r REAL,
    cci REAL,
    demarker REAL,
    roc REAL,

    -- MA group
    sma_10 REAL, sma_20 REAL, sma_50 REAL,
    ema_9 REAL, ema_21 REAL, ema_50 REAL, ema_200 REAL,
    dema_21 REAL, tema_21 REAL,
    ichimoku_tenkan REAL, ichimoku_kijun REAL,
    ichimoku_senkou_a REAL, ichimoku_senkou_b REAL,
    ichimoku_chikou REAL,
    price_vs_cloud TEXT,

    -- Sentiment group
    tick_volume_ratio REAL,
    spread_ratio REAL,
    long_short_ratio REAL,
    session_strength REAL,

    -- Broky original indicators
    macd_hist REAL,
    adx REAL, plus_di REAL, minus_di REAL,
    boll_pct_b REAL, boll_bw REAL,
    atr REAL, atr_to_price REAL,

    -- Control variables
    balance_at_entry REAL,
    leverage_at_entry INTEGER,

    -- External sentiment (Fear & Greed, news)
    fear_greed_value REAL,
    gold_bias_strength REAL,
    news_sentiment REAL,

    -- Multi-timeframe price context
    h1_close REAL,
    h4_close REAL,
    d1_close REAL,
    m5_high REAL,
    m5_low REAL,

    FOREIGN KEY (signal_id) REFERENCES signals(id)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON feature_snapshots(timestamp);
CREATE INDEX IF NOT EXISTS idx_snapshots_group ON feature_snapshots(signal_id);

-- Orders
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    signal_id INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    symbol TEXT NOT NULL DEFAULT 'XAUUSD',
    direction TEXT NOT NULL,
    order_type TEXT NOT NULL DEFAULT 'market',
    lot_size REAL NOT NULL,
    entry_price REAL NOT NULL,
    stop_loss REAL,
    take_profit REAL,
    status TEXT NOT NULL DEFAULT 'pending',
    mt5_ticket INTEGER,
    created_at TEXT DEFAULT (datetime('now')),
    filled_at TEXT,
    FOREIGN KEY (signal_id) REFERENCES signals(id),
    FOREIGN KEY (account_id) REFERENCES accounts(id)
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

-- Completed trades with outcomes
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL,
    signal_id INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    direction TEXT NOT NULL,
    lot_size REAL NOT NULL,
    pnl REAL NOT NULL,
    pnl_pct REAL NOT NULL,
    pips REAL NOT NULL,
    exit_reason TEXT NOT NULL,
    holding_bars INTEGER,
    opened_at TEXT NOT NULL,
    closed_at TEXT NOT NULL,
    duration_seconds INTEGER,
    balance_at_entry REAL NOT NULL,
    leverage_at_entry INTEGER NOT NULL,
    FOREIGN KEY (order_id) REFERENCES orders(id),
    FOREIGN KEY (signal_id) REFERENCES signals(id),
    FOREIGN KEY (account_id) REFERENCES accounts(id)
);
CREATE INDEX IF NOT EXISTS idx_trades_group ON trades(signal_id);
CREATE INDEX IF NOT EXISTS idx_trades_outcome ON trades(pnl);

-- Live trade journal (for live/paper trading via MT5)
CREATE TABLE IF NOT EXISTS live_trades (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    direction TEXT NOT NULL,
    symbol TEXT NOT NULL DEFAULT 'XAUUSD',
    entry_price REAL NOT NULL,
    stop_loss REAL NOT NULL DEFAULT 0,
    take_profit REAL NOT NULL DEFAULT 0,
    lot_size REAL NOT NULL,
    confidence REAL NOT NULL,
    regime TEXT,
    session TEXT,
    d1_trend TEXT,
    reason TEXT,
    trading_mode TEXT NOT NULL DEFAULT 'swing',
    strategy_id TEXT NOT NULL DEFAULT '',
    signal_id INTEGER,
    ticket INTEGER,
    exit_price REAL,
    exit_time TEXT,
    pnl REAL,
    pnl_pct REAL,
    exit_reason TEXT,
    is_open INTEGER NOT NULL DEFAULT 1,

    -- MFE/MAE tracking (max favorable/adverse excursion)
    mfe REAL,
    mae REAL,
    mfe_pct REAL,
    mae_pct REAL,

    -- Exit context (regime/trend at exit time)
    exit_regime TEXT,
    exit_d1_trend TEXT,
    exit_h4_trend TEXT,

    -- Execution quality
    spread_at_entry REAL,
    slippage REAL,
    atr_at_entry REAL,

    -- ML filter info (what the model said before entry)
    ml_risk_multiplier REAL,
    ml_risk_reason TEXT,
    ml_model_version TEXT,
    ml_loss_proba REAL,
    ml_model_used TEXT,

    -- Calendar context (news/events near entry)
    minutes_to_next_event INTEGER,
    next_event_type TEXT,
    next_event_impact TEXT,

    -- Signal intermediate scores (debugging + feature importance)
    indicator_scores_json TEXT,

    -- Partial TP tracking (TP1 → Scale-In)
    tp1_price REAL,
    parent_trade_id INTEGER,
    tp_level INTEGER DEFAULT 1,
    remaining_lots REAL,

    -- Trading parameters (what config was used for this trade)
    atr_multiplier REAL,
    rr_ratio REAL,
    min_confidence_threshold REAL,

    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (account_id) REFERENCES accounts(id)
);
CREATE INDEX IF NOT EXISTS idx_live_trades_open ON live_trades(is_open, account_id);
CREATE INDEX IF NOT EXISTS idx_live_trades_ts ON live_trades(timestamp);

-- ML experiment tracking
CREATE TABLE IF NOT EXISTS ml_experiments (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    config TEXT NOT NULL,
    feature_columns TEXT NOT NULL,
    group_filter TEXT,
    account_filter TEXT,
    min_samples INTEGER NOT NULL DEFAULT 100,
    train_start TEXT,
    train_end TEXT,
    test_start TEXT,
    test_end TEXT,
    results TEXT,
    model_path TEXT,
    win_rate REAL,
    profit_factor REAL,
    total_trades INTEGER,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Trade outcomes for ML training (linked trade + signal + features)
CREATE TABLE IF NOT EXISTS trade_outcomes (
    id INTEGER PRIMARY KEY,
    trade_id INTEGER NOT NULL UNIQUE,
    signal_id INTEGER,
    snapshot_id INTEGER,
    account_id INTEGER NOT NULL,
    symbol TEXT NOT NULL DEFAULT 'XAUUSD',
    direction TEXT NOT NULL,
    trading_mode TEXT NOT NULL DEFAULT 'swing',
    strategy_id TEXT NOT NULL DEFAULT '',
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    profit REAL NOT NULL,
    profit_pct REAL NOT NULL,
    outcome_label TEXT NOT NULL CHECK(outcome_label IN ('WIN', 'LOSS', 'BREAKEVEN')),
    holding_minutes INTEGER,
    exit_reason TEXT,
    features_json TEXT,

    -- MFE/MAE tracking (max favorable/adverse excursion)
    mfe REAL,
    mae REAL,
    mfe_pct REAL,
    mae_pct REAL,

    -- Exit context
    exit_regime TEXT,
    exit_d1_trend TEXT,
    exit_h4_trend TEXT,

    -- pnl aliases (ISSUE-014: same as profit/profit_pct)
    pnl REAL,
    pnl_pct REAL,

    -- Trading parameters (what config was used for this trade)
    atr_multiplier REAL,
    rr_ratio REAL,
    min_confidence_threshold REAL,

    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (trade_id) REFERENCES live_trades(id),
    FOREIGN KEY (signal_id) REFERENCES signals(id),
    FOREIGN KEY (snapshot_id) REFERENCES feature_snapshots(id),
    FOREIGN KEY (account_id) REFERENCES accounts(id)
);
CREATE INDEX IF NOT EXISTS idx_trade_outcomes_label ON trade_outcomes(outcome_label);
CREATE INDEX IF NOT EXISTS idx_trade_outcomes_mode ON trade_outcomes(trading_mode, strategy_id);

-- Rejected signals (for tracking survivorship bias)
CREATE TABLE IF NOT EXISTS rejected_signals (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    direction TEXT NOT NULL,
    confidence REAL NOT NULL,
    price REAL NOT NULL,
    rejection_reason TEXT NOT NULL,
    trading_mode TEXT NOT NULL DEFAULT 'swing',
    strategy_id TEXT NOT NULL DEFAULT '',
    regime TEXT,
    session TEXT,
    d1_trend TEXT,
    signal_json TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (account_id) REFERENCES accounts(id)
);
CREATE INDEX IF NOT EXISTS idx_rejected_ts ON rejected_signals(timestamp);
CREATE INDEX IF NOT EXISTS idx_rejected_reason ON rejected_signals(rejection_reason);
"""


def get_connection(db_path: Optional[Path | str] = None) -> sqlite3.Connection:
    """Get a connection to the SQLite database with WAL mode."""
    path = Path(db_path) if isinstance(db_path, str) else (db_path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: Optional[Path] = None) -> None:
    """Initialize the database schema."""
    conn = get_connection(db_path)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
        # Migrate: add trading_mode and strategy_id columns if missing
        _migrate_trading_columns(conn)
        conn.commit()

        # Verify data integrity on startup
        integrity = check_data_integrity(db_path)
        if not integrity.get("healthy", True):
            import logging
            logging.getLogger(__name__).warning(
                "Data integrity check failed on startup. Run backfill_trade_outcomes() to fix."
            )
    finally:
        conn.close()


def check_data_integrity(db_path: Optional[Path | str] = None) -> dict:
    """Verify that required trading parameters are populated in all rows.

    Returns a dict with table names as keys and integrity stats as values.
    Logs warnings for any NULL columns found.
    """
    import logging
    logger = logging.getLogger(__name__)

    conn = get_connection(db_path)
    results = {}

    try:
        # Check live_trades trading params
        lt_total = conn.execute("SELECT COUNT(*) FROM live_trades").fetchone()[0]
        lt_null_atr = conn.execute("SELECT COUNT(*) FROM live_trades WHERE atr_multiplier IS NULL").fetchone()[0]
        lt_null_rr = conn.execute("SELECT COUNT(*) FROM live_trades WHERE rr_ratio IS NULL").fetchone()[0]
        lt_null_conf = conn.execute("SELECT COUNT(*) FROM live_trades WHERE min_confidence_threshold IS NULL").fetchone()[0]

        results["live_trades"] = {
            "total": lt_total,
            "null_atr_multiplier": lt_null_atr,
            "null_rr_ratio": lt_null_rr,
            "null_min_confidence_threshold": lt_null_conf,
            "healthy": lt_null_atr == 0 and lt_null_rr == 0 and lt_null_conf == 0,
        }

        # Check trade_outcomes trading params
        to_total = conn.execute("SELECT COUNT(*) FROM trade_outcomes").fetchone()[0]
        to_null_atr = conn.execute("SELECT COUNT(*) FROM trade_outcomes WHERE atr_multiplier IS NULL").fetchone()[0]
        to_null_rr = conn.execute("SELECT COUNT(*) FROM trade_outcomes WHERE rr_ratio IS NULL").fetchone()[0]
        to_null_conf = conn.execute("SELECT COUNT(*) FROM trade_outcomes WHERE min_confidence_threshold IS NULL").fetchone()[0]

        results["trade_outcomes"] = {
            "total": to_total,
            "null_atr_multiplier": to_null_atr,
            "null_rr_ratio": to_null_rr,
            "null_min_confidence_threshold": to_null_conf,
            "healthy": to_null_atr == 0 and to_null_rr == 0 and to_null_conf == 0,
        }

        # Check features_json has d1_trend and h4_trend
        to_no_d1 = conn.execute(
            "SELECT COUNT(*) FROM trade_outcomes WHERE features_json IS NOT NULL AND features_json NOT LIKE '%d1_trend%'"
        ).fetchone()[0]
        to_no_h4 = conn.execute(
            "SELECT COUNT(*) FROM trade_outcomes WHERE features_json IS NOT NULL AND features_json NOT LIKE '%h4_trend%'"
        ).fetchone()[0]

        results["trade_outcomes"]["null_d1_trend_in_json"] = to_no_d1
        results["trade_outcomes"]["null_h4_trend_in_json"] = to_no_h4

        # Overall health
        all_healthy = results["live_trades"]["healthy"] and results["trade_outcomes"]["healthy"]
        results["healthy"] = all_healthy

        if not all_healthy:
            logger.warning(
                f"Data integrity check FAILED: live_trades has {lt_null_atr} NULL atr_multiplier, "
                f"{lt_null_rr} NULL rr_ratio, {lt_null_conf} NULL min_confidence_threshold; "
                f"trade_outcomes has {to_null_atr} NULL atr_multiplier, "
                f"{to_null_rr} NULL rr_ratio, {to_null_conf} NULL min_confidence_threshold. "
                f"Run backfill_trade_outcomes() to fix."
            )
        else:
            logger.info(f"Data integrity check PASSED: {lt_total} live_trades, {to_total} trade_outcoes all have trading params")

    finally:
        conn.close()

    return results


def _migrate_trading_columns(conn: sqlite3.Connection) -> None:
    """Add new columns to existing tables for evolving schema."""
    migrations = [
        # Original migrations
        ("live_trades", "trading_mode", "TEXT NOT NULL DEFAULT 'swing'"),
        ("live_trades", "strategy_id", "TEXT NOT NULL DEFAULT ''"),
        ("live_trades", "signal_id", "INTEGER"),
        ("signals", "trading_mode", "TEXT NOT NULL DEFAULT 'swing'"),
        ("signals", "strategy_id", "TEXT NOT NULL DEFAULT ''"),
        ("feature_snapshots", "trading_mode", "TEXT NOT NULL DEFAULT 'swing'"),
        ("feature_snapshots", "strategy_id", "TEXT NOT NULL DEFAULT ''"),
        ("feature_snapshots", "h4_trend", "TEXT NOT NULL DEFAULT ''"),
        ("candles", "trading_mode", "TEXT NOT NULL DEFAULT 'swing'"),
        ("candles", "strategy_id", "TEXT NOT NULL DEFAULT ''"),
        # MFE/MAE tracking
        ("live_trades", "mfe", "REAL"),
        ("live_trades", "mae", "REAL"),
        ("live_trades", "mfe_pct", "REAL"),
        ("live_trades", "mae_pct", "REAL"),
        # Exit context
        ("live_trades", "exit_regime", "TEXT"),
        ("live_trades", "exit_d1_trend", "TEXT"),
        ("live_trades", "exit_h4_trend", "TEXT"),
        # Execution quality
        ("live_trades", "spread_at_entry", "REAL"),
        ("live_trades", "slippage", "REAL"),
        ("live_trades", "atr_at_entry", "REAL"),
        # ML filter info
        ("live_trades", "ml_risk_multiplier", "REAL"),
        ("live_trades", "ml_risk_reason", "TEXT"),
        ("live_trades", "ml_model_version", "TEXT"),
        ("live_trades", "ml_loss_proba", "REAL"),
        ("live_trades", "ml_model_used", "TEXT"),
        # Calendar context
        ("live_trades", "minutes_to_next_event", "INTEGER"),
        ("live_trades", "next_event_type", "TEXT"),
        ("live_trades", "next_event_impact", "TEXT"),
        # Signal intermediate scores
        ("live_trades", "indicator_scores_json", "TEXT"),
        # Multi-TF price in feature_snapshots
        ("feature_snapshots", "h1_close", "REAL"),
        ("feature_snapshots", "h4_close", "REAL"),
        ("feature_snapshots", "d1_close", "REAL"),
        ("feature_snapshots", "m5_high", "REAL"),
        ("feature_snapshots", "m5_low", "REAL"),
        # Partial TP tracking
        ("live_trades", "tp1_price", "REAL"),
        ("live_trades", "parent_trade_id", "INTEGER"),
        ("live_trades", "tp_level", "INTEGER DEFAULT 1"),
        ("live_trades", "remaining_lots", "REAL"),
        # MFE/MAE in trade_outcomes
        ("trade_outcomes", "mfe", "REAL"),
        ("trade_outcomes", "mae", "REAL"),
        ("trade_outcomes", "mfe_pct", "REAL"),
        ("trade_outcomes", "mae_pct", "REAL"),
        ("trade_outcomes", "exit_regime", "TEXT"),
        ("trade_outcomes", "exit_d1_trend", "TEXT"),
        ("trade_outcomes", "exit_h4_trend", "TEXT"),
        # pnl/pnl_pct aliases in trade_outcomes (ISSUE-014)
        ("trade_outcomes", "pnl", "REAL"),
        ("trade_outcomes", "pnl_pct", "REAL"),
        # Trading parameters per trade (what config was used)
        ("live_trades", "atr_multiplier", "REAL"),
        ("live_trades", "rr_ratio", "REAL"),
        ("live_trades", "min_confidence_threshold", "REAL"),
        ("trade_outcomes", "atr_multiplier", "REAL"),
        ("trade_outcomes", "rr_ratio", "REAL"),
        ("trade_outcomes", "min_confidence_threshold", "REAL"),
    ]
    for table, column, col_type in migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        except sqlite3.OperationalError:
            pass  # Column already exists
    # Add indexes for new columns
    index_migrations = [
        ("idx_live_trades_mode", "live_trades(trading_mode, strategy_id)"),
        ("idx_signals_mode", "signals(trading_mode, strategy_id)"),
        ("idx_rejected_ts", "rejected_signals(timestamp)"),
        ("idx_rejected_reason", "rejected_signals(rejection_reason)"),
    ]
    for idx_name, idx_def in index_migrations:
        try:
            conn.execute(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {idx_def}")
        except sqlite3.OperationalError:
            pass


def insert_account(
    name: str,
    balance: float,
    leverage: int,
    bridge_host: str,
    bridge_port: int,
    signal_group: str,
    broker_login: str = "",
    broker_server: str = "Exness-MT5",
    db_path: Optional[Path] = None,
) -> int:
    """Insert an account configuration and return its ID."""
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            """INSERT INTO accounts (name, broker_login, broker_server, balance, leverage,
               bridge_host, bridge_port, signal_group)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, broker_login, broker_server, balance, leverage, bridge_host, bridge_port, signal_group),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def insert_signal(
    timestamp: str,
    group_name: str,
    direction: str,
    confidence: float,
    triggering_indicators: str,
    price: float,
    account_id: int,
    trading_mode: str = "swing",
    strategy_id: str = "",
    db_path: Optional[Path] = None,
) -> int:
    """Insert a signal record and return its ID."""
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            """INSERT INTO signals (timestamp, group_name, direction, confidence,
               triggering_indicators, price, account_id, trading_mode, strategy_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (timestamp, group_name, direction, confidence, triggering_indicators,
             price, account_id, trading_mode, strategy_id),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def insert_feature_snapshot(
    signal_id: int,
    timestamp: str,
    price: float,
    timeframe: str = "M5",
    session: str = "",
    d1_trend: str = "",
    h4_trend: str = "",
    trading_mode: str = "swing",
    strategy_id: str = "",
    db_path: Optional[Path] = None,
    **indicator_values: float | str | int | None,
) -> int:
    """Insert a feature snapshot with all indicator values."""
    conn = get_connection(db_path)
    try:
        columns = ["signal_id", "timestamp", "price", "timeframe", "session", "d1_trend", "h4_trend", "trading_mode", "strategy_id"]
        values: list[float | str | int | None] = [signal_id, timestamp, price, timeframe, session, d1_trend, h4_trend, trading_mode, strategy_id]

        for key, val in indicator_values.items():
            if key in _SNAPSHOT_COLUMNS:
                columns.append(key)
                values.append(val)

        placeholders = ", ".join(["?"] * len(columns))
        cols = ", ".join(columns)
        cursor = conn.execute(
            f"INSERT INTO feature_snapshots ({cols}) VALUES ({placeholders})",
            values,
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def insert_trade(
    order_id: int,
    signal_id: int,
    account_id: int,
    entry_price: float,
    exit_price: float,
    direction: str,
    lot_size: float,
    pnl: float,
    pnl_pct: float,
    pips: float,
    exit_reason: str,
    holding_bars: int,
    opened_at: str,
    closed_at: str,
    duration_seconds: int,
    balance_at_entry: float,
    leverage_at_entry: int,
    db_path: Optional[Path] = None,
) -> int:
    """Insert a completed trade record."""
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            """INSERT INTO trades (order_id, signal_id, account_id, entry_price, exit_price,
               direction, lot_size, pnl, pnl_pct, pips, exit_reason, holding_bars,
               opened_at, closed_at, duration_seconds, balance_at_entry, leverage_at_entry)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (order_id, signal_id, account_id, entry_price, exit_price, direction, lot_size,
             pnl, pnl_pct, pips, exit_reason, holding_bars, opened_at, closed_at,
             duration_seconds, balance_at_entry, leverage_at_entry),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


# Valid columns for feature_snapshots (excluding id, signal_id, timestamp, price, timeframe, session, d1_trend)
_SNAPSHOT_COLUMNS: set[str] = {
    "obv", "obv_slope", "mfi", "mfi_signal", "vwap_offset_pct", "volume_roc",
    "ad_line", "ad_line_slope", "cmf",
    "rsi", "stoch_k", "stoch_d", "williams_r", "cci", "demarker", "roc",
    "sma_10", "sma_20", "sma_50",
    "ema_9", "ema_21", "ema_50", "ema_200",
    "dema_21", "tema_21",
    "ichimoku_tenkan", "ichimoku_kijun", "ichimoku_senkou_a", "ichimoku_senkou_b",
    "ichimoku_chikou", "price_vs_cloud",
    "tick_volume_ratio", "spread_ratio", "long_short_ratio", "session_strength",
    "macd_hist", "adx", "plus_di", "minus_di", "boll_pct_b", "boll_bw",
    "atr", "atr_to_price",
    "balance_at_entry", "leverage_at_entry",
    "fear_greed_value", "gold_bias_strength", "news_sentiment",
    # Multi-timeframe price context
    "h1_close", "h4_close", "d1_close", "m5_high", "m5_low",
}


def get_snapshot_count(db_path: Optional[Path] = None) -> int:
    """Return total number of feature snapshots in the database."""
    conn = get_connection(db_path)
    try:
        cursor = conn.execute("SELECT COUNT(*) FROM feature_snapshots")
        return cursor.fetchone()[0]
    finally:
        conn.close()


def query_snapshots_for_training(
    min_samples: int = 100,
    group_filter: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> list[dict]:
    """Query feature snapshots for ML training.

    Returns list of dicts with all indicator columns plus timestamp and price.
    """
    conn = get_connection(db_path)
    try:
        query = """
            SELECT fs.*, s.group_name, s.direction, s.confidence as signal_confidence
            FROM feature_snapshots fs
            JOIN signals s ON fs.signal_id = s.id
            WHERE 1=1
        """
        params: list = []

        if group_filter:
            query += " AND s.group_name = ?"
            params.append(group_filter)

        query += " ORDER BY fs.timestamp ASC"

        cursor = conn.execute(query, params)
        columns = [desc[0] for desc in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        return rows
    finally:
        conn.close()


def insert_ml_experiment(
    name: str,
    config: str,
    feature_columns: str,
    min_samples: int = 100,
    description: str = "",
    group_filter: Optional[str] = None,
    account_filter: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> int:
    """Insert an ML experiment record and return its ID."""
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            """INSERT INTO ml_experiments
               (name, description, started_at, status, config, feature_columns,
                group_filter, account_filter, min_samples)
               VALUES (?, ?, datetime('now'), 'running', ?, ?, ?, ?, ?)""",
            (name, description, config, feature_columns,
             group_filter, account_filter, min_samples),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def update_ml_experiment(
    experiment_id: int,
    status: str,
    results: Optional[str] = None,
    model_path: Optional[str] = None,
    win_rate: Optional[float] = None,
    profit_factor: Optional[float] = None,
    total_trades: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> None:
    """Update an ML experiment with results."""
    conn = get_connection(db_path)
    try:
        updates = ["status = ?", "completed_at = datetime('now')"]
        values: list = [status]

        if results is not None:
            updates.append("results = ?")
            values.append(results)
        if model_path is not None:
            updates.append("model_path = ?")
            values.append(model_path)
        if win_rate is not None:
            updates.append("win_rate = ?")
            values.append(win_rate)
        if profit_factor is not None:
            updates.append("profit_factor = ?")
            values.append(profit_factor)
        if total_trades is not None:
            updates.append("total_trades = ?")
            values.append(total_trades)

        values.append(experiment_id)
        conn.execute(
            f"UPDATE ml_experiments SET {', '.join(updates)} WHERE id = ?",
            values,
        )
        conn.commit()
    finally:
        conn.close()


def insert_live_trade(
    account_id: int,
    timestamp: str,
    direction: str,
    entry_price: float,
    stop_loss: float = 0.0,
    take_profit: float = 0.0,
    lot_size: float = 0.01,
    confidence: float = 0.0,
    regime: str = "unknown",
    session: str = "unknown",
    d1_trend: str = "unknown",
    reason: str = "",
    ticket: int | None = None,
    symbol: str = "XAUUSD",
    trading_mode: str = "swing",
    strategy_id: str = "",
    signal_id: int | None = None,
    # Execution quality
    spread_at_entry: float | None = None,
    slippage: float | None = None,
    atr_at_entry: float | None = None,
    # ML filter info
    ml_risk_multiplier: float | None = None,
    ml_risk_reason: str | None = None,
    ml_model_version: str | None = None,
    ml_loss_proba: float | None = None,
    ml_model_used: str | None = None,
    # Calendar context
    minutes_to_next_event: int | None = None,
    next_event_type: str | None = None,
    next_event_impact: str | None = None,
    # Signal intermediate scores
    indicator_scores_json: str | None = None,
    # Partial TP tracking
    tp1_price: float | None = None,
    tp_level: int = 1,
    parent_trade_id: int | None = None,
    # Trading parameters (what config was used)
    atr_multiplier: float | None = None,
    rr_ratio: float | None = None,
    min_confidence_threshold: float | None = None,
    db_path: Optional[Path] = None,
) -> int:
    """Insert a live trade record and return its ID."""
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            """INSERT INTO live_trades
               (account_id, timestamp, direction, symbol, entry_price, stop_loss,
                take_profit, lot_size, confidence, regime, session, d1_trend,
                reason, trading_mode, strategy_id, signal_id, ticket, is_open,
                spread_at_entry, slippage, atr_at_entry,
                ml_risk_multiplier, ml_risk_reason, ml_model_version,
                ml_loss_proba, ml_model_used,
                minutes_to_next_event, next_event_type, next_event_impact,
                indicator_scores_json, tp1_price, tp_level, parent_trade_id,
                atr_multiplier, rr_ratio, min_confidence_threshold)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1,
                       ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (account_id, timestamp, direction, symbol, entry_price, stop_loss,
             take_profit, lot_size, confidence, regime, session, d1_trend,
             reason, trading_mode, strategy_id, signal_id, ticket,
             spread_at_entry, slippage, atr_at_entry,
             ml_risk_multiplier, ml_risk_reason, ml_model_version,
             ml_loss_proba, ml_model_used,
             minutes_to_next_event, next_event_type, next_event_impact,
             indicator_scores_json, tp1_price, tp_level, parent_trade_id,
             atr_multiplier, rr_ratio, min_confidence_threshold),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def insert_rejected_signal(
    account_id: int,
    timestamp: str,
    direction: str,
    confidence: float,
    price: float,
    rejection_reason: str,
    trading_mode: str = "swing",
    strategy_id: str = "",
    regime: str | None = None,
    session: str | None = None,
    d1_trend: str | None = None,
    signal_json: str | None = None,
    db_path: Optional[Path] = None,
) -> int:
    """Insert a rejected signal record and return its ID.

    Tracks signals that were generated but rejected (low confidence,
    circuit breaker, ML filter, etc.) for survivorship bias analysis.
    """
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            """INSERT INTO rejected_signals
               (account_id, timestamp, direction, confidence, price,
                rejection_reason, trading_mode, strategy_id, regime,
                session, d1_trend, signal_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (account_id, timestamp, direction, confidence, price,
             rejection_reason, trading_mode, strategy_id, regime,
             session, d1_trend, signal_json),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def get_latest_signal_id(
    account_id: int,
    db_path: Optional[Path] = None,
) -> int | None:
    """Get the most recent signal_id from feature_snapshots for this account.

    Feature snapshots are linked to signals. We join through signals to find
    collector snapshots that match the account. Returns the most recent signal_id.
    """
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            """SELECT fs.signal_id FROM feature_snapshots fs
               JOIN signals s ON fs.signal_id = s.id
               WHERE s.account_id = ?
               ORDER BY fs.timestamp DESC LIMIT 1""",
            (account_id,),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def get_open_trades(
    account_id: int,
    db_path: Optional[Path] = None,
) -> list[dict]:
    """Get all open trades for an account."""
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            """SELECT * FROM live_trades WHERE account_id = ? AND is_open = 1
               ORDER BY timestamp DESC""",
            (account_id,),
        )
        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        conn.close()


def close_live_trade(
    trade_id: int,
    exit_price: float,
    exit_time: str,
    pnl: float,
    pnl_pct: float,
    exit_reason: str,
    # MFE/MAE tracking
    mfe: float | None = None,
    mae: float | None = None,
    mfe_pct: float | None = None,
    mae_pct: float | None = None,
    # Exit context
    exit_regime: str | None = None,
    exit_d1_trend: str | None = None,
    exit_h4_trend: str | None = None,
    # Partial TP tracking
    tp1_price: float | None = None,
    tp_level: int = 1,
    parent_trade_id: int | None = None,
    remaining_lots: float | None = None,
    db_path: Optional[Path] = None,
) -> None:
    """Close a live trade by marking it as closed with exit details.

    Also creates a trade_outcomes row with the trading parameters from live_trades.
    """
    import json
    from datetime import datetime

    conn = get_connection(db_path)
    try:
        conn.execute(
            """UPDATE live_trades SET
               exit_price = ?, exit_time = ?, pnl = ?, pnl_pct = ?,
               exit_reason = ?, is_open = 0,
               mfe = ?, mae = ?, mfe_pct = ?, mae_pct = ?,
               exit_regime = ?, exit_d1_trend = ?, exit_h4_trend = ?,
               tp1_price = ?, tp_level = ?, parent_trade_id = ?, remaining_lots = ?
               WHERE id = ?""",
            (exit_price, exit_time, pnl, pnl_pct, exit_reason,
             mfe, mae, mfe_pct, mae_pct,
             exit_regime, exit_d1_trend, exit_h4_trend,
             tp1_price, tp_level, parent_trade_id, remaining_lots,
             trade_id),
        )

        # Create a trade_outcomes row from the closed trade
        # Skip if already exists (idempotent)
        existing = conn.execute(
            "SELECT id FROM trade_outcomes WHERE trade_id = ?", (trade_id,)
        ).fetchone()
        if not existing:
            trade_row = conn.execute(
                "SELECT * FROM live_trades WHERE id = ?", (trade_id,)
            ).fetchone()
            if trade_row:
                trade_cols = [desc[0] for desc in conn.execute(
                    "SELECT * FROM live_trades LIMIT 0"
                ).description]
                t = dict(zip(trade_cols, trade_row))

                # Determine outcome label
                outcome = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "BREAKEVEN")

                # Calculate holding minutes
                holding_minutes = None
                if t.get("exit_time") and t.get("timestamp"):
                    try:
                        entry_dt = datetime.fromisoformat(t["timestamp"])
                        exit_dt = datetime.fromisoformat(t["exit_time"])
                        holding_minutes = int((exit_dt - entry_dt).total_seconds() / 60)
                    except (ValueError, TypeError):
                        pass

                # Build features_json from trade context
                features = {}
                if t.get("regime"):
                    features["regime"] = t["regime"]
                if t.get("d1_trend"):
                    features["d1_trend"] = t["d1_trend"]
                if t.get("h4_trend"):
                    features["h4_trend"] = t["h4_trend"]
                if t.get("session"):
                    features["session"] = t["session"]
                if t.get("atr_at_entry"):
                    features["atr_at_entry"] = t["atr_at_entry"]
                if t.get("confidence"):
                    features["confidence"] = t["confidence"]
                if t.get("indicator_scores_json"):
                    try:
                        scores = json.loads(t["indicator_scores_json"])
                        features.update(scores)
                    except (json.JSONDecodeError, TypeError):
                        pass
                if t.get("ml_risk_multiplier"):
                    features["ml_risk_multiplier"] = t["ml_risk_multiplier"]
                if t.get("ml_risk_reason"):
                    features["ml_risk_reason"] = t["ml_risk_reason"]
                if t.get("ml_loss_proba"):
                    features["ml_loss_proba"] = t["ml_loss_proba"]
                # Include trading parameters in features_json for ML
                if t.get("atr_multiplier"):
                    features["atr_multiplier"] = t["atr_multiplier"]
                if t.get("rr_ratio"):
                    features["rr_ratio"] = t["rr_ratio"]
                if t.get("min_confidence_threshold"):
                    features["min_confidence_threshold"] = t["min_confidence_threshold"]
                features_json = json.dumps(features, separators=(",", ":")) if features else None

                # Check which columns exist in trade_outcomes
                to_cols = {r[1] for r in conn.execute("PRAGMA table_info(trade_outcomes)").fetchall()}
                has_trading_params = "atr_multiplier" in to_cols

                if has_trading_params:
                    conn.execute(
                        """INSERT INTO trade_outcomes
                           (trade_id, signal_id, snapshot_id, account_id, symbol, direction,
                            trading_mode, strategy_id, entry_price, exit_price, profit,
                            profit_pct, outcome_label, holding_minutes, exit_reason, features_json,
                            mfe, mae, mfe_pct, mae_pct,
                            exit_regime, exit_d1_trend, exit_h4_trend,
                            pnl, pnl_pct,
                            atr_multiplier, rr_ratio, min_confidence_threshold)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                   ?, ?, ?, ?, ?, ?, ?,
                                   ?, ?,
                                   ?, ?, ?)""",
                        (trade_id, t.get("signal_id"), None, t.get("account_id"),
                         t.get("symbol", "XAUUSD"), t.get("direction"),
                         t.get("trading_mode", "swing"), t.get("strategy_id"),
                         t.get("entry_price"), exit_price, pnl, pnl_pct,
                         outcome, holding_minutes, exit_reason, features_json,
                         mfe, mae, mfe_pct, mae_pct,
                         exit_regime, exit_d1_trend, exit_h4_trend,
                         pnl, pnl_pct,
                         t.get("atr_multiplier"), t.get("rr_ratio"), t.get("min_confidence_threshold")),
                    )
                else:
                    conn.execute(
                        """INSERT INTO trade_outcomes
                           (trade_id, signal_id, snapshot_id, account_id, symbol, direction,
                            trading_mode, strategy_id, entry_price, exit_price, profit,
                            profit_pct, outcome_label, holding_minutes, exit_reason, features_json,
                            mfe, mae, mfe_pct, mae_pct,
                            exit_regime, exit_d1_trend, exit_h4_trend,
                            pnl, pnl_pct)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                   ?, ?, ?, ?, ?, ?, ?,
                                   ?, ?)""",
                        (trade_id, t.get("signal_id"), None, t.get("account_id"),
                         t.get("symbol", "XAUUSD"), t.get("direction"),
                         t.get("trading_mode", "swing"), t.get("strategy_id"),
                         t.get("entry_price"), exit_price, pnl, pnl_pct,
                         outcome, holding_minutes, exit_reason, features_json,
                         mfe, mae, mfe_pct, mae_pct,
                         exit_regime, exit_d1_trend, exit_h4_trend,
                         pnl, pnl_pct),
                    )

        conn.commit()
    finally:
        conn.close()


def get_closed_trades(
    account_id: int,
    db_path: Optional[Path] = None,
    limit: int = 100,
) -> list[dict]:
    """Get recent closed trades for an account (for Kelly criterion etc.)."""
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            """SELECT * FROM live_trades
               WHERE account_id = ? AND is_open = 0 AND pnl IS NOT NULL
               ORDER BY exit_time DESC LIMIT ?""",
            (account_id, limit),
        )
        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        conn.close()


def query_trade_outcomes(
    min_confidence: float = 0.0,
    exclude_phantom: bool = True,
    db_path: Optional[Path] = None,
) -> list[dict]:
    """Query feature snapshots joined with live trade outcomes for ML training.

    Returns one row per trade with all features + PnL outcome.
    """
    conn = get_connection(db_path)
    try:
        query = """
            SELECT
                fs.*,
                lt.pnl, lt.pnl_pct, lt.exit_reason, lt.direction,
                lt.confidence as signal_confidence, lt.regime, lt.account_id
            FROM feature_snapshots fs
            JOIN signals s ON fs.signal_id = s.id
            JOIN live_trades lt ON lt.signal_id = s.id
            WHERE lt.is_open = 0
              AND lt.pnl IS NOT NULL
        """
        params: list = []

        if exclude_phantom:
            query += " AND lt.exit_reason != 'phantom'"

        if min_confidence > 0:
            query += " AND lt.confidence >= ?"
            params.append(min_confidence)

        query += " ORDER BY fs.timestamp ASC"

        cursor = conn.execute(query, params)
        columns = [desc[0] for desc in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        return rows
    finally:
        conn.close()


def backfill_trade_outcomes(db_path: Optional[Path] = None) -> dict:
    """Backfill trade_outcomes from live_trades + signals + feature_snapshots.

    Links trades to signals by signal_id when available, otherwise by
    timestamp proximity (±60s, same account). Extracts feature snapshots
    for each linked signal. Propagates MFE/MAE and exit context from live_trades.

    Returns:
        Dict with stats: {linked, skipped, total, outcomes: {WIN, LOSS, BREAKEVEN}}
    """
    import json
    from datetime import datetime, timedelta

    conn = get_connection(db_path)
    stats = {"linked": 0, "skipped": 0, "updated": 0, "total": 0, "outcomes": {"WIN": 0, "LOSS": 0, "BREAKEVEN": 0}}

    try:
        # Check if trading parameter columns exist (may not in older schemas)
        all_lt_cols = {r[1] for r in conn.execute("PRAGMA table_info(live_trades)").fetchall()}
        all_to_cols = {r[1] for r in conn.execute("PRAGMA table_info(trade_outcomes)").fetchall()}
        has_trading_params = "atr_multiplier" in all_lt_cols and "atr_multiplier" in all_to_cols

        # Build SELECT columns dynamically based on schema
        base_select = """SELECT id, account_id, timestamp, direction, symbol, trading_mode,
                      strategy_id, entry_price, exit_price, pnl, pnl_pct,
                      exit_reason, signal_id, exit_time,
                      mfe, mae, mfe_pct, mae_pct,
                      exit_regime, exit_d1_trend, exit_h4_trend"""
        if has_trading_params:
            base_select += ", atr_multiplier, rr_ratio, min_confidence_threshold"

        # Get all closed trades with results (including MFE/MAE)
        trades = conn.execute(
            f"""{base_select}
               FROM live_trades
               WHERE is_open = 0 AND pnl IS NOT NULL AND exit_price IS NOT NULL
               ORDER BY id"""
        ).fetchall()

        stats["total"] = len(trades)

        for t in trades:
            if has_trading_params:
                t_id, acc_id, ts, direction, symbol, mode, strategy = t[0], t[1], t[2], t[3], t[4], t[5], t[6]
                entry_price, exit_price, pnl, pnl_pct, exit_reason = t[7], t[8], t[9], t[10], t[11]
                signal_id, exit_time = t[12], t[13]
                mfe, mae, mfe_pct, mae_pct = t[14], t[15], t[16], t[17]
                exit_regime, exit_d1_trend, exit_h4_trend = t[18], t[19], t[20]
                lt_atr, lt_rr, lt_conf = t[21], t[22], t[23]
            else:
                t_id, acc_id, ts, direction, symbol, mode, strategy = t[0], t[1], t[2], t[3], t[4], t[5], t[6]
                entry_price, exit_price, pnl, pnl_pct, exit_reason = t[7], t[8], t[9], t[10], t[11]
                signal_id, exit_time = t[12], t[13]
                mfe, mae, mfe_pct, mae_pct = t[14], t[15], t[16], t[17]
                exit_regime, exit_d1_trend, exit_h4_trend = t[18], t[19], t[20]
                lt_atr, lt_rr, lt_conf = None, None, None

            # Check if already in trade_outcomes
            existing = conn.execute(
                "SELECT id, atr_multiplier FROM trade_outcomes WHERE trade_id = ?", (t_id,)
            ).fetchone()
            if existing:
                # Update trading params if they're NULL but available from live_trades
                if has_trading_params and existing[1] is None and lt_atr is not None:
                    conn.execute(
                        """UPDATE trade_outcomes
                           SET atr_multiplier = ?, rr_ratio = ?, min_confidence_threshold = ?
                           WHERE trade_id = ?""",
                        (lt_atr, lt_rr, lt_conf, t_id),
                    )
                    stats["updated"] = stats.get("updated", 0) + 1
                else:
                    stats["skipped"] += 1
                continue

            # Determine signal_id if not directly linked
            snapshot_id = None
            resolved_signal_id = signal_id

            if not resolved_signal_id:
                # Match by timestamp proximity (closest signal within ±60s)
                try:
                    ts_dt = datetime.fromisoformat(ts)
                    ts_lower = (ts_dt - timedelta(seconds=60)).isoformat()
                    ts_upper = (ts_dt + timedelta(seconds=60)).isoformat()
                except (ValueError, TypeError):
                    stats["skipped"] += 1
                    continue

                match = conn.execute(
                    """SELECT s.id, fs.id
                       FROM signals s
                       LEFT JOIN feature_snapshots fs ON fs.signal_id = s.id
                       WHERE s.account_id = ?
                         AND s.timestamp BETWEEN ? AND ?
                         AND s.trading_mode = ?
                       ORDER BY ABS(
                           (julianday(s.timestamp) - julianday(?)) * 86400
                       ) ASC
                       LIMIT 1""",
                    (acc_id, ts_lower, ts_upper, mode, ts),
                ).fetchone()

                if match:
                    resolved_signal_id = match[0]
                    snapshot_id = match[1]
            else:
                # Directly linked — find snapshot
                snap = conn.execute(
                    "SELECT id FROM feature_snapshots WHERE signal_id = ? LIMIT 1",
                    (signal_id,),
                ).fetchone()
                if snap:
                    snapshot_id = snap[0]

            # Determine outcome label
            if pnl > 0:
                outcome = "WIN"
            elif pnl < 0:
                outcome = "LOSS"
            else:
                outcome = "BREAKEVEN"

            # Calculate holding minutes
            holding_minutes = None
            if exit_time:
                try:
                    entry_dt = datetime.fromisoformat(ts)
                    exit_dt = datetime.fromisoformat(exit_time)
                    holding_minutes = int((exit_dt - entry_dt).total_seconds() / 60)
                except (ValueError, TypeError):
                    pass

            # Get features JSON from snapshot
            features_json = None
            if snapshot_id:
                snap_row = conn.execute(
                    "SELECT * FROM feature_snapshots WHERE id = ?", (snapshot_id,)
                ).fetchone()
                if snap_row:
                    columns = [desc[0] for desc in conn.execute(
                        "SELECT * FROM feature_snapshots LIMIT 0"
                    ).description]
                    snap_dict = dict(zip(columns, snap_row))
                    # Remove metadata keys (keep d1_trend, h4_trend, session for ML training)
                    for key in ["id", "signal_id", "timestamp", "timeframe",
                                "trading_mode", "strategy_id"]:
                        snap_dict.pop(key, None)
                    features_json = json.dumps(snap_dict)

                    # Validate critical ML features are present
                    if features_json:
                        critical_keys = {"d1_trend", "h4_trend"}
                        missing = critical_keys - set(snap_dict.keys())
                        if missing:
                            import logging
                            logging.getLogger(__name__).warning(
                                f"Backfill trade {t_id}: features_json missing critical keys {missing}. "
                                f"Available keys: {list(snap_dict.keys())[:10]}"
                            )

            # Build INSERT dynamically based on whether trading param columns exist
            if has_trading_params:
                conn.execute(
                    """INSERT INTO trade_outcomes
                       (trade_id, signal_id, snapshot_id, account_id, symbol, direction,
                        trading_mode, strategy_id, entry_price, exit_price, profit,
                        profit_pct, outcome_label, holding_minutes, exit_reason, features_json,
                        mfe, mae, mfe_pct, mae_pct,
                        exit_regime, exit_d1_trend, exit_h4_trend,
                        pnl, pnl_pct,
                        atr_multiplier, rr_ratio, min_confidence_threshold)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                               ?, ?, ?, ?, ?, ?, ?,
                               ?, ?,
                               ?, ?, ?)""",
                    (t_id, resolved_signal_id, snapshot_id, acc_id, symbol, direction,
                     mode, strategy, entry_price, exit_price, pnl, pnl_pct,
                     outcome, holding_minutes, exit_reason, features_json,
                     mfe, mae, mfe_pct, mae_pct,
                     exit_regime, exit_d1_trend, exit_h4_trend,
                     pnl, pnl_pct,
                     lt_atr, lt_rr, lt_conf),
                )
            else:
                conn.execute(
                    """INSERT INTO trade_outcomes
                       (trade_id, signal_id, snapshot_id, account_id, symbol, direction,
                        trading_mode, strategy_id, entry_price, exit_price, profit,
                        profit_pct, outcome_label, holding_minutes, exit_reason, features_json,
                        mfe, mae, mfe_pct, mae_pct,
                        exit_regime, exit_d1_trend, exit_h4_trend,
                        pnl, pnl_pct)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                               ?, ?, ?, ?, ?, ?, ?,
                               ?, ?)""",
                    (t_id, resolved_signal_id, snapshot_id, acc_id, symbol, direction,
                     mode, strategy, entry_price, exit_price, pnl, pnl_pct,
                     outcome, holding_minutes, exit_reason, features_json,
                     mfe, mae, mfe_pct, mae_pct,
                     exit_regime, exit_d1_trend, exit_h4_trend,
                     pnl, pnl_pct),
                )
            stats["linked"] += 1
            stats["outcomes"][outcome] += 1

        conn.commit()
    finally:
        conn.close()

    return stats



# Reserved account ID for synthetic/backtest trades (not a real trading account).
SYNTHETIC_ACCOUNT_ID = 0


def ensure_synthetic_account(db_path: Optional[Path] = None) -> None:
    """Create the synthetic account row if it does not exist (id=0).

    Required because trade_outcomes.account_id has a FK to accounts(id).
    """
    conn = get_connection(db_path)
    try:
        existing = conn.execute(
            "SELECT id FROM accounts WHERE id = ?", (SYNTHETIC_ACCOUNT_ID,)
        ).fetchone()
        if not existing:
            conn.execute(
                """INSERT INTO accounts
                   (id, name, broker_login, broker_server, balance, leverage,
                    bridge_host, bridge_port, signal_group, is_active)
                   VALUES (?, 'synthetic_backtest', '', 'Synthetic', 0, 100,
                           'localhost', 0, '', 0)""",
                (SYNTHETIC_ACCOUNT_ID,),
            )
            conn.commit()
    finally:
        conn.close()


def insert_synthetic_trade(
    direction: str,
    entry_price: float,
    exit_price: float,
    pnl: float,
    pnl_pct: float,
    d1_trend: str = "unknown",
    session: str = "unknown",
    regime: str = "unknown",
    strategy_id: str = "backtest_synth",
    entry_time: str = "",
    exit_time: str = "",
    exit_reason: str = "",
    symbol: str = "XAUUSD",
    trading_mode: str = "swing",
    stop_loss: float = 0.0,
    take_profit: float = 0.0,
    lot_size: float = 0.01,
    confidence: float = 0.0,
    h4_trend: str | None = None,
    atr_multiplier: float | None = None,
    rr_ratio: float | None = None,
    min_confidence_threshold: float | None = None,
    db_path: Optional[Path] = None,
) -> int:
    """Insert a minimal live_trades row for a synthetic backtest trade.

    Creates a closed trade with account_id=SYNTHETIC_ACCOUNT_ID (0).
    Returns the trade ID for linking to trade_outcomes.
    """
    ensure_synthetic_account(db_path)

    conn = get_connection(db_path)
    try:
        # Check if trading parameter columns exist (added via migration)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(live_trades)").fetchall()}
        has_atr_cols = "atr_multiplier" in cols

        if has_atr_cols:
            cursor = conn.execute(
                """INSERT INTO live_trades
                   (account_id, timestamp, direction, symbol, entry_price, stop_loss,
                    take_profit, lot_size, confidence, regime, session, d1_trend,
                    reason, trading_mode, strategy_id, is_open,
                    exit_price, exit_time, pnl, pnl_pct, exit_reason,
                    atr_multiplier, rr_ratio, min_confidence_threshold)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0,
                           ?, ?, ?, ?, ?,
                           ?, ?, ?)""",
                (SYNTHETIC_ACCOUNT_ID, entry_time, direction, symbol, entry_price,
                 stop_loss, take_profit, lot_size, confidence, regime, session,
                 d1_trend, f"backtest_{exit_reason}", trading_mode, strategy_id,
                 exit_price, exit_time, pnl, pnl_pct, exit_reason,
                 atr_multiplier, rr_ratio, min_confidence_threshold),
            )
        else:
            cursor = conn.execute(
                """INSERT INTO live_trades
                   (account_id, timestamp, direction, symbol, entry_price, stop_loss,
                    take_profit, lot_size, confidence, regime, session, d1_trend,
                    reason, trading_mode, strategy_id, is_open,
                    exit_price, exit_time, pnl, pnl_pct, exit_reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0,
                           ?, ?, ?, ?, ?)""",
                (SYNTHETIC_ACCOUNT_ID, entry_time, direction, symbol, entry_price,
                 stop_loss, take_profit, lot_size, confidence, regime, session,
                 d1_trend, f"backtest_{exit_reason}", trading_mode, strategy_id,
                 exit_price, exit_time, pnl, pnl_pct, exit_reason),
            )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def insert_synthetic_trade_outcome(
    trade_id: int,
    direction: str,
    entry_price: float,
    exit_price: float,
    profit: float,
    profit_pct: float,
    outcome_label: str,
    holding_minutes: int,
    exit_reason: str,
    features_json: str,
    regime: str = "unknown",
    d1_trend: str = "unknown",
    h4_trend: str = "unknown",
    session: str = "unknown",
    strategy_id: str = "backtest_synth",
    trading_mode: str = "swing",
    symbol: str = "XAUUSD",
    mfe: float | None = None,
    mae: float | None = None,
    mfe_pct: float | None = None,
    mae_pct: float | None = None,
    exit_regime: str | None = None,
    exit_d1_trend: str | None = None,
    exit_h4_trend: str | None = None,
    atr_multiplier: float | None = None,
    rr_ratio: float | None = None,
    min_confidence_threshold: float | None = None,
    db_path: Optional[Path] = None,
) -> int:
    """Insert a trade_outcomes row for a synthetic backtest trade.

    Links to a live_trades row created by insert_synthetic_trade().
    MFE/MAE and exit context are stored in features_json (not as separate columns)
    since the trade_outcomes table doesn't have dedicated columns for them.
    Returns the outcome ID.
    """
    # Enrich features_json with MFE/MAE and exit context before insertion
    import json
    features = json.loads(features_json) if isinstance(features_json, str) else features_json.copy() if features_json else {}
    if mfe is not None:
        features["mfe"] = mfe
    if mae is not None:
        features["mae"] = mae
    if mfe_pct is not None:
        features["mfe_pct"] = mfe_pct
    if mae_pct is not None:
        features["mae_pct"] = mae_pct
    if exit_regime is not None:
        features["exit_regime"] = exit_regime
    if exit_d1_trend is not None:
        features["exit_d1_trend"] = exit_d1_trend
    if exit_h4_trend is not None:
        features["exit_h4_trend"] = exit_h4_trend
    if atr_multiplier is not None:
        features["atr_multiplier"] = atr_multiplier
    if rr_ratio is not None:
        features["rr_ratio"] = rr_ratio
    if min_confidence_threshold is not None:
        features["min_confidence_threshold"] = min_confidence_threshold
    enriched_json = json.dumps(features)

    conn = get_connection(db_path)
    try:
        # Check if atr_multiplier column exists (migration may not have run yet)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(trade_outcomes)").fetchall()}
        has_atr_cols = "atr_multiplier" in cols

        if has_atr_cols:
            cursor = conn.execute(
                """INSERT INTO trade_outcomes
                   (trade_id, signal_id, snapshot_id, account_id, symbol, direction,
                    trading_mode, strategy_id, entry_price, exit_price, profit,
                    profit_pct, outcome_label, holding_minutes, exit_reason, features_json,
                    pnl, pnl_pct, atr_multiplier, rr_ratio, min_confidence_threshold)
                   VALUES (?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           ?, ?, ?, ?, ?)""",
                (trade_id, SYNTHETIC_ACCOUNT_ID, symbol, direction,
                 trading_mode, strategy_id, entry_price, exit_price, profit,
                 profit_pct, outcome_label, holding_minutes, exit_reason, enriched_json,
                 profit, profit_pct, atr_multiplier, rr_ratio, min_confidence_threshold),
            )
        else:
            cursor = conn.execute(
                """INSERT INTO trade_outcomes
                   (trade_id, signal_id, snapshot_id, account_id, symbol, direction,
                    trading_mode, strategy_id, entry_price, exit_price, profit,
                    profit_pct, outcome_label, holding_minutes, exit_reason, features_json,
                    pnl, pnl_pct)
                   VALUES (?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           ?, ?)""",
                (trade_id, SYNTHETIC_ACCOUNT_ID, symbol, direction,
                 trading_mode, strategy_id, entry_price, exit_price, profit,
                 profit_pct, outcome_label, holding_minutes, exit_reason, enriched_json,
                 profit, profit_pct),
            )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def query_trade_outcomes_for_training(
    min_confidence: float = 0.0,
    exclude_breakeven: bool = True,
    db_path: Optional[Path] = None,
) -> list[dict]:
    """Query trade_outcomes for ML training — returns rows with features_json parsed.

    Each row has: trade_id, direction, trading_mode, outcome_label, profit,
    profit_pct, features (dict), and all feature columns from features_json.
    """
    import json

    conn = get_connection(db_path)
    try:
        query = """
            SELECT * FROM trade_outcomes
            WHERE features_json IS NOT NULL
        """
        params: list = []

        if exclude_breakeven:
            query += " AND outcome_label != 'BREAKEVEN'"

        if min_confidence > 0:
            query += " AND profit_pct >= ?"
            params.append(min_confidence)

        query += " ORDER BY created_at ASC"

        cursor = conn.execute(query, params)
        columns = [desc[0] for desc in cursor.description]
        rows = []
        for row in cursor.fetchall():
            d = dict(zip(columns, row))
            features_str = d.pop("features_json", "{}")
            d["features"] = json.loads(features_str) if features_str else {}
            rows.append(d)
        return rows
    finally:
        conn.close()