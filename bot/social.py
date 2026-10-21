"""Signaux sociaux et smart money (Stratégie v1 : « le social repère, le on-chain confirme »).

Pour l'instant ces données sont ENREGISTRÉES avec chaque signal, sans bloquer ni
déclencher d'achat : les seuils seront calibrés sur les semaines d'observation.

- Farcaster (Neynar) : mentions du token ($SYMBOLE ou adresse) sur 1 h et 15 min,
  auteurs distincts, plus gros compte (abonnés), engagement.
- Smart money (Helius, Solana) : quels wallets de la liste config["smart_money"]
  détiennent ce token au moment du signal (1 crédit par wallet, cache 10 min).
Les clés API viennent de /etc/defi-bot/secrets.env (jamais du dépôt).
"""
import json, os, time, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta

UA = {"User-Agent": "defi-bot-observer/1.0", "Accept": "application/json"}
STATUS = {}          # état de chaque source, publié dans status.json
_wallet_cache = {}   # wallet -> (t, swaps)


def _shape(v):
    """Forme d'une clé SANS rien révéler de sa valeur (aide au diagnostic)."""
    if not v:
        return False
    import re
    return {"longueur": len(v),
            "format_uuid": bool(re.fullmatch(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", v)),
            "caracteres_bizarres": bool(re.search(r"[^A-Za-z0-9_\-]", v)),
            "ressemble_cle_ssh": v.startswith("ssh-")}


def keys_present():
    return {k: _shape(os.environ.get(k)) for k in
            ("NEYNAR_API_KEY", "HELIUS_API_KEY", "REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET")}


def _get(url, headers=None):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def _err(e):
    code = getattr(e, "code", None)
    body = ""
    try:
        body = e.read().decode("utf-8", "replace")[:160] if hasattr(e, "read") else ""
    except Exception:
        pass
    return f"erreur {code or type(e).__name__} {body}".strip()


def _ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0


# ---------- Farcaster ----------
def farcaster(symbol, token):
    key = os.environ.get("NEYNAR_API_KEY")
    if not key:
        STATUS["neynar"] = "clé absente"
        return None
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
    terms = [f'"{token}"']
    if symbol and len(symbol) >= 3:
        terms.append(f'"${symbol}"')
    q = f"({' | '.join(terms)}) after:{since}"
    url = ("https://api.neynar.com/v2/farcaster/cast/search/?"
           + urllib.parse.urlencode({"q": q, "limit": 100, "sort_type": "desc_chron"}))
    try:
        casts = (_get(url, {"x-api-key": key}).get("result") or {}).get("casts") or []
        STATUS["neynar"] = "ok"
    except Exception as e:
        STATUS["neynar"] = _err(e)
        return None
    now = time.time()
    recent = [c for c in casts if now - _ts(c.get("timestamp", "")) <= 3600]
    last15 = [c for c in recent if now - _ts(c.get("timestamp", "")) <= 900]
    authors = {(c.get("author") or {}).get("fid") for c in recent}
    followers = [(c.get("author") or {}).get("follower_count") or 0 for c in recent]
    engagement = sum(((c.get("reactions") or {}).get("likes_count") or 0)
                     + ((c.get("reactions") or {}).get("recasts_count") or 0) for c in recent)
    return {"mentions_1h": len(recent), "mentions_15min": len(last15),
            "auteurs_distincts": len(authors - {None}),
            "plus_gros_compte_abonnes": max(followers or [0]), "engagement": engagement}


# ---------- Smart money (Solana) ----------
def _holds(wallet, mint, key):
    """1 crédit Helius : le wallet détient-il ce token ?"""
    hit = _wallet_cache.get((wallet, mint))
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
                       "params": [wallet, {"mint": mint}, {"encoding": "jsonParsed"}]}).encode()
    req = urllib.request.Request(f"https://mainnet.helius-rpc.com/?api-key={urllib.parse.quote(key)}",
                                 data=body, headers={**UA, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        res = json.loads(r.read().decode("utf-8"))
    if "error" in res:
        raise RuntimeError(str(res["error"])[:100])
    held = any(float((((a.get("account") or {}).get("data") or {}).get("parsed") or {})
                     .get("info", {}).get("tokenAmount", {}).get("uiAmount") or 0) > 0
               for a in (res.get("result") or {}).get("value") or [])
    _wallet_cache[(wallet, mint)] = (time.time(), held)
    return held


def smart_money(token, wallets):
    key = os.environ.get("HELIUS_API_KEY")
    if not key:
        STATUS["helius"] = "clé absente"
        return None
    if not wallets:
        STATUS["helius"] = "ok (liste de wallets vide)"
        return {"detenteurs": [], "liste_vide": True}
    holders = []
    try:
        for w in wallets:
            addr = w["address"] if isinstance(w, dict) else w
            if _holds(addr, token, key):
                holders.append({"wallet": addr[:6] + "…",
                                "source": w.get("source") if isinstance(w, dict) else None})
        STATUS["helius"] = "ok"
    except Exception as e:
        STATUS["helius"] = _err(e)
        return None
    return {"detenteurs": holders, "nb": len(holders), "suivis": len(wallets)}


_selftest_done = {}


def selftest():
    """Vérifie une fois par heure que chaque clé est acceptée (requêtes très légères)."""
    now = time.time()
    if now - _selftest_done.get("t", 0) < 3600:
        return
    _selftest_done["t"] = now
    k = os.environ.get("NEYNAR_API_KEY")
    if k:
        try:
            _get("https://api.neynar.com/v2/farcaster/user/bulk/?fids=3", {"x-api-key": k})
            STATUS["neynar_cle"] = "valide"
        except Exception as e:
            STATUS["neynar_cle"] = _err(e)
    k = os.environ.get("HELIUS_API_KEY")
    if k:
        try:
            req = urllib.request.Request(
                f"https://mainnet.helius-rpc.com/?api-key={urllib.parse.quote(k)}",
                data=b'{"jsonrpc":"2.0","id":1,"method":"getHealth"}',
                headers={**UA, "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                STATUS["helius_cle"] = "valide" if b"ok" in r.read() else "réponse inattendue"
        except Exception as e:
            STATUS["helius_cle"] = _err(e)


def enrich(chain, row, cfg):
    """Ajoute les données sociales à un signal (sans décider de l'achat)."""
    s = cfg.get("social", {})
    out = {}
    if s.get("farcaster"):
        out["farcaster"] = farcaster(row.get("symbol"), row.get("token"))
    if chain == "solana" and s.get("smart_money"):
        out["smart_money"] = smart_money(row.get("token"), cfg.get("smart_money", []))
    return out
