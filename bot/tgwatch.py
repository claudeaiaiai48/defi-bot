"""Veille Telegram en LECTURE SEULE (groupes/canaux dont le compte du bot est membre).

Le bot ne poste rien et ne rejoint rien. Il ne garde PAS le texte des messages :
seulement, pour chaque jeton cité ($SYMBOLE ou adresse), l'heure, l'auteur et le groupe.
Inactif tant que la clé API, le module telethon et la session ne sont pas prêts.
Session : /var/lib/defi-bot/telegram.session (créée par scripts/telegram-login.sh).
"""
import asyncio, os, re, threading, time
from collections import defaultdict, deque

DATA = os.environ.get("DEFIBOT_DATA", "/var/lib/defi-bot")
SESSION = os.path.join(DATA, "telegram")
STATE = {"etat": "inactif", "groupes": 0, "messages_vus": 0}
_mentions = defaultdict(lambda: deque(maxlen=2000))   # clé -> (t, auteur, groupe)
_lock = threading.Lock()

RE_SYMBOL = re.compile(r"(?<![A-Za-z0-9_])\$([A-Za-z][A-Za-z0-9]{1,14})\b")
RE_SOL = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])")
RE_EVM = re.compile(r"\b0x[a-fA-F0-9]{40}\b")


def extract_keys(text):
    """Jetons cités dans un message : symboles ($ABC) et adresses Solana / EVM."""
    keys = {"sym:" + s.upper() for s in RE_SYMBOL.findall(text or "")}
    keys |= {"addr:" + a for a in RE_SOL.findall(text or "")}
    keys |= {"addr:" + a.lower() for a in RE_EVM.findall(text or "")}
    return keys


def record(keys, author, group, t=None):
    t = t or time.time()
    with _lock:
        for k in keys:
            _mentions[k].append((t, author, group))
        STATE["messages_vus"] += 1


def mentions(symbol, token):
    """Mentions sur 1 h / 15 min d'un jeton (par adresse ou par $symbole)."""
    now = time.time()
    keys = ["addr:" + (token.lower() if token.startswith("0x") else token)]
    if symbol and len(symbol) >= 3:
        keys.append("sym:" + symbol.upper())
    rows = []
    with _lock:
        for k in keys:
            rows += [(r, k.startswith("addr:")) for r in _mentions.get(k, ())]
    h1 = [(r, a) for r, a in rows if now - r[0] <= 3600]
    m15 = [r for r, _ in h1 if now - r[0] <= 900]
    return {"mentions_1h": len(h1), "mentions_15min": len(m15),
            "mentions_adresse_1h": sum(1 for _, a in h1 if a),
            "auteurs_distincts": len({r[1] for r, _ in h1}),
            "groupes_distincts": len({r[2] for r, _ in h1})}


async def _run(api_id, api_hash):
    from telethon import TelegramClient, events
    client = TelegramClient(SESSION, api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        STATE["etat"] = "session non connectée (lancer connexion-telegram.bat)"
        return
    STATE["etat"] = "actif"
    n = 0
    async for d in client.iter_dialogs():
        if d.is_group or d.is_channel:
            n += 1
    STATE["groupes"] = n

    @client.on(events.NewMessage(incoming=True))
    async def _on(ev):
        if not (ev.is_group or ev.is_channel):
            return
        keys = extract_keys(ev.raw_text or "")
        if keys:
            record(keys, ev.sender_id, ev.chat_id)
        else:
            STATE["messages_vus"] += 1

    await client.run_until_disconnected()


def start():
    """Lance la veille en arrière-plan ; ne fait rien si les prérequis manquent."""
    api_id, api_hash = os.environ.get("TELEGRAM_API_ID"), os.environ.get("TELEGRAM_API_HASH")
    if not (api_id and api_hash):
        STATE["etat"] = "clés absentes"
        return
    if not os.path.exists(SESSION + ".session"):
        STATE["etat"] = "session non connectée (lancer connexion-telegram.bat)"
        return
    try:
        import telethon  # noqa: F401
        api_id = int(api_id)
    except Exception as e:
        STATE["etat"] = f"prérequis manquant ({type(e).__name__})"
        return

    def loop():
        while True:
            try:
                asyncio.run(_run(api_id, api_hash))
            except Exception as e:
                STATE["etat"] = f"erreur {type(e).__name__}, nouvel essai"
            time.sleep(30)

    threading.Thread(target=loop, daemon=True, name="tgwatch").start()
