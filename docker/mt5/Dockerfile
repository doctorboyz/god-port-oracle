# MT5 Bridge Container — extends gmag11/MetaTrader5-Docker
#
# gmag11 provides: Debian + Wine + VNC + MT5 + Python 3.9 + mt5linux
# We add: custom RPyC bridge server + numpy downgrade + auto-start
#
# Architecture:
#   macOS (oracle-engine)
#       ↕ RPyC over network (port 5005/5006/5007 → 8001 inside container)
#   Container (gmag11 + our bridge)
#       ↕ Wine Python
#   MetaTrader5 → Exness broker
#
# Usage:
#   docker compose -f docker-compose.vps.yml up -d
#   Then login to MT5 via VNC (first time only)
#   Bridge auto-starts on container restart

FROM ghcr.io/gmag11/metatrader5-docker:2.3

# Downgrade numpy — numpy 2.x is incompatible with MetaTrader5
# Must run under Wine since MT5 Python is a Windows Python
RUN wine python -m pip install 'numpy<2' --force-reinstall || true

# Install rpyc for bridge server (Linux Python)
RUN pip3 install --no-cache-dir 'rpyc>=5.2.0' || true

# Copy our custom RPyC bridge server (runs under Wine Python)
COPY mt5_bridge_server.py /app/mt5_bridge_server.py

# Preserve gmag11's original start script
RUN cp /Metatrader/start.sh /original_start.sh

# Copy our entrypoint
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=10s --start-period=180s --retries=5 \
    CMD python3 -c "import socket; s=socket.socket(); s.settimeout(2); s.connect(('localhost', 8001)); s.close()" || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]