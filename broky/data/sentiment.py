"""Fear & Greed Index + sentiment data for gold trading context."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import requests

logger = logging.getLogger(__name__)

CNN_FEAR_GREED_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"


def fetch_fear_greed_index() -> dict:
    """Fetch CNN Fear & Greed Index.

    Returns:
        Dict with: value (0-100), label, timestamp.
        0 = Extreme Fear, 100 = Extreme Greed.
        0-25 = Extreme Fear
        25-45 = Fear
        45-55 = Neutral
        55-75 = Greed
        75-100 = Extreme Greed
    """
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }
        response = requests.get(CNN_FEAR_GREED_URL, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()

        # Parse the response
        fg_data = data.get("fear_and_greed", {})
        value = fg_data.get("score", 50)
        label = fg_data.get("rating", "Neutral")
        timestamp_str = fg_data.get("timestamp", "")

        # Also get category-specific scores
        categories = {}
        for key in data:
            if key != "fear_and_greed" and isinstance(data[key], dict):
                score = data[key].get("score")
                if score is not None:
                    categories[key] = score

        result = {
            "value": float(value),
            "label": label,
            "timestamp": timestamp_str or datetime.utcnow().isoformat(),
            "categories": categories,
            "source": "cnn",
        }

        logger.info("Fear & Greed Index: %.0f (%s)", result["value"], result["label"])
        return result

    except Exception as e:
        logger.warning("Fear & Greed fetch failed: %s", e)
        return {
            "value": 50.0,
            "label": "Neutral (unavailable)",
            "timestamp": datetime.utcnow().isoformat(),
            "categories": {},
            "source": "fallback",
        }


def fear_greed_to_gold_signal(fg_value: float) -> dict:
    """Convert Fear & Greed value to gold trading context.

    Gold typically:
    - Rises during Fear (safe haven demand)
    - Falls during Greed (risk-on, less demand for gold)

    Args:
        fg_value: Fear & Greed index value (0-100).

    Returns:
        Dict with gold_bias and signal strength.
    """
    if fg_value <= 25:
        # Extreme Fear → gold bullish (safe haven)
        return {"gold_bias": "bullish", "strength": 0.8, "context": "extreme_fear_safe_haven"}
    elif fg_value <= 45:
        # Fear → gold slightly bullish
        return {"gold_bias": "slightly_bullish", "strength": 0.5, "context": "fear_safe_haven"}
    elif fg_value <= 55:
        # Neutral → no signal
        return {"gold_bias": "neutral", "strength": 0.0, "context": "neutral"}
    elif fg_value <= 75:
        # Greed → gold slightly bearish (risk-on)
        return {"gold_bias": "slightly_bearish", "strength": 0.4, "context": "greed_risk_on"}
    else:
        # Extreme Greed → gold bearish
        return {"gold_bias": "bearish", "strength": 0.7, "context": "extreme_greed_risk_on"}


def get_sentiment_snapshot(
    fg_index: Optional[dict] = None,
    news_items: Optional[list] = None,
) -> dict:
    """Compile a complete sentiment snapshot for ML features.

    Combines Fear & Greed, news sentiment, and other signals.

    Returns:
        Dict suitable for feature snapshot storage.
    """
    if fg_index is None:
        fg_index = fetch_fear_greed_index()

    fg_value = fg_index.get("value", 50.0)
    gold_signal = fear_greed_to_gold_signal(fg_value)

    result = {
        "fear_greed_value": fg_value,
        "fear_greed_label": fg_index.get("label", "Neutral"),
        "gold_bias": gold_signal["gold_bias"],
        "gold_bias_strength": gold_signal["strength"],
        "sentiment_context": gold_signal["context"],
        "source": fg_index.get("source", "unknown"),
        "timestamp": fg_index.get("timestamp", datetime.utcnow().isoformat()),
    }

    # Add category scores if available
    categories = fg_index.get("categories", {})
    if "market_volatility_vix" in categories:
        result["vix_score"] = categories["market_volatility_vix"]
    if "junk_bond_demand" in categories:
        result["junk_bond_score"] = categories["junk_bond_demand"]
    if "safe_haven_demand" in categories:
        result["safe_haven_score"] = categories["safe_haven_demand"]

    # News sentiment (if provided)
    if news_items:
        from broky.data.news import news_to_sentiment_score
        result["news_sentiment"] = news_to_sentiment_score(news_items)
    else:
        result["news_sentiment"] = 0.0

    return result