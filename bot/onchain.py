"""Contrôles on-chain Uniswap v4 sur Base (lecture seule, RPC publics) : activité d'un pool, swaps à sens unique
(signe de honeypot), arrêt des swaps (pool mort) et retrait de liquidité. Utilisé seulement par les wallets de test
qui ont l'option "onchain" ; en cas d'erreur RPC le contrôle est simplement ignoré (rien n'est mis en cache)."""
import json, time, urllib.request

RPC = "https://base-rpc.publicnode.com"
POOLMANAGER = "0x498581ff718922c3f8e6a244956af099b2652b2b"
SWAP = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
MODLIQ = "0xf208f4912782fd25c7f114ca3723a2d5dd6f3bcc3ac8db5af63baa85f711d5ec"
BLOCS = 3600          # fenêtre d'analyse : ~2 h (blocs de 2 s sur Base)
_CACHE, _BLOC = {}, [0, 0]


def _rpc(method, params, timeout=6):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(RPC, body, {"Content-Type": "application/json", "User-Agent": "defi-bot"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        j = json.load(r)
    if "error" in j:
        raise RuntimeError(str(j["error"].get("message")))
    return j["result"]


def _bloc():
    if time.time() - _BLOC[1] > 10:
        _BLOC[0], _BLOC[1] = int(_rpc("eth_blockNumber", []), 16), time.time()
    return _BLOC[0]


def _logs(topic, pool_id, frm):
    return _rpc("eth_getLogs", [{"address": POOLMANAGER, "fromBlock": hex(frm), "toBlock": "latest",
                                 "topics": [topic, pool_id]}], timeout=10)


def _mots(data):
    d = data[2:]
    return [d[i:i + 64] for i in range(0, len(d), 64)]


def _signe(h):
    v = int(h, 16)
    return v - (1 << 256) if v >= (1 << 255) else v


def etat(pool_id, blocs=BLOCS):
    """Etat d'un pool v4 sur la fenêtre récente : n swaps, minorité (swaps du sens le moins fréquent), minutes depuis
    le dernier swap, part de liquidité retirée (retrait net / liquidité active maximale vue)."""
    c = _CACHE.get(pool_id)
    if c and time.time() - c[0] < 90:
        return c[1]
    bn = _bloc()
    frm = bn - blocs
    sw, ml = _logs(SWAP, pool_id, frm), _logs(MODLIQ, pool_id, frm)
    a = b = liq_max = 0
    dernier = None
    for l in sw:
        w = _mots(l["data"])
        s0 = _signe(w[0])
        a, b = a + (s0 > 0), b + (s0 < 0)
        liq_max = max(liq_max, int(w[3], 16))
        dernier = max(dernier or 0, int(l["blockNumber"], 16))
    ajout = retrait = 0
    for l in ml:
        d = _signe(_mots(l["data"])[2])
        if d > 0:
            ajout += d
        else:
            retrait -= d
    net = max(0, retrait - ajout)
    res = {"n": len(sw), "minorite": min(a, b),
           "dernier_swap_min": round((bn - dernier) * 2 / 60, 1) if dernier else round(blocs * 2 / 60, 1),
           "retrait_ratio": round(net / liq_max, 3) if liq_max else None}
    _CACHE[pool_id] = (time.time(), res)
    return res
