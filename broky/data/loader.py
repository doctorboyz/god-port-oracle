"""Load XAUUSD Premium Data CSVs.

Premium CSV format: no header, columns are Date,Open,High,Low,Close,Volume.
Date format varies but typically YYYY.MM.DD or similar.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

COLUMN_NAMES = ["Date", "Open", "High", "Low", "Close", "Volume"]

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
        DataFrame with columns: Date, Open, High, Low, Close, Volume.
        Date is parsed as datetime, set as index.

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
        parse_dates=["Date"],
        date_format=date_format,
    )
    df = df.set_index("Date").sort_index()
    df = df.dropna()

    for col in ["Open", "High", "Low", "Close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["Volume"] = pd.to_numeric(df["Volume"], errors="coerce").fillna(0)

    df = df.dropna(subset=["Open", "High", "Low", "Close"])

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