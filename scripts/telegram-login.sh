#!/bin/bash
# Connexion Telegram du bot (à lancer en root, dans un terminal interactif : ssh -t).
set -euo pipefail
set -a; source /etc/defi-bot/secrets.env; set +a
[ -n "${TELEGRAM_API_ID:-}" ] && [ -n "${TELEGRAM_API_HASH:-}" ] || { echo "Ajoute d'abord TELEGRAM_API_ID et TELEGRAM_API_HASH (ajouter-cle.bat)."; exit 1; }
python3 -c "import telethon" 2>/dev/null || { echo "telethon pas encore installé : attends 2 minutes et relance."; exit 1; }
sudo -u defibot --preserve-env=TELEGRAM_API_ID,TELEGRAM_API_HASH python3 /opt/defi-bot/bot/tglogin.py
systemctl restart defi-bot
