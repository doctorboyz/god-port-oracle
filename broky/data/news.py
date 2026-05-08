"""Finnhub news and sentiment data — market news, company sentiment."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# Finnhub free tier: 60 calls/min
# Get API key from https://finnhub.io/register


def get_finnhub_client():
    """Get Finnhub client with API key from environment."""
    try:
        import finnhub
    except ImportError:
        raise ImportError("finnhub-python not installed. Run: pip install finnhub-python")

    api_key = os.environ.get("FINNHUB_API_KEY", "")
    if not api_key:
        logger.warning("FINNHUB_API_KEY not set. Using free tier with rate limits.")
    return finnhub.Client(api_key=api_key)


def fetch_market_news(
    category: str = "forex",
    hours_back: int = 24,
    api_key: Optional[str] = None,
) -> list[dict]:
    """Fetch market news from Finnhub.

    Args:
        category: News category (general, forex, crypto, merger).
        hours_back: How many hours back to fetch.
        api_key: Optional API key (uses FINNHUB_API_KEY env var if not set).

    Returns:
        List of news dicts with: headline, summary, source, url, datetime, related.
    """
    try:
        import finnhub
    except ImportError:
        raise ImportError("finnhub-python not installed. Run: pip install finnhub-python")

    key = api_key or os.environ.get("FINNHUB_API_KEY", "")
    if not key:
        logger.warning("No Finnhub API key — register at https://finnhub.io/register")
        return []

    client = finnhub.Client(api_key=key)

    # Finnhub market news endpoint
    try:
        news = client.general_news(category)
    except Exception as e:
        logger.error("Finnhub news fetch failed: %s", e)
        return []

    # Filter by time
    cutoff = datetime.utcnow() - timedelta(hours=hours_back)
    cutoff_ts = int(cutoff.timestamp())

    results = []
    for item in news:
        ts = item.get("datetime", 0)
        if ts < cutoff_ts:
            continue
        results.append({
            "headline": item.get("headline", ""),
            "summary": item.get("summary", ""),
            "source": item.get("source", ""),
            "url": item.get("url", ""),
            "datetime": datetime.utcfromtimestamp(ts).isoformat(),
            "related": item.get("related", ""),
        })

    logger.info("Fetched %d news items from Finnhub (category=%s)", len(results), category)
    return results


def fetch_gold_sentiment(api_key: Optional[str] = None) -> dict:
    """Fetch sentiment indicators for gold/XAUUSD.

    Uses Finnhub's sentiment data (available on free tier).

    Returns:
        Dict with sentiment scores: bearish_percent, bullish_percent.
    """
    try:
        import finnhub
    except ImportError:
        raise ImportError("finnhub-python not installed. Run: pip install finnhub-python")

    key = api_key or os.environ.get("FINNHUB_API_KEY", "")
    if not key:
        return {"bearish_percent": 0.0, "bullish_percent": 0.0, "source": "no_api_key"}

    client = finnhub.Client(api_key=key)

    try:
        # XAUUSD social sentiment (premium tier — may not be available)
        if hasattr(client, "social_sentiment"):
            sentiment = client.social_sentiment("XAUUSD")
            if sentiment and "reddit" in sentiment:
                reddit = sentiment["reddit"]
                return {
                    "bearish_percent": reddit.get("bearishPercent", 0.0),
                    "bullish_percent": reddit.get("bullishPercent", 0.0),
                    "source": "finnhub_reddit",
                }
    except Exception as e:
        logger.warning("Finnhub sentiment not available (free tier): %s", e)

    return {"bearish_percent": 0.0, "bullish_percent": 0.0, "source": "unavailable"}


def news_to_sentiment_score(news_items: list[dict]) -> float:
    """Convert news headlines to a simple sentiment score (-1 to +1).

    Uses keyword matching for gold-related sentiment.
    This is a basic NLP approach — for production, consider a proper NLP model.
    """
    if not news_items:
        return 0.0

    bullish_words = {"rise", "rally", "gain", "bullish", "higher", "support", "buy",
                     "strong", "recovery", "upbeat", "optimistic", "surge", "climb"}
    bearish_words = {"fall", "drop", "bearish", "lower", "resistance", "sell", "weak",
                     "decline", "slump", "pessimistic", "tumble", "dive", "retreat"}

    scores = []
    for item in news_items:
        headline = item.get("headline", "").lower()
        words = set(headline.split())
        bull = len(words & bullish_words)
        bear = len(words & bearish_words)
        if bull + bear > 0:
            scores.append((bull - bear) / (bull + bear))

    return sum(scores) / len(scores) if scores else 0.0