#!/bin/bash
set -e

# MT5 Bridge Entrypoint — 3-phase startup
#
# Phase 1: gmag11 initialization (installs Wine, MT5, Python — takes 2-5 min)
# Phase 1.5: Fix numpy + install rpyc (needs Xvfb running from Phase 1)
# Phase 2: Start MT5 terminal (user must login manually via VNC the first time)
# Phase 3: Start custom RPyC bridge server in Wine Python
#
# The bridge server runs as Wine Python (not Linux Python) because
# MetaTrader5 package only works under Wine where it can talk to MT5 terminal.
#
# IMPORTANT: gmag11 runs as user 'abc' (uid 911). Wine prefix at
# /config/.wine is owned by abc. All Wine commands must run as abc.

export DISPLAY=:99
export WINEPREFIX=/config/.wine
export WINEDEBUG=-all

# Run a command as user abc (gmag11's default user)
as_abc() {
    sudo -u abc DISPLAY=:99 WINEPREFIX=/config/.wine WINEDEBUG=-all "$@"
}

MT5_FILE="/config/.wine/drive_c/Program Files/MetaTrader 5/terminal64.exe"
MAX_WAIT=600  # Maximum wait time for gmag11 init (10 minutes)
WAIT_INTERVAL=10

echo "=== MT5 Bridge Container Starting ==="

# Phase 0: Start VNC services (KasmVNC + nginx) for remote access
# These are normally started by s6-overlay, but since we use our own entrypoint,
# we need to start them manually.
echo "[Phase 0] Starting VNC services..."

# Configure nginx: gmag11 uses a custom template at /defaults/default.conf
# that proxies port 3000 → KasmVNC on port 6900/6901
NGINX_CONFIG=/etc/nginx/sites-available/default
CPORT="${CUSTOM_PORT:-3000}"
CHPORT="${CUSTOM_HTTPS_PORT:-3001}"
SFOLDER="${SUBFOLDER:-/}"
CUSER="${CUSTOM_USER:-abc}"

# Generate SSL cert if missing
if [ ! -f "/config/ssl/cert.pem" ]; then
    mkdir -p /config/ssl
    openssl req -new -x509 -days 3650 -nodes \
        -out /config/ssl/cert.pem -keyout /config/ssl/cert.key \
        -subj "/C=US/ST=CA/L=Carlsbad/O=LSIO/CN=*" 2>/dev/null
    chmod 600 /config/ssl/cert.key
    chown -R abc:abc /config/ssl
fi

# Copy and configure nginx template
cp /defaults/default.conf ${NGINX_CONFIG}
sed -i "s/3000/$CPORT/g" ${NGINX_CONFIG}
sed -i "s/3001/$CHPORT/g" ${NGINX_CONFIG}
sed -i "s|SUBFOLDER|$SFOLDER|g" ${NGINX_CONFIG}

# Set up basic auth if PASSWORD is set
if [ ! -z "${PASSWORD+x}" ]; then
    printf "${CUSER}:$(openssl passwd -apr1 ${PASSWORD})\n" > /etc/nginx/.htpasswd
    sed -i 's/#//g' ${NGINX_CONFIG}
fi

# Create XDG runtime dir for abc
if [ ! -d "/config/.XDG" ]; then
    mkdir -p /config/.XDG
    chown abc:abc /config/.XDG
fi
export XDG_RUNTIME_DIR=/config/.XDG

# Start nginx (provides web interface on port 3000)
nginx &
echo "[Phase 0] nginx started."

# Start KasmVNC (web-based VNC server)
# Runs as user abc on display :99
s6-setuidgid abc /usr/local/bin/Xvnc :99 \
    -PublicIP 127.0.0.1 \
    -disableBasicAuth \
    -SecurityTypes None \
    -AlwaysShared \
    -geometry 1024x768 \
    -sslOnly 0 \
    -RectThreads 0 \
    -websocketPort 6901 \
    -interface 0.0.0.0 \
    -Log *:stdout:10 &
echo "[Phase 0] KasmVNC started on port 6901 (web on port 3000)."

# Phase 1: Run gmag11's start.sh to initialize everything
# This handles: Wine setup, MT5 install, Python install, pip packages
echo "[Phase 1] Running gmag11 initialization..."
/original_start.sh &

# Wait for gmag11 initialization to complete by checking for MT5 terminal
# gmag11 downloads and installs MT5 terminal as part of its init — when the
# terminal file exists, initialization is complete.
echo "[Phase 1] Waiting for MT5 terminal to be installed (up to ${MAX_WAIT}s)..."
elapsed=0
while [ ! -f "${MT5_FILE}" ] && [ ${elapsed} -lt ${MAX_WAIT} ]; do
    sleep ${WAIT_INTERVAL}
    elapsed=$((elapsed + WAIT_INTERVAL))
    echo "[Phase 1] Still waiting... (${elapsed}s elapsed)"
done

if [ ! -f "${MT5_FILE}" ]; then
    echo "[Phase 1] ERROR: MT5 terminal not found after ${MAX_WAIT}s"
    echo "[Phase 1] Searching for terminal64.exe..."
    find /config/.wine -name "terminal64.exe" -type f 2>/dev/null || echo "[Phase 1] Not found anywhere"
    echo "[Phase 1] Container will keep running. Try restarting after MT5 installs."
else
    echo "[Phase 1] MT5 terminal found at ${MT5_FILE} (${elapsed}s)"
fi

# Phase 1.5: Fix Python packages after gmag11 init completes
# These can't be done at Docker build time because:
# - Wine needs Xvfb to run pip (not available during build)
# - Debian 12 blocks system-wide pip installs (PEP 668)
echo "[Phase 1.5] Fixing Python packages..."

# Downgrade numpy in Wine Python — numpy 2.x is incompatible with MetaTrader5
echo "[Phase 1.5] Downgrading numpy in Wine Python (2.x incompatible with MT5)..."
as_abc wine python -m pip install 'numpy<2' --force-reinstall || {
    echo "[Phase 1.5] WARNING: numpy downgrade failed, bridge may not work correctly"
}

# Install rpyc in Wine Python for the bridge server
echo "[Phase 1.5] Installing rpyc in Wine Python (for bridge server)..."
as_abc wine python -m pip install 'rpyc>=5.2.0' || {
    echo "[Phase 1.5] WARNING: rpyc Wine install failed"
}

echo "[Phase 1.5] Python package fixes complete."

# Phase 2: Start MT5 terminal
if [ -f "${MT5_FILE}" ]; then
    echo "[Phase 2] Starting MT5 terminal..."
    as_abc wine "${MT5_FILE}" ${MT5_CMD_OPTIONS:-} &
    sleep 30
    echo "[Phase 2] MT5 terminal started."
else
    echo "[Phase 2] WARNING: MT5 terminal not found, cannot start."
fi

# Phase 3: Start custom RPyC bridge server
# This runs under Wine Python (not Linux Python) because MetaTrader5 package
# needs to communicate with MT5 terminal through Windows IPC.
# The bridge listens on port 8001 inside the container (mapped to 5005/5006/5007 externally).
echo "[Phase 3] Starting RPyC bridge server on port 8001..."
as_abc wine python /app/mt5_bridge_server.py 8001 &

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