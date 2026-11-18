#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

echo "=========================================================="
echo " Lab Scheduler — Hardened Setup & In-Place Updater"
echo "=========================================================="

# 0. Install Docker & Docker Compose automatically if missing inside a fresh VM
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

# 1. Only ask for initial Admin Password on first install (never overwrites existing .env)
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

# 2. Preserve existing dataset & migrate legacy DB to git-ignored lab_production.db once
mkdir -p ./data ./tailscale/state
if [ ! -f ./data/lab_production.db ] && [ -f ./data/lab_scheduler.db ]; then
    cp -a ./data/lab_scheduler.db ./data/lab_production.db
    echo "Migrated existing bookings/credentials to ./data/lab_production.db (protected from git updates)."
fi

# Lock ./data ownership to non-root container UID 10001 (mode 700, no world-writable 777)
chown -R 10001:10001 ./data
chmod 700 ./data
if [ -f ./data/lab_production.db ]; then
    chmod 600 ./data/lab_production.db*
fi

# 3. Build and update containers in-place (preserving ./data and ./tailscale/state volumes)
echo ""
echo "=== [1/4] Building and updating containers ==="
if docker compose version >/dev/null 2>&1; then
    docker compose up -d --build --remove-orphans
else
    docker-compose up -d --build --remove-orphans
fi

# 4. Verify FastAPI app is listening on 127.0.0.1:8000 inside the shared namespace
echo ""
echo "=== [2/4] Verifying FastAPI app health on 127.0.0.1:8000 ==="
APP_OK=0
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
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

# 5. Authenticate Tailscale (only prompts if not already authenticated)
echo ""
echo "=== [3/4] Checking Tailscale Connection ==="
if ! docker exec lab-scheduler-ts tailscale status >/dev/null 2>&1; then
    echo "Open the login link below in your browser to connect:"
    docker exec -it lab-scheduler-ts tailscale up --hostname=lab-scheduler --accept-dns=false
else
    echo "Tailscale is already authenticated."
fi

# 6. Ensure Tailscale Funnel is active on port 8000 & HTTPS certificate is valid
echo ""
echo "=== [4/4] Ensuring Public HTTPS via Tailscale Funnel ==="
docker exec -it lab-scheduler-ts tailscale funnel --bg 8000

TS_DOMAIN=$(docker exec lab-scheduler-ts tailscale status --json | grep -o '"DNSName":[[:space:]]*"[^"]*"' | head -n1 | cut -d'"' -f4 | sed 's/\.$//')
if [ -n "${TS_DOMAIN:-}" ]; then
    docker exec lab-scheduler-ts tailscale cert "${TS_DOMAIN}" >/dev/null 2>&1 || true
fi

echo ""
echo "=========================================================="
echo " Update Complete! Current Tailscale Funnel Status:"
echo "=========================================================="
docker exec lab-scheduler-ts tailscale funnel status
echo "=========================================================="
