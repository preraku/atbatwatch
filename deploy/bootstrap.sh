#!/usr/bin/env bash
# One-shot setup for a fresh Hetzner CX22 (Ubuntu 24.04).
# Run as root: bash bootstrap.sh
set -euo pipefail

REPO_URL="https://github.com/preraku/atbatwatch.git"
APP_DIR="/opt/atbatwatch"
DEPLOY_USER="deploy"

# ── System updates ──────────────────────────────────────────────────────────
apt-get update -y
apt-get upgrade -y
apt-get install -y ca-certificates curl gnupg ufw unattended-upgrades git

# Enable unattended security upgrades
dpkg-reconfigure --priority=low unattended-upgrades

# ── Non-root deploy user ─────────────────────────────────────────────────────
if ! id "$DEPLOY_USER" &>/dev/null; then
    useradd -m -s /bin/bash "$DEPLOY_USER"
fi
mkdir -p "/home/$DEPLOY_USER/.ssh"
if [[ -n "${DEPLOY_PUBKEY:-}" ]]; then
    echo "$DEPLOY_PUBKEY" > "/home/$DEPLOY_USER/.ssh/authorized_keys"
elif [[ -f /root/.ssh/authorized_keys ]]; then
    cp /root/.ssh/authorized_keys "/home/$DEPLOY_USER/.ssh/authorized_keys"
fi
chown -R "$DEPLOY_USER:$DEPLOY_USER" "/home/$DEPLOY_USER/.ssh"
chmod 700 "/home/$DEPLOY_USER/.ssh"
chmod 600 "/home/$DEPLOY_USER/.ssh/authorized_keys"
usermod -aG docker "$DEPLOY_USER" 2>/dev/null || true

# ── Docker ───────────────────────────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    # shellcheck disable=SC1091
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
        https://download.docker.com/linux/ubuntu \
        $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update -y
    apt-get install -y docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin
    systemctl enable --now docker
    usermod -aG docker "$DEPLOY_USER"
fi

# ── UFW firewall ─────────────────────────────────────────────────────────────
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp   comment 'SSH'
ufw allow 80/tcp   comment 'HTTP (Caddy)'
ufw allow 443/tcp  comment 'HTTPS (Caddy)'
ufw allow 443/udp  comment 'HTTP/3 (Caddy)'
ufw --force enable

# ── Clone repo ───────────────────────────────────────────────────────────────
if [[ ! -d "$APP_DIR/.git" ]]; then
    git clone "$REPO_URL" "$APP_DIR"
    chown -R "$DEPLOY_USER:$DEPLOY_USER" "$APP_DIR"
fi

# ── Secrets ──────────────────────────────────────────────────────────────────
# .env is written on every deploy by the GitHub Actions workflow via scp.
# Seed a placeholder so docker-compose doesn't fail before the first deploy.
if [[ ! -f "$APP_DIR/.env" ]]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
fi

# ── Install backup cron (optional — requires STORAGE_BOX vars in .env) ───────
CRON_LINE="0 3 * * * $DEPLOY_USER bash $APP_DIR/deploy/backup.sh >> /var/log/atbatwatch-backup.log 2>&1"
if ! grep -qF "atbatwatch-backup" /etc/cron.d/atbatwatch 2>/dev/null; then
    echo "$CRON_LINE" > /etc/cron.d/atbatwatch
    chmod 644 /etc/cron.d/atbatwatch
fi

# ── Start services ────────────────────────────────────────────────────────────
cd "$APP_DIR"
docker compose -f docker-compose.prod.yml up -d --build

echo ""
echo "Bootstrap complete. Check: docker compose -f $APP_DIR/docker-compose.prod.yml ps"
