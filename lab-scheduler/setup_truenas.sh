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

# 2. Prepare persistent folders and ensure SQLite database is writable inside container
mkdir -p ./data ./tailscale/state
chmod -R 777 ./data

# 3. Build and start the containers cleanly
echo ""
echo "=== [1/4] Building and starting containers ==="
if docker compose version >/dev/null 2>&1; then
    docker compose down --remove-orphans || true
    docker compose up -d --build
else
    docker-compose down --remove-orphans || true
    docker-compose up -d --build
fi

# 4. Verify FastAPI app is listening on 127.0.0.1:8000 inside the shared namespace
echo ""
echo "=== [2/4] Verifying FastAPI app health on 127.0.0.1:8000 ==="
APP_OK=0
for i in 1 2 3 4 5 6 7 8 9 10; do
    if docker exec lab-scheduler curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; then
        APP_OK=1
        echo "FastAPI app is healthy and listening on 127.0.0.1:8000!"
        break
    fi
    sleep 1
done

if [ "$APP_OK" -ne 1 ]; then
    echo "ERROR: lab-scheduler container is not responding on port 8000. Logs:"
    docker logs lab-scheduler || true
    exit 1
fi

# 5. Interactive Tailscale authentication
echo ""
echo "=== [3/4] Authenticating Tailscale Container ==="
echo "If prompted below, open the login link in your browser to connect:"
docker exec -it lab-scheduler-ts tailscale up --hostname=lab-scheduler --accept-dns=false

# 6. Enable Tailscale Funnel on port 8000 & pre-provision HTTPS certificate
echo ""
echo "=== [4/4] Enabling Public HTTPS via Tailscale Funnel ==="
docker exec -it lab-scheduler-ts tailscale funnel --bg 8000

TS_DOMAIN=$(docker exec lab-scheduler-ts tailscale status --json | grep -o '"DNSName":[[:space:]]*"[^"]*"' | head -n1 | cut -d'"' -f4 | sed 's/\.$//')
if [ -n "${TS_DOMAIN:-}" ]; then
    echo "Provisioning Let's Encrypt HTTPS certificate for ${TS_DOMAIN} (this may take 15-30 seconds on first run)..."
    docker exec lab-scheduler-ts tailscale cert "${TS_DOMAIN}" >/dev/null 2>&1 || true
fi

echo ""
echo "=========================================================="
echo " Setup Complete! Current Tailscale Funnel Status:"
echo "=========================================================="
docker exec lab-scheduler-ts tailscale funnel status
echo "=========================================================="
