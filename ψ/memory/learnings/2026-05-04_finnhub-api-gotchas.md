---
name: finnhub-api-gotchas
description: Finnhub API response format gotchas — string timestamps, country codes, free tier limits
type: project
---

# Finnhub API Gotchas

## Timestamp Format
Finnhub `calendar_economic()` returns `time` as **string** (`"2026-05-04 00:00:00"`), NOT unix timestamp int. Code must handle both formats. Use `_parse_finnhub_time()` helper.

**Why**: API changed format without documentation update. Old code assumed int and crashed with `'str' object cannot be interpreted as an integer`.

**How to apply**: Always test API integrations against live service. Parse defensively.

## Country vs Currency
Finnhub uses **ISO country codes** (US, CA, EU, GB) not **currency codes** (USD, CAD, EUR, GBP). Must map explicitly:

```python
COUNTRY_TO_CURRENCY = {"US": "USD", "CA": "CAD", "EU": "EUR", ...}
```

**Why**: Filtering `{'USD'}` against `country="US"` returned 0 events.

**How to apply**: When integrating external APIs, verify field semantics with actual data.

## Free Tier Limits
- `social_sentiment()` is NOT available on free tier — `Client` object has no attribute `social_sentiment`
- `general_news("forex")` returns limited articles (~1-5 on free tier)
- `calendar_economic()` works on free tier (349+ events)
- Rate limit: 60 calls/min

**How to apply**: Use `hasattr()` checks for premium endpoints. Cache aggressively.

## Other Finnhub Notes
- Use `_from` (not `start`) parameter for calendar queries
- `estimate` field (not `forecast`) for forecast values
- `prev` field for previous values