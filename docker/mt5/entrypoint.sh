#!/bin/bash
set -e

# MT5 Bridge Entrypoint — 3-phase startup
#
# Phase 0: VNC services (nginx + KasmVNC)
# Phase 1: gmag11 initialization (installs Wine, MT5, Python — takes 2-5 min)
# Phase 1.5: Fix numpy + install rpyc (needs Xvfb running from Phase 1)
# Phase 2: Start MT5 terminal (user must login manually via VNC the first time)
# Phase 3: Start custom RPyC bridge server in Wine Python
#
# The bridge server runs as Wine Python (not Linux Python) because
# MetaTrader5 package only works under Wine where it can talk to MT5 terminal.
#
# CRITICAL: gmag11's start.sh runs as root and creates files in /config as root.
# Wine refuses to use a prefix not owned by the executing user.
# We fix this by waiting for gmag11 to FULLY complete, then chowning everything
# to user abc (uid 911) before running any Wine commands as abc.

export DISPLAY=:99
export WINEPREFIX=/config/.wine
export WINEDEBUG=-all

# Run a command as user abc (gmag11's default user)
as_abc() {
    sudo -u abc DISPLAY=:99 WINEPREFIX=/config/.wine WINEDEBUG=-all "$@"
}

MT5_FILE="/config/.wine/drive_c/Program Files/MetaTrader 5/terminal64.exe"
GMAG11_TIMEOUT=600  # Max wait for gmag11 init (10 minutes)

echo "=== MT5 Bridge Container Starting ==="

# Phase 0: Start VNC services (KasmVNC + nginx) for remote access
echo "[Phase 0] Starting VNC services..."

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

# KasmVNC serves both HTTP and websocket on port 6901, not 6900
sed -i 's|proxy_pass.*http://127.0.0.1:6900;|proxy_pass http://127.0.0.1:6901;|g' ${NGINX_CONFIG}

if [ ! -z "${PASSWORD+x}" ]; then
    printf "${CUSER}:$(openssl passwd -apr1 ${PASSWORD})\n" > /etc/nginx/.htpasswd
    sed -i 's/#//g' ${NGINX_CONFIG}
fi

if [ ! -d "/config/.XDG" ]; then
    mkdir -p /config/.XDG
    chown abc:abc /config/.XDG
fi
export XDG_RUNTIME_DIR=/config/.XDG

nginx &
echo "[Phase 0] nginx started."

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

# Phase 1: Run gmag11's start.sh and WAIT for it to fully complete.
# gmag11's start.sh runs as root and creates /config/.wine owned by root.
# We must wait for it to finish before fixing ownership.
echo "[Phase 1] Running gmag11 initialization..."
/original_start.sh &
GMAG11_PID=$!

echo "[Phase 1] Waiting for gmag11 init to complete (PID ${GMAG11_PID}, up to ${GMAG11_TIMEOUT}s)..."
elapsed=0
while kill -0 ${GMAG11_PID} 2>/dev/null && [ ${elapsed} -lt ${GMAG11_TIMEOUT} ]; do
    sleep 10
    elapsed=$((elapsed + 10))
    echo "[Phase 1] Still waiting... (${elapsed}s elapsed)"
done

if kill -0 ${GMAG11_PID} 2>/dev/null; then
    echo "[Phase 1] WARNING: gmag11 init still running after ${GMAG11_TIMEOUT}s, proceeding anyway"
else
    echo "[Phase 1] gmag11 init completed (${elapsed}s)"
fi

# Verify MT5 terminal exists
if [ ! -f "${MT5_FILE}" ]; then
    echo "[Phase 1] ERROR: MT5 terminal not found"
    find /config/.wine -name "terminal64.exe" -type f 2>/dev/null || echo "[Phase 1] Not found anywhere"
fi

# Fix ownership: gmag11's start.sh creates everything as root.
# Wine refuses to run if the prefix isn't owned by the executing user.
# This MUST happen after gmag11 finishes — not just after terminal64.exe exists.
echo "[Phase 1] Fixing /config ownership for user abc..."
chown -R abc:abc /config

echo "[Phase 1] Ownership fix complete. /config/.wine owner:"
ls -la /config/.wine | head -3

# Phase 1.5: Fix Python packages after gmag11 init + chown
echo "[Phase 1.5] Fixing Python packages..."

echo "[Phase 1.5] Downgrading numpy in Wine Python (2.x incompatible with MT5)..."
as_abc wine python -m pip install 'numpy<2' --force-reinstall || {
    echo "[Phase 1.5] WARNING: numpy downgrade failed, bridge may not work correctly"
}

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
echo "[Phase 3] Starting RPyC bridge server on port 8001..."
as_abc wine python /app/mt5_bridge_server.py 8001 &

sleep 10

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