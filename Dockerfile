FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .

# Install runtime dependencies (pandas-ta excluded — not needed at runtime;
# broky indicators are computed with custom implementations)
RUN pip install --no-cache-dir \
    pandas>=2.2.0 numpy>=1.26.0 \
    aiosqlite>=0.20.0 pydantic>=2.7.0 pyyaml>=6.0 \
    click>=8.1.0 "rpyc>=5.2.0,<6.0.0" matplotlib>=3.8.0 tabulate>=0.9.0 \
    python-dotenv>=1.0.0 finnhub-python>=2.4.0 requests>=2.31.0 \
    scikit-learn>=1.3.0

COPY . .

# Install project packages so broky/, metty/, shared/ are importable
RUN pip install --no-cache-dir --no-deps . || true

# Create data directory
RUN mkdir -p /app/data

ENV TRADING_PHASE=both
ENV DB_PATH=/app/data/oracle.db
ENV DRY_RUN=1
ENV ACCOUNTS=A,B,C
ENV COLLECT_INTERVAL=300
ENV TRADE_INTERVAL=300
ENV SCALP_ENABLED=0
ENV SCALP_INTERVAL=60
ENV SCALP_SPREAD_MAX=30
ENV SCALP_RISK_PER_TRADE=0.01
ENV TG_BOT_TOKEN=
ENV TG_CHAT_ID=

VOLUME ["/app/data"]

# Health check: verify the runner can import modules
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python3 -c "from metty.execution.live_collector import LiveCollector; from metty.execution.live_trader import LiveTrader; print('OK')" || exit 1

CMD ["python3", "scripts/oracle_runner.py"]