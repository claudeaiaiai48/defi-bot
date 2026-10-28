#!/bin/bash
# Applique la configuration système (services) et redémarre le bot.
set -euo pipefail
mkdir -p /var/lib/defi-bot/public /etc/defi-bot
chown -R defibot:defibot /var/lib/defi-bot
touch /etc/defi-bot/secrets.env
chown root:defibot /etc/defi-bot/secrets.env
chmod 640 /etc/defi-bot/secrets.env

cat > /etc/systemd/system/defi-bot.service <<'UNIT'
[Unit]
Description=defi-bot (observation)
After=network-online.target
Wants=network-online.target
[Service]
User=defibot
EnvironmentFile=-/etc/defi-bot/secrets.env
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

cat > /etc/systemd/system/defi-bot-backtest.service <<'UNIT'
[Unit]
Description=defi-bot backtest hors ligne (journaux d'observation -> backtest.json)
[Service]
Type=oneshot
User=defibot
Nice=19
IOSchedulingClass=idle
CPUQuota=60%
MemoryMax=900M
ExecStart=/usr/bin/python3 /opt/defi-bot/bot/backtest.py
UNIT

cat > /etc/systemd/system/defi-bot-backtest.timer <<'UNIT'
[Unit]
Description=defi-bot backtest toutes les 6 heures
[Timer]
OnBootSec=15min
OnUnitActiveSec=6h
[Install]
WantedBy=timers.target
UNIT

cat > /etc/systemd/system/defi-bot-recherche.service <<'UNIT'
[Unit]
Description=defi-bot recherche continue des meilleurs reglages (backtest en boucle)
After=network-online.target
[Service]
User=defibot
Nice=19
IOSchedulingClass=idle
CPUQuota=40%
MemoryMax=700M
ExecStart=/usr/bin/python3 /opt/defi-bot/bot/recherche_continue.py
Restart=always
RestartSec=60
[Install]
WantedBy=multi-user.target
UNIT

python3 -c "import telethon" 2>/dev/null || {
  DEBIAN_FRONTEND=noninteractive apt-get install -y -q python3-pip >/dev/null 2>&1 || true
  pip3 install -q --break-system-packages telethon >/dev/null 2>&1 || true
}
python3 -c "import websocket" 2>/dev/null || {
  DEBIAN_FRONTEND=noninteractive apt-get install -y -q python3-pip >/dev/null 2>&1 || true
  pip3 install -q --break-system-packages websocket-client >/dev/null 2>&1 || true
}
systemctl daemon-reload
systemctl enable -q defi-bot defi-bot-web defi-bot-update.timer
systemctl restart defi-bot defi-bot-web
systemctl start defi-bot-update.timer
systemctl enable -q --now defi-bot-backtest.timer || true
systemctl enable -q defi-bot-recherche || true
systemctl restart defi-bot-recherche || true
systemctl start --no-block defi-bot-backtest.service || true
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow 22/tcp >/dev/null; ufw allow 8080/tcp >/dev/null
fi
systemctl --no-pager --lines=0 status defi-bot defi-bot-web | grep -E "●|Active:" || true
