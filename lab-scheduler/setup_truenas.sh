#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

echo "=========================================================="
echo " Lab Scheduler — Isolated VM / Container Setup"
echo "=========================================================="

# 0. If running inside a fresh Ubuntu/Debian VM without Docker installed yet, install it automatically
if ! command -v docker >/dev/null 2>&1; then
    echo ""
    echo "=== Installing Docker & Docker Compose ==="
    apt-get update
    apt-get install -y docker.io docker-compose-v2 || apt-get install -y docker.io docker-compose
    systemctl enable --now docker
elif ! docker compose version >/dev/null 2>&1 && ! command -v docker-compose >/dev/null 2>&1; then
    echo ""
    echo "=== Installing Docker Compose ==="
    apt-get update
    apt-get install -y docker-compose-v2 || apt-get install -y docker-compose
fi

# 1. Optional: Set Admin Password interactively if .env doesn't exist yet
if [ ! -f .env ]; then
    echo ""
    read -r -p "Set an initial Admin Password for /admin (or press Enter for 'changeme'): " INPUT_PASS
    ADMIN_PASS="${INPUT_PASS:-changeme}"
    cat > .env <<EOF
TZ=Asia/Jerusalem
ADMIN_USERNAME=admin
ADMIN_PASSWORD=${ADMIN_PASS}
DENIED_RETENTION_DAYS=30
EOF
    chmod 600 .env
    echo "Saved initial admin credentials to .env (changeable anytime at /admin/settings)."
fi

# 2. Prepare persistent folders
mkdir -p ./data ./tailscale/state

# 3. Build and start the isolated containers
echo ""
echo "=== [1/3] Building and starting containers ==="
if docker compose version >/dev/null 2>&1; then
    docker compose up -d --build
else
    docker-compose up -d --build
fi

# Wait briefly for tailscaled socket to be ready
sleep 2

# 4. Interactive Tailscale authentication
echo ""
echo "=== [2/3] Authenticating Tailscale Container ==="
echo "Open the login link below in your browser to connect this container:"
docker exec -it lab-scheduler-ts tailscale up --hostname=lab-scheduler --accept-dns=false

# 5. Enable Tailscale Funnel on port 8000
echo ""
echo "=== [3/3] Enabling Public HTTPS via Tailscale Funnel ==="
echo "If Tailscale prompts you with a link to enable HTTPS/Funnel, click it in your browser:"
docker exec -it lab-scheduler-ts tailscale funnel --bg 8000

echo ""
echo "=========================================================="
echo " Setup Complete! Current Tailscale Funnel Status:"
echo "=========================================================="
docker exec lab-scheduler-ts tailscale funnel status
echo "=========================================================="
