#!/bin/bash
# Enregistre une clé API sur le serveur (jamais dans le dépôt GitHub).
# Usage (en root) : echo -n "valeur" | set-secret.sh NOM_DE_LA_CLE
set -euo pipefail
name="${1:-}"
[[ "$name" =~ ^[A-Z][A-Z0-9_]{1,40}$ ]] || { echo "nom de clé invalide"; exit 1; }
value="$(cat | tr -d '\r' | tr -cd '[:print:]\n' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
[ -n "$value" ] || { echo "valeur vide"; exit 1; }
[[ "$value" != *$'\n'* ]] || { echo "valeur sur plusieurs lignes refusée"; exit 1; }
mkdir -p /etc/defi-bot
f=/etc/defi-bot/secrets.env
touch "$f"
tmp="$(mktemp)"
grep -v "^${name}=" "$f" > "$tmp" || true
printf '%s=%s\n' "$name" "$value" >> "$tmp"
install -o root -g defibot -m 640 "$tmp" "$f"
rm -f "$tmp"
systemctl restart defi-bot
echo "CLE_OK ${name} longueur=${#value}"
