#!/bin/bash
# VPS Setup Script for God Port Oracle MT5 Bridge
# Run this script on your Ubuntu x86_64 VPS as root
#
# Prerequisites: Fresh Ubuntu 22.04+ VPS with root access
#
# Usage:
#   1. SSH into your VPS: ssh root@vpsdeluna
#   2. Copy this script and run it: bash vps-setup.sh
#
# After setup, clone the repo and start containers:
#   cd /opt/god-port-oracle
#   docker compose -f docker-compose.vps.yml up -d

set -e

echo "=== God Port Oracle VPS Setup ==="
echo "Target: Ubuntu x86_64 with Docker"

# Check architecture
ARCH=$(uname -m)
if [ "$ARCH" != "x86_64" ]; then
    echo "ERROR: This script requires x86_64 architecture. Current: $ARCH"
    exit 1
fi
echo "Architecture: $ARCH ✓"

# Update system
echo "Updating system packages..."
apt-get update && apt-get upgrade -y

# Install Docker
if command -v docker &> /dev/null; then
    echo "Docker is already installed: $(docker --version)"
else
    echo "Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable docker
    systemctl start docker
    echo "Docker installed: $(docker --version)"
fi

# Install Docker Compose
if command -v docker &> /dev/null && docker compose version &> /dev/null; then
    echo "Docker Compose is already installed: $(docker compose version)"
else
    echo "Installing Docker Compose..."
    apt-get install -y docker-compose-plugin
    echo "Docker Compose installed: $(docker compose version)"
fi

# Install Git
if command -v git &> /dev/null; then
    echo "Git is already installed: $(git --version)"
else
    echo "Installing Git..."
    apt-get install -y git
fi

# Open firewall ports for MT5 bridges and VNC
echo "Configuring firewall..."
if command -v ufw &> /dev/null; then
    ufw allow 5005/tcp  # MT5 Bridge Account A
    ufw allow 5006/tcp  # MT5 Bridge Account B
    ufw allow 5007/tcp  # MT5 Bridge Account C
    ufw allow 3000/tcp  # VNC Account A
    ufw allow 3001/tcp  # VNC Account B
    ufw allow 3002/tcp  # VNC Account C
    ufw allow 22/tcp    # SSH
    echo "Firewall rules added"
else
    echo "UFW not installed. Manual firewall configuration may be needed."
    echo "Required ports: 22, 3000, 3001, 3002, 5005, 5006, 5007"
fi

# Clone repo
REPO_DIR="/opt/god-port-oracle"
if [ -d "$REPO_DIR" ]; then
    echo "Repository already exists at $REPO_DIR"
    cd "$REPO_DIR"
    git pull || true
else
    echo "Cloning repository..."
    git clone https://github.com/doctorboyz/god-port-oracle.git "$REPO_DIR"
    cd "$REPO_DIR"
fi

# Create .env file if it doesn't exist
if [ ! -f "$REPO_DIR/.env" ]; then
    echo "Creating .env file from template..."
    cp "$REPO_DIR/.env.example" "$REPO_DIR/.env"
    echo "⚠️  IMPORTANT: Edit .env with your MT5 credentials before starting containers!"
    echo "   nano /opt/god-port-oracle/.env"
else
    echo ".env file already exists"
fi

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Next steps:"
echo "1. Edit .env with your MT5 credentials:"
echo "   nano /opt/god-port-oracle/.env"
echo ""
echo "2. Build and start MT5 containers:"
echo "   cd /opt/god-port-oracle"
echo "   docker compose -f docker-compose.vps.yml build"
echo "   docker compose -f docker-compose.vps.yml up -d"
echo ""
echo "3. Check container status:"
echo "   docker compose -f docker-compose.vps.yml ps"
echo ""
echo "4. View logs:"
echo "   docker compose -f docker-compose.vps.yml logs -f mt5-account-a"
echo ""
echo "5. Test bridge connection from macOS:"
echo "   python3 -c \"import rpyc; c=rpyc.connect('vpsdeluna', 5005); print('Bridge A OK')\""