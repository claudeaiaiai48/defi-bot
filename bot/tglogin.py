"""Connexion initiale du compte Telegram du bot (interactive : numéro, code, mot de passe).
Lancée par scripts/telegram-login.sh. Les saisies se font dans ton terminal, pas via Claude."""
import os, sys
from telethon.sync import TelegramClient

DATA = os.environ.get("DEFIBOT_DATA", "/var/lib/defi-bot")
c = TelegramClient(os.path.join(DATA, "telegram"),
                   int(os.environ["TELEGRAM_API_ID"]), os.environ["TELEGRAM_API_HASH"])
c.start()          # demande le numéro, le code reçu dans Telegram, puis le mot de passe éventuel
me = c.get_me()
n = sum(1 for d in c.iter_dialogs() if d.is_group or d.is_channel)
print(f"CONNEXION_OK groupes_et_canaux={n}")
c.disconnect()
os.chmod(os.path.join(DATA, "telegram.session"), 0o600)
