#!/usr/bin/env bash
# =============================================================================
# DarkGPT — deploy inside an LXD container on your Contabo VPS
# Allocates: 2 GB RAM, 2 vCPU, 8 GB disk (inside a 10 GB btrfs pool)
#
# RUN THIS ON THE VPS (as root):
#   1) scp this file to the server, or paste it into a file
#   2) edit the CONFIG block below (tokens + your Telegram ID)
#   3) chmod +x deploy_darkgpt_lxc.sh && ./deploy_darkgpt_lxc.sh
# =============================================================================
set -euo pipefail

# ------------------------- CONFIG — EDIT THESE -------------------------------
CONTAINER="darkgpt"
IMAGE="ubuntu:22.04"
MEM_LIMIT="2GB"
CPU_LIMIT="2"
DISK_SIZE="8GB"            # container root cap (pool below is 10GB)
POOL_SIZE="10"            # GB, btrfs loop pool

# --- The git branch to deploy (merge to main first, or use the feature branch)
GIT_BRANCH="claude/chatgpt-access-control-supabase-j6duxk"
# --- Private repo clone URL. Create a GitHub token (repo read scope) and put it here:
GITHUB_TOKEN="ghp_PUT_YOUR_TOKEN_HERE"
GIT_URL="https://${GITHUB_TOKEN}@github.com/snackshell/DarkGPT.git"

# --- App secrets (these become the container's environment) --------------------
BOT_TOKEN="PUT_TELEGRAM_BOT_TOKEN"
OPENROUTER_API_KEY="PUT_OPENROUTER_KEY"
SUPABASE_URL="https://fxfquwoshovdnqgjejtz.supabase.co"
SUPABASE_SERVICE_KEY="PUT_SUPABASE_SECRET_KEY"     # the sb_secret_... / service_role key
ADMIN_IDS="PUT_YOUR_TELEGRAM_ID"                    # e.g. 123456789
# -----------------------------------------------------------------------------

echo ">> [1/7] Installing LXD (if needed)..."
if ! command -v lxd >/dev/null 2>&1; then
  apt-get update -y
  apt-get install -y snapd
  snap install lxd
fi
export PATH="$PATH:/snap/bin"

echo ">> [2/7] Initialising LXD (10GB btrfs pool, only if not already set up)..."
if ! lxc storage list --format csv 2>/dev/null | grep -q '^default,'; then
  lxd init --auto --storage-backend btrfs --storage-create-loop "${POOL_SIZE}"
else
  echo "   LXD already initialised, reusing existing pool."
fi

echo ">> [3/7] Launching container '${CONTAINER}'..."
if lxc info "${CONTAINER}" >/dev/null 2>&1; then
  echo "   Container exists; stopping to reapply config."
  lxc stop "${CONTAINER}" --force || true
else
  lxc launch "${IMAGE}" "${CONTAINER}"
fi

echo ">> [4/7] Applying resource limits (RAM ${MEM_LIMIT}, CPU ${CPU_LIMIT}, disk ${DISK_SIZE})..."
lxc config set "${CONTAINER}" limits.memory "${MEM_LIMIT}"
lxc config set "${CONTAINER}" limits.memory.enforce hard
lxc config set "${CONTAINER}" limits.cpu "${CPU_LIMIT}"
lxc config device override "${CONTAINER}" root size="${DISK_SIZE}" 2>/dev/null || \
  lxc config device set "${CONTAINER}" root size="${DISK_SIZE}"
lxc start "${CONTAINER}" 2>/dev/null || true

echo ">> Waiting for container network..."
for i in $(seq 1 30); do
  if lxc exec "${CONTAINER}" -- getent hosts github.com >/dev/null 2>&1; then break; fi
  sleep 2
done

echo ">> [5/7] Installing system packages inside the container..."
lxc exec "${CONTAINER}" -- bash -c '
  set -e
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y python3 python3-pip python3-venv git
'

echo ">> [6/7] Cloning repo + installing Python deps..."
lxc exec "${CONTAINER}" -- bash -c "
  set -e
  rm -rf /opt/DarkGPT
  git clone --branch '${GIT_BRANCH}' --depth 1 '${GIT_URL}' /opt/DarkGPT
  python3 -m venv /opt/DarkGPT/venv
  /opt/DarkGPT/venv/bin/pip install --upgrade pip
  /opt/DarkGPT/venv/bin/pip install -r /opt/DarkGPT/requirements.txt
"

echo ">> Writing environment file (secrets) inside container..."
lxc exec "${CONTAINER}" -- bash -c "cat > /etc/darkgpt.env <<EOF
BOT_TOKEN=${BOT_TOKEN}
OPENROUTER_API_KEY=${OPENROUTER_API_KEY}
SUPABASE_URL=${SUPABASE_URL}
SUPABASE_SERVICE_KEY=${SUPABASE_SERVICE_KEY}
ADMIN_IDS=${ADMIN_IDS}
PORT=8080
EOF
chmod 600 /etc/darkgpt.env"

echo ">> [7/7] Installing systemd service + starting bot..."
lxc exec "${CONTAINER}" -- bash -c 'cat > /etc/systemd/system/darkgpt.service <<EOF
[Unit]
Description=DarkGPT Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/DarkGPT
EnvironmentFile=/etc/darkgpt.env
ExecStart=/opt/DarkGPT/venv/bin/python /opt/DarkGPT/darkgpt_bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now darkgpt.service'

echo
echo "============================================================"
echo " DarkGPT deployed in container '${CONTAINER}'."
echo " Check logs:   lxc exec ${CONTAINER} -- journalctl -u darkgpt -f"
echo " Restart:      lxc exec ${CONTAINER} -- systemctl restart darkgpt"
echo " Update code:  lxc exec ${CONTAINER} -- bash -c 'cd /opt/DarkGPT && git pull && systemctl restart darkgpt'"
echo "============================================================"
