"""Signature on-chain d'un lancement Base (lecture seule, noeud public, sans cle) : détecte le schéma
« créateur neuf + envoi de lots de tokens à ~9 portefeuilles + liquidité retirable » observé sur les rugs.
Collecte seule par défaut ; ne décide rien sans l'option filtre_rug d'un wallet de test."""
import json, time, urllib.request

RPC = "https://base-rpc.publicnode.com"      # le noeud mainnet.base.org limite trop vite les requêtes ; publicnode accepte eth_getLogs sur ~9000 blocs
POOLMANAGER = "0x498581ff718922c3f8e6a244956af099b2652b2b"
OPERATEURS = {"0x215199f691cd1cc86ce7bbf803a8b9d9e79eb2e7"}      # contrat d'achat/envoi en lot vu sur les rugs retracés
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO = "0x" + "0" * 40
_CACHE = {}


def _rpc(method, params, timeout=5):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(RPC, data=body, headers={"Content-Type": "application/json", "User-Agent": "defi-bot-observer/1.0"})
    j = json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8"))
    if "error" in j:
        raise RuntimeError(str(j["error"])[:80])
    return j["result"]


def signature(token, pair_created_ms=None):
    """Retourne {lot, operateur, createur, createur_nonce, createur_neuf, nb_transferts} ou {erreur} (erreur non mémorisée)."""
    token = (token or "").lower()
    if not token:
        return None
    if token in _CACHE:
        return _CACHE[token]
    out = {"lot": 0, "operateur": False, "nb_transferts": 0}
    try:
        latest = int(_rpc("eth_blockNumber", []), 16)
        age_s = max(60.0, time.time() - pair_created_ms / 1000.0) if pair_created_ms else 3600.0
        debut = max(0, latest - min(9000, int(age_s / 2) + 300))        # ~2 s par bloc ; plage limitée pour le noeud public
        logs = _rpc("eth_getLogs", [{"fromBlock": hex(debut), "toBlock": "latest", "address": token, "topics": [TRANSFER]}], timeout=8)
        envois, createur = {}, None
        for lg in logs:
            tp = lg.get("topics") or []
            if len(tp) < 3:
                continue
            exp, dest = "0x" + tp[1][-40:], "0x" + tp[2][-40:]
            if exp == ZERO:
                createur = createur or dest
                continue
            if exp == POOLMANAGER:
                continue
            envois.setdefault(exp, set()).add(dest)
            if exp in OPERATEURS:
                out["operateur"] = True
        out["nb_transferts"] = len(logs)
        out["lot"] = max([len(v) for v in envois.values()] or [0])
        if createur and createur != POOLMANAGER:
            nonce = int(_rpc("eth_getTransactionCount", [createur, "latest"]), 16)
            out.update({"createur": createur, "createur_nonce": nonce, "createur_neuf": nonce < 10})
    except Exception as e:
        return {"erreur": str(e)[:80]}
    _CACHE[token] = out
    return out
