#!/bin/bash
set -e

# MT5 Bridge Entrypoint — 3-phase startup
#
# Phase 1: gmag11 initialization (installs Wine, MT5, Python — takes ~2-3 min)
# Phase 2: Start MT5 terminal (user must login manually via VNC the first time)
# Phase 3: Start custom RPyC bridge server in Wine Python
#
# The bridge server runs as Wine Python (not Linux Python) because
# MetaTrader5 package only works under Wine where it can talk to MT5 terminal.

export DISPLAY=:99
export WINEPREFIX=/config/.wine

echo "=== MT5 Bridge Container Starting ==="

# Phase 1: Run gmag11's start.sh to initialize everything
# This handles: Xvfb, Wine setup, MT5 install, Python install, pip packages
echo "[Phase 1] Running gmag11 initialization..."
/original_start.sh &

# Wait for gmag11 initialization to complete (~2-3 minutes)
# gmag11 creates Wine prefix, installs MT5 terminal, sets up Python
echo "[Phase 1] Waiting for initialization (120s)..."
sleep 120

# Phase 2: Start MT5 terminal
# After gmag11 init, MT5 terminal should be installed at the expected path.
# Login credentials come from MT5_CMD_OPTIONS env var (set by docker-compose).
# NOTE: First-time login must be done manually via VNC — MT5 doesn't accept
# passwords via command line for new accounts.
MT5_FILE="/config/.wine/drive_c/Program Files/MetaTrader 5/terminal64.exe"

if [ -f "${MT5_FILE}" ]; then
    echo "[Phase 2] Starting MT5 terminal..."
    WINEPREFIX="${WINEPREFIX}" WINEDEBUG=-all wine "${MT5_FILE}" ${MT5_CMD_OPTIONS:-} &
    sleep 30
    echo "[Phase 2] MT5 terminal started."
else
    echo "[Phase 2] WARNING: MT5 terminal not found at ${MT5_FILE}"
    echo "[Phase 2] Searching for terminal64.exe..."
    find "${WINEPREFIX}" -name "terminal64.exe" -type f 2>/dev/null || echo "[Phase 2] Not found"
fi

# Phase 3: Start custom RPyC bridge server
# This runs under Wine Python (not Linux Python) because MetaTrader5 package
# needs to communicate with MT5 terminal through Windows IPC.
# The bridge listens on port 8001 inside the container (mapped to 5005/5006/5007 externally).
echo "[Phase 3] Starting RPyC bridge server on port 8001..."
DISPLAY=:99 WINEPREFIX=/config/.wine wine python /app/mt5_bridge_server.py 8001 &

# Give bridge server time to start
sleep 10

# Verify bridge server is listening
if python3 -c "import socket; s=socket.socket(); s.settimeout(2); s.connect(('localhost', 8001)); s.close()" 2>/dev/null; then
    echo "[Phase 3] Bridge server is running on port 8001."
else
    echo "[Phase 3] WARNING: Bridge server may not be listening on port 8001 yet."
    echo "[Phase 3] It may take a few more seconds to start under Wine."
fi

echo "=== MT5 Bridge Container Ready ==="
echo "VNC: http://localhost:3000 (or mapped port)"
echo "Bridge: port 8001 (or mapped port)"
echo ""
echo "NOTE: If this is a new container, login to MT5 via VNC first!"
echo "The bridge server cannot connect to MT5 until a successful manual login."

# Keep container running — wait for any background process
wait