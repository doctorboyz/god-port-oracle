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
    ticket INTEGER,
    exit_price REAL,
    exit_time TEXT,
    pnl REAL,
    pnl_pct REAL,
    exit_reason TEXT,
    is_open INTEGER NOT NULL DEFAULT 1,
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
"""


def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Get a connection to the SQLite database with WAL mode."""
    path = db_path or DB_PATH
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
    finally:
        conn.close()


def _migrate_trading_columns(conn: sqlite3.Connection) -> None:
    """Add trading_mode and strategy_id columns to existing tables."""
    migrations = [
        ("live_trades", "trading_mode", "TEXT NOT NULL DEFAULT 'swing'"),
        ("live_trades", "strategy_id", "TEXT NOT NULL DEFAULT ''"),
        ("signals", "trading_mode", "TEXT NOT NULL DEFAULT 'swing'"),
        ("signals", "strategy_id", "TEXT NOT NULL DEFAULT ''"),
        ("feature_snapshots", "trading_mode", "TEXT NOT NULL DEFAULT 'swing'"),
        ("feature_snapshots", "strategy_id", "TEXT NOT NULL DEFAULT ''"),
        ("candles", "trading_mode", "TEXT NOT NULL DEFAULT 'swing'"),
        ("candles", "strategy_id", "TEXT NOT NULL DEFAULT ''"),
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
    trading_mode: str = "swing",
    strategy_id: str = "",
    db_path: Optional[Path] = None,
    **indicator_values: float | str | int | None,
) -> int:
    """Insert a feature snapshot with all indicator values."""
    conn = get_connection(db_path)
    try:
        columns = ["signal_id", "timestamp", "price", "timeframe", "session", "d1_trend", "trading_mode", "strategy_id"]
        values: list[float | str | int | None] = [signal_id, timestamp, price, timeframe, session, d1_trend, trading_mode, strategy_id]

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
    db_path: Optional[Path] = None,
) -> int:
    """Insert a live trade record and return its ID."""
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            """INSERT INTO live_trades
               (account_id, timestamp, direction, symbol, entry_price, stop_loss,
                take_profit, lot_size, confidence, regime, session, d1_trend,
                reason, trading_mode, strategy_id, ticket, is_open)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
            (account_id, timestamp, direction, symbol, entry_price, stop_loss,
             take_profit, lot_size, confidence, regime, session, d1_trend,
             reason, trading_mode, strategy_id, ticket),
        )
        conn.commit()
        return cursor.lastrowid
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
    db_path: Optional[Path] = None,
) -> None:
    """Close a live trade by marking it as closed with exit details."""
    conn = get_connection(db_path)
    try:
        conn.execute(
            """UPDATE live_trades SET
               exit_price = ?, exit_time = ?, pnl = ?, pnl_pct = ?,
               exit_reason = ?, is_open = 0
               WHERE id = ?""",
            (exit_price, exit_time, pnl, pnl_pct, exit_reason, trade_id),
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