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

# Lock ./data ownership to non-root container UID 10001 (mode 700)
chown -R 10001:10001 ./data
chmod 700 ./data
if [ -f ./data/lab_production.db ]; then
    chmod 600 ./data/lab_production.db*
fi

# Lock ./tailscale/state so Tailscale's private node key is readable only by root
chown -R 0:0 ./tailscale/state
chmod 700 ./tailscale/state

# 3. Build and update containers in-place (preserving ./data and ./tailscale/state volumes)
echo ""
echo "=== [1/5] Building and updating hardened containers ==="
if docker compose version >/dev/null 2>&1; then
    docker compose up -d --build --remove-orphans
else
    docker-compose up -d --build --remove-orphans
fi

# 4. Isolate `br-labsched` containers from reaching private LAN / TrueNAS IPs via DOCKER-USER
echo ""
echo "=== [2/5] Applying DOCKER-USER Local-Network Isolation Firewall ==="
if command -v iptables >/dev/null 2>&1 && iptables -L DOCKER-USER >/dev/null 2>&1; then
    # Remove any previous br-labsched rules so re-runs stay clean & idempotent
    while iptables -S DOCKER-USER | grep -q -- "-i br-labsched"; do
        RULE=$(iptables -S DOCKER-USER | grep -- "-i br-labsched" | head -n1 | sed 's/^-A /-D /')
        # shellcheck disable=SC2086
        iptables $RULE || break
    done

    # Insert from bottom to top at position 1 so final order is:
    # 1. Allow ESTABLISHED/RELATED return traffic
    # 2. Allow DNS (UDP/TCP 53) to gateway & configured nameservers
    # 3. DROP any NEW outbound connections from br-labsched to private LAN / link-local ranges
    for SUBNET in 169.254.0.0/16 172.16.0.0/12 10.0.0.0/8 192.168.0.0/16; do
        iptables -I DOCKER-USER 1 -i br-labsched -d "${SUBNET}" -j DROP
    done

    # Allow DNS to default gateway and /etc/resolv.conf nameservers so Tailscale & holiday sync work
    GW_IP=$(ip route 2>/dev/null | awk '/default/ {print $3; exit}' || true)
    DNS_IPS=$(awk '/^nameserver/ {print $2}' /etc/resolv.conf 2>/dev/null || true)
    for NS in ${GW_IP} ${DNS_IPS}; do
        case "${NS}" in
            ''|127.*) continue ;;
        esac
        iptables -I DOCKER-USER 1 -i br-labsched -d "${NS}" -p tcp --dport 53 -j RETURN
        iptables -I DOCKER-USER 1 -i br-labsched -d "${NS}" -p udp --dport 53 -j RETURN
    done

    iptables -I DOCKER-USER 1 -i br-labsched -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN

    if command -v netfilter-persistent >/dev/null 2>&1; then
        netfilter-persistent save >/dev/null 2>&1 || true
    fi
    echo "DOCKER-USER isolation active: containers on br-labsched are blocked from accessing your LAN/TrueNAS."
else
    echo "Skipping iptables DOCKER-USER rules (iptables chain not present in this environment)."
fi

# 5. Verify FastAPI app is listening on 127.0.0.1:8000 inside the shared namespace
echo ""
echo "=== [3/5] Verifying FastAPI app health on 127.0.0.1:8000 ==="
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

# 6. Authenticate Tailscale (only prompts if not already authenticated)
echo ""
echo "=== [4/5] Checking Tailscale Connection ==="
if ! docker exec lab-scheduler-ts tailscale status >/dev/null 2>&1; then
    echo "Open the login link below in your browser to connect:"
    docker exec -it lab-scheduler-ts tailscale up --hostname=lab-scheduler --accept-dns=false
else
    echo "Tailscale is already authenticated."
fi

# 7. Ensure Tailscale Funnel is active on port 8000 & HTTPS certificate is valid
echo ""
echo "=== [5/5] Ensuring Public HTTPS via Tailscale Funnel ==="
docker exec -it lab-scheduler-ts tailscale funnel --bg 8000

TS_STATUS_JSON=$(docker exec lab-scheduler-ts tailscale status --json 2>/dev/null || true)
TS_DOMAIN=$(echo "${TS_STATUS_JSON}" | grep -o '"DNSName":[[:space:]]*"[^"]*"' | head -n1 | cut -d'"' -f4 | sed 's/\.$//')
if [ -n "${TS_DOMAIN:-}" ]; then
    docker exec lab-scheduler-ts tailscale cert "${TS_DOMAIN}" >/dev/null 2>&1 || true
fi

if ! echo "${TS_STATUS_JSON}" | grep -q "tag:funnel-only"; then
    echo ""
    echo "SECURITY REMINDER: Assign tag 'tag:funnel-only' to 'lab-scheduler' in"
    echo "https://login.tailscale.com/admin/machines (... -> Edit ACL tags)"
    echo "so this container cannot connect to your other Tailscale devices!"
fi

echo ""
echo "=========================================================="
echo " Update Complete! Current Tailscale Funnel Status:"
echo "=========================================================="
docker exec lab-scheduler-ts tailscale funnel status
echo "=========================================================="
