"""Détenteurs d'un token Base lus directement sur la chaîne (RPC publics, sans clé).

Sert quand GoPlus n'a pas encore indexé les détenteurs d'un token récent.
On relit tous les événements Transfer depuis la création (les RPC publics gratuits
n'autorisent que ~8000 blocs en arrière, soit ~4 h : au-delà, le résultat est « incomplet »
et on renvoie None).
"""
import json, time, urllib.request

RPCS = ["https://base-rpc.publicnode.com", "https://mainnet.base.org"]
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO = "0x" + "0" * 40
DEAD = "0x000000000000000000000000000000000000dead"
CHUNK = 2000
MAX_CHUNKS = 4


def _rpc(method, params):
    last = None
    for url in RPCS:
        try:
            req = urllib.request.Request(url, data=json.dumps(
                {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
                headers={"Content-Type": "application/json", "User-Agent": "defi-bot-observer/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.loads(r.read().decode())
            if "error" in d:
                last = d["error"].get("message", "erreur RPC")
                continue
            return d["result"]
        except Exception as e:
            last = str(e)
    raise RuntimeError(last or "RPC indisponible")


def _pools_dexscreener(token):
    """Adresses des pools (les paires v2 / Aerodrome détiennent une grosse part de l'offre ; elles ne comptent pas comme détenteurs)."""
    try:
        req = urllib.request.Request("https://api.dexscreener.com/latest/dex/tokens/" + token,
                                     headers={"User-Agent": "defi-bot-observer/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read().decode())
        return {(p.get("pairAddress") or "").lower() for p in d.get("pairs") or []}
    except Exception:
        return set()


def holders_onchain(token, exclude=(), top_n=10):
    """Renvoie {"top10_pct", "holders", "blocs"} ou None si l'historique complet n'est pas lisible."""
    token = token.lower()
    bn = int(_rpc("eth_blockNumber", []), 16)
    supply = int(_rpc("eth_call", [{"to": token, "data": "0x18160ddd"}, "latest"]), 16)
    if supply <= 0:
        return None
    bal, mint_seen = {}, False
    hi = bn
    for _ in range(MAX_CHUNKS):
        lo = hi - CHUNK + 1
        logs = _rpc("eth_getLogs", [{"address": token, "topics": [TRANSFER],
                                      "fromBlock": hex(lo), "toBlock": hex(hi)}])
        for l in logs:
            if len(l.get("topics", [])) < 3:
                continue
            f, t = "0x" + l["topics"][1][26:], "0x" + l["topics"][2][26:]
            v = int(l["data"], 16)
            if f == ZERO:
                mint_seen = True
            bal[f] = bal.get(f, 0) - v
            bal[t] = bal.get(t, 0) + v
        if mint_seen:
            break
        hi = lo - 1
        time.sleep(0.3)
    if not mint_seen:
        return None
    skip = {ZERO, DEAD, token} | {a.lower() for a in exclude} | _pools_dexscreener(token)
    pos = sorted((v for a, v in bal.items() if v > 0 and a not in skip), reverse=True)
    return {"top10_pct": round(100 * sum(pos[:top_n]) / supply, 1), "holders": len(pos),
            "blocs": bn - hi}
