"""YFinance data loader — backup XAUUSD data source from Yahoo Finance."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

XAUUSD_TICKER = "GC=F"  # Gold futures (most reliable on yfinance)
# Alternative tickers: XAUUSD=X (spot, may not work), GC=F (futures)


def fetch_xauusd(
    period: str = "5d",
    interval: str = "5m",
    ticker: str = XAUUSD_TICKER,
) -> pd.DataFrame:
    """Fetch XAUUSD OHLCV data from Yahoo Finance.

    Args:
        period: Lookback period (1d, 5d, 1mo, 3mo, 6mo, 1y, 2y, 5y, max).
        interval: Bar interval (1m, 2m, 5m, 15m, 30m, 60m, 90m, 1h, 1d, 5d, 1wk, 1mo).
            Note: 1m-30m only available for last 60 days.
        ticker: Yahoo Finance ticker symbol.

    Returns:
        DataFrame with columns: open, high, low, close, volume (lowercase, matching loader.py convention). DatetimeIndex.
    """
    try:
        import yfinance as yf
    except ImportError:
        raise ImportError("yfinance not installed. Run: pip install yfinance")

    df = yf.download(ticker, period=period, interval=interval, progress=False)

    if df.empty:
        logger.warning("No data returned from yfinance for %s", ticker)
        return pd.DataFrame()

    # Flatten multi-level columns if present
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    # Keep only OHLCV and normalize to lowercase (pipeline convention)
    keep = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]
    df = df[keep].copy()
    df.columns = [c.lower() for c in df.columns]

    # Remove timezone from index for compatibility
    if df.index.tz is not None:
        df.index = df.index.tz_convert("UTC").tz_localize(None)

    df = df.dropna()
    logger.info("Fetched %d bars from yfinance (%s, %s)", len(df), period, interval)
    return df


def fetch_xauusd_range(
    start: str | datetime,
    end: str | datetime,
    interval: str = "5m",
    ticker: str = XAUUSD_TICKER,
) -> pd.DataFrame:
    """Fetch XAUUSD data for a specific date range.

    Args:
        start: Start date (YYYY-MM-DD or datetime).
        end: End date (YYYY-MM-DD or datetime).
        interval: Bar interval.
        ticker: Yahoo Finance ticker.

    Returns:
        DataFrame with columns: open, high, low, close, volume (lowercase, matching loader.py convention). DatetimeIndex.
    """
    try:
        import yfinance as yf
    except ImportError:
        raise ImportError("yfinance not installed. Run: pip install yfinance")

    df = yf.download(ticker, start=start, end=end, interval=interval, progress=False)

    if df.empty:
        logger.warning("No data returned from yfinance for %s to %s", start, end)
        return pd.DataFrame()

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    keep = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]
    df = df[keep].copy()
    df.columns = [c.lower() for c in df.columns]

    if df.index.tz is not None:
        df.index = df.index.tz_convert("UTC").tz_localize(None)

    df = df.dropna()
    logger.info("Fetched %d bars from yfinance (%s to %s, %s)", len(df), start, end, interval)
    return df