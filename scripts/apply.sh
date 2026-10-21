#!/bin/bash
# Applique la configuration système (services) et redémarre le bot.
set -euo pipefail
mkdir -p /var/lib/defi-bot/public
chown -R defibot:defibot /var/lib/defi-bot

cat > /etc/systemd/system/defi-bot.service <<'UNIT'
[Unit]
Description=defi-bot (observation)
After=network-online.target
Wants=network-online.target
[Service]
User=defibot
ExecStart=/usr/bin/python3 /opt/defi-bot/bot/observer.py
Restart=always
RestartSec=15
[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/defi-bot-web.service <<'UNIT'
[Unit]
Description=defi-bot statut public (lecture seule)
After=network-online.target
[Service]
User=defibot
ExecStart=/usr/bin/python3 -m http.server 8080 --directory /var/lib/defi-bot/public
Restart=always
[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/defi-bot-update.service <<'UNIT'
[Unit]
Description=defi-bot mise à jour depuis GitHub
[Service]
Type=oneshot
ExecStart=/bin/bash /opt/defi-bot/scripts/update.sh
UNIT

cat > /etc/systemd/system/defi-bot-update.timer <<'UNIT'
[Unit]
Description=defi-bot mise à jour toutes les 2 minutes
[Timer]
OnBootSec=1min
OnUnitActiveSec=2min
[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable -q defi-bot defi-bot-web defi-bot-update.timer
systemctl restart defi-bot defi-bot-web
systemctl start defi-bot-update.timer
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow 22/tcp >/dev/null; ufw allow 8080/tcp >/dev/null
fi
systemctl --no-pager --lines=0 status defi-bot defi-bot-web | grep -E "●|Active:" || true
