#!/bin/bash
# Installation initiale du serveur defi-bot (à lancer une seule fois, en root).
set -euo pipefail
REPO_URL="https://github.com/claudeaiaiai48/defi-bot.git"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q git python3 ca-certificates
id defibot >/dev/null 2>&1 || useradd --system --home /var/lib/defi-bot --shell /usr/sbin/nologin defibot
if [ -d /opt/defi-bot/.git ]; then
  git -C /opt/defi-bot fetch -q origin main && git -C /opt/defi-bot reset -q --hard origin/main
else
  git clone -q "$REPO_URL" /opt/defi-bot
fi
bash /opt/defi-bot/scripts/apply.sh
echo "INSTALLATION_OK"
