# defi-bot

Bot DeFi (Solana + Base) — **mode observation** : aucun achat, aucune vente, aucun wallet ni clé privée sur le serveur.

- `config.json` : réglages (chaînes, fréquence, filtres de signal). Toute modification poussée ici est appliquée sur le serveur en moins de 2 minutes.
- `bot/observer.py` : surveille les nouveaux tokens via l'API publique DexScreener et enregistre volume, liquidité et transactions.
- `scripts/` : installation et mise à jour automatique (systemd).

Statut public (lecture seule) : `http://<ip-du-serveur>:8080/status.json` et `/signals.json`.

**Ne jamais mettre de clé privée, seed ou token dans ce dépôt : il est public.**
