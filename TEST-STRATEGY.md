# God Port Trading — Test Strategy

## Coverage Target: 80%+

Current: 283 tests passing, coverage TBD (no coverage tool wired yet)

## Test Categories

### Unit Tests (`tests/`)
- **test_indicators.py** — TA-Lib wrappers, ATR, ADX, MACD, Bollinger
- **test_models.py** — data model serialization/deserialization
- **test_events.py** — event bus publish/subscribe
- **test_signal_generator.py** — signal scoring, confidence calculation
- **test_risk.py** — circuit breaker, position sizing
- **test_ml_predictor.py** — TradeOutcomePredictor, feature computation, risk multiplier

### Integration Tests (`tests/`)
- **test_data_pipeline.py** — CSV loading, resampling, validation
- **test_backtest.py** — backtest engine with mock data
- **test_registry.py** — strategy decorator registration
- **test_llm_analyzer.py** — LLM strategy analysis (mock)
- **test_m5_scalp_integration.py** — M5 scalp full cycle (mock bridge)
- **test_scaling.py** — position sizing scaling, edge cases

### E2E / Live Tests
- **VPS sandbox**: Dry-run trades on demo accounts before live
- **Forward test**: 4 weeks profitable paper trading before live money
- **Smoke test**: Deploy → check container health → check first cycle succeeds

## What's NOT Tested (gaps)

| Gap | Risk | Plan |
|-----|------|------|
| MT5 bridge RPyC (real) | Mocked in tests — real bridge failures not caught | Monitor VPS logs |
| Order execution (real fills) | Slippage, rejection, partial fills not simulated | Paper trade phase |
| Fear & Greed API | External API — no mock | Low impact — sentiment is supplementary |
| Finnhub news API | External API — no mock | Low impact — calendar is advisory |
| Docker deployment | Not tested in CI | Manual deploy + health check |
| Multi-account race conditions | 3 parallel traders not integration-tested together | Monitor for phantom trades |

## Test Conventions

- **AAA pattern**: Arrange → Act → Assert
- **Mock external dependencies**: bridge, APIs, MT5
- **Test file naming**: `test_<module>.py`
- **Use `@pytest.mark.unit` / `@pytest.mark.integration`** for categorization (planned)

## Running Tests

```bash
# All tests
python -m pytest tests/ -v

# Unit only
python -m pytest tests/ -v -m unit

# Integration only
python -m pytest tests/ -v -m integration

# With coverage (planned)
python -m pytest tests/ --cov=broky --cov=metty --cov-report=term-missing
```

## Pre-Commit Checklist
- [ ] All 283 tests pass
- [ ] New code has tests (≥80% of new lines)
- [ ] Bridge mock still valid after MT5 API changes
- [ ] No hardcoded credentials in test fixtures
