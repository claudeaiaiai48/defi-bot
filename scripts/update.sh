#!/bin/bash
# Récupère les modifications du dépôt GitHub et réapplique si quelque chose a changé.
set -euo pipefail
cd /opt/defi-bot
old=$(git rev-parse HEAD)
git fetch -q origin main
git reset -q --hard origin/main
new=$(git rev-parse HEAD)
if [ "$old" != "$new" ]; then
  echo "mise à jour $old -> $new"
  bash /opt/defi-bot/scripts/apply.sh
fi
