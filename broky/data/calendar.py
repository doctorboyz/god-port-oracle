"""Economic calendar — know when NOT to trade.

Uses Finnhub economic calendar API (free tier).
Provides high-impact event awareness for gold trading.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Impact levels
IMPACT_HIGH = "High"
IMPACT_MEDIUM = "Medium"
IMPACT_LOW = "Low"

# Currencies that affect gold
GOLD_CURRENCIES = {"USD", "XAU"}


@dataclass
class CalendarEvent:
    """Economic calendar event."""
    datetime: datetime
    currency: str
    impact: str  # High, Medium, Low
    event: str
    actual: str = ""
    forecast: str = ""
    previous: str = ""


def _parse_finnhub_time(time_val) -> datetime:
    """Parse Finnhub time field — handles both string and int timestamps."""
    if isinstance(time_val, str):
        try:
            return datetime.strptime(time_val, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            try:
                return datetime.strptime(time_val, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            except ValueError:
                return datetime.now(timezone.utc)
    if isinstance(time_val, (int, float)) and time_val > 0:
        return datetime.fromtimestamp(time_val, tz=timezone.utc)
    return datetime.now(timezone.utc)


def fetch_calendar_finnhub(
    days_ahead: int = 7,
    api_key: Optional[str] = None,
) -> list[CalendarEvent]:
    """Fetch economic calendar from Finnhub.

    Args:
        days_ahead: Number of days ahead to fetch.
        api_key: Finnhub API key (uses FINNHUB_API_KEY env var if not set).

    Returns:
        List of CalendarEvent objects.
    """
    try:
        import finnhub
    except ImportError:
        logger.warning("finnhub-python not installed. Run: pip install finnhub-python")
        return []

    key = api_key or os.environ.get("FINNHUB_API_KEY", "")
    if not key:
        logger.warning("FINNHUB_API_KEY not set. Register at https://finnhub.io/register")
        return []

    client = finnhub.Client(api_key=key)

    try:
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        end = (now + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

        cal = client.calendar_economic(_from=today, to=end)

        events = []
        for item in cal.get("economicCalendar", []):
            impact_map = {"high": IMPACT_HIGH, "medium": IMPACT_MEDIUM, "low": IMPACT_LOW}
            impact = impact_map.get(item.get("impact", "").lower(), IMPACT_LOW)

            event_dt = _parse_finnhub_time(item.get("time", ""))

            def _str(val) -> str:
                return str(val) if val is not None else ""

            events.append(CalendarEvent(
                datetime=event_dt,
                currency=item.get("country", "US"),
                impact=impact,
                event=item.get("event", ""),
                actual=_str(item.get("actual")),
                forecast=_str(item.get("estimate")),
                previous=_str(item.get("prev")),
            ))

        logger.info("Fetched %d calendar events from Finnhub", len(events))
        return events

    except Exception as e:
        logger.debug("Finnhub calendar unavailable: %s", e)
        return []


# Finnhub uses country codes (US, CA, EU) but we want currency codes (USD, CAD, EUR)
COUNTRY_TO_CURRENCY = {
    "US": "USD", "CA": "CAD", "EU": "EUR", "GB": "GBP", "JP": "JPY",
    "AU": "AUD", "NZ": "NZD", "CH": "CHF", "CN": "CNY", "IN": "INR",
    "SG": "SGD", "HK": "HKD", "NO": "NOK", "SE": "SEK", "MX": "MXN",
    "ZA": "ZAR", "TR": "TRY", "BR": "BRL", "RU": "RUB", "KR": "KRW",
}


def fetch_calendar(
    days_ahead: int = 7,
    filter_currencies: Optional[set] = None,
    api_key: Optional[str] = None,
) -> list[CalendarEvent]:
    """Fetch economic calendar events.

    Tries Finnhub first, returns empty list if unavailable.
    Filter by currencies (e.g., {'USD'} for gold-relevant events).

    Args:
        days_ahead: Number of days ahead to fetch.
        filter_currencies: Only return events for these currencies.
        api_key: Optional Finnhub API key.

    Returns:
        List of CalendarEvent objects sorted by datetime.
    """
    events = fetch_calendar_finnhub(days_ahead=days_ahead, api_key=api_key)

    # Map country codes to currency codes
    for event in events:
        if event.currency in COUNTRY_TO_CURRENCY:
            event.currency = COUNTRY_TO_CURRENCY[event.currency]

    if filter_currencies:
        events = [e for e in events if e.currency in filter_currencies]

    events.sort(key=lambda e: e.datetime)
    return events


def _parse_date_cell(text: str) -> Optional[datetime]:
    """Parse date cell text into datetime."""
    text = text.strip().lower()
    if "today" in text:
        return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0)
    if "tomorrow" in text:
        return (datetime.now(timezone.utc) + timedelta(days=1)).replace(hour=0, minute=0, second=0)
    if "yesterday" in text:
        return (datetime.now(timezone.utc) - timedelta(days=1)).replace(hour=0, minute=0, second=0)
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0)


def _build_datetime(date: Optional[datetime], time_text: str) -> datetime:
    """Build datetime from date and time text."""
    if date is None:
        return datetime.now(timezone.utc)

    time_text = time_text.strip().lower()
    if not time_text or time_text in ("all day", "tentative", "day"):
        return date

    try:
        parts = time_text.replace("am", "").replace("pm", "").strip().split(":")
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0

        if "pm" in time_text and hour < 12:
            hour += 12
        elif "am" in time_text and hour == 12:
            hour = 0

        return date.replace(hour=hour, minute=minute)
    except (ValueError, IndexError):
        return date


def is_high_impact_soon(
    events: list[CalendarEvent],
    minutes_before: int = 30,
    minutes_after: int = 15,
    currencies: Optional[set] = None,
) -> list[CalendarEvent]:
    """Check if any high-impact events are happening soon.

    Args:
        events: List of calendar events.
        minutes_before: Minutes before event to consider "soon".
        minutes_after: Minutes after event to consider "active".
        currencies: Only check these currencies. None = check all.

    Returns:
        List of high-impact events happening within the window.
    """
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(minutes=minutes_after)
    window_end = now + timedelta(minutes=minutes_before)

    results = []
    for event in events:
        if event.impact != IMPACT_HIGH:
            continue
        if currencies and event.currency not in currencies:
            continue
        if window_start <= event.datetime <= window_end:
            results.append(event)

    return results


def should_avoid_trading(
    events: list[CalendarEvent],
    currencies: set = GOLD_CURRENCIES,
    minutes_before: int = 30,
) -> bool:
    """Check if we should avoid trading due to upcoming high-impact news.

    Returns True if there's a high-impact event for gold-related currencies
    happening within minutes_before from now.
    """
    soon_events = is_high_impact_soon(events, minutes_before=minutes_before, currencies=currencies)
    return len(soon_events) > 0