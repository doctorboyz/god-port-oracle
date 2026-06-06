"""Load XAUUSD Premium Data CSVs.

Premium CSV format: no header, columns are date,open,high,low,close,volume.
Date format varies but typically YYYY.MM.DD or similar.

All column names are standardized to lowercase for consistency across
the pipeline (live trading, backtesting, ML feature computation).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# Standardized lowercase column names — used throughout the pipeline
COLUMN_NAMES = ["date", "open", "high", "low", "close", "volume"]

TIMEFRAME_MAP = {
    "M1": "1min",
    "M5": "5min",
    "M15": "15min",
    "M30": "30min",
    "H1": "1h",
    "H4": "4h",
    "D1": "1D",
}


def load_csv(
    filepath: str | Path,
    date_format: Optional[str] = None,
) -> pd.DataFrame:
    """Load a single XAUUSD Premium Data CSV file.

    Args:
        filepath: Path to the CSV file.
        date_format: Optional strptime format string for the date column.
            If None, pandas will try to infer.

    Returns:
        DataFrame with lowercase columns: open, high, low, close, volume.
        Indexed by datetime (date column).

    Example:
        >>> df = load_csv("data/xau-data/XAUUSD_M5.csv")
        >>> df.head()
    """
    filepath = Path(filepath)
    if not filepath.exists():
        raise FileNotFoundError(f"Data file not found: {filepath}")

    df = pd.read_csv(
        filepath,
        header=None,
        names=COLUMN_NAMES,
        parse_dates=["date"],
        date_format=date_format,
    )
    df = df.set_index("date").sort_index()
    df = df.dropna()

    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)

    df = df.dropna(subset=["open", "high", "low", "close"])

    logger.info("Loaded %d rows from %s", len(df), filepath.name)
    return df


def load_timeframe(
    data_dir: str | Path,
    timeframe: str = "M5",
    date_format: Optional[str] = None,
) -> pd.DataFrame:
    """Load data for a specific timeframe from the data directory.

    Looks for files matching patterns like XAUUSD_M5.csv or M5.csv in the directory.

    Args:
        data_dir: Directory containing CSV files.
        timeframe: Timeframe string (M1, M5, M15, M30, H1, H4, D1).
        date_format: Optional date format string.

    Returns:
        DataFrame with OHLCV data indexed by datetime.

    Example:
        >>> df = load_timeframe("data/xau-data", timeframe="M5")
    """
    data_dir = Path(data_dir)
    if not data_dir.exists():
        raise FileNotFoundError(f"Data directory not found: {data_dir}")

    # Try various filename patterns
    patterns = [
        f"XAUUSD_{timeframe}*.csv",
        f"*_{timeframe}*.csv",
        f"{timeframe}.csv",
    ]

    for pattern in patterns:
        matches = list(data_dir.glob(pattern))
        if matches:
            return load_csv(matches[0], date_format=date_format)

    raise FileNotFoundError(
        f"No CSV file found for timeframe {timeframe} in {data_dir}. "
        f"Tried patterns: {patterns}"
    )