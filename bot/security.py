"""Filtres de sécurité (document « Stratégie v1 », tous obligatoires avant un achat).

- Simulation de revente OK, pas de taxe cachée
- Top 10 des détenteurs (hors pool) sous le seuil
- Le créateur n'a pas tout vendu ; pas de lancement bundlé
- Liquidité minimale (déjà vérifiée par le signal)

Sources gratuites, sans clé :
- Solana : RugCheck (détenteurs, créateur, réseaux d'initiés, autorités, frais de transfert)
- Base : Honeypot.is (simulation achat/revente, taxes) + GoPlus (détenteurs, créateur)
Si une source ne répond pas, le token est refusé (prudence).
"""
import json, time, urllib.request

UA = {"User-Agent": "defi-bot-observer/1.0", "Accept": "application/json"}
_cache = {}


def _get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def check(chain, token, cfg):
    key = (chain, token)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < cfg.get("cache_minutes", 10) * 60:
        return hit[1]
    try:
        res = _solana(token, cfg) if chain == "solana" else _base(token, cfg)
    except Exception as e:
        res = {"ok": False, "raisons": [f"vérification impossible ({type(e).__name__})"], "details": {}}
    _cache[key] = (time.time(), res)
    if len(_cache) > 2000:
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:500]:
            del _cache[k]
    return res


def _solana(token, cfg):
    d = _get(f"https://api.rugcheck.xyz/v1/tokens/{token}/report")
    reasons, det = [], {}
    supply = float((d.get("token") or {}).get("supply") or 0)

    # 1. revente possible / pas de taxe cachée
    if d.get("rugged"):
        reasons.append("marqué rugged")
    if (d.get("token") or {}).get("freezeAuthority") or d.get("freezeAuthority"):
        reasons.append("autorité de gel active (revente bloquable)")
    if (d.get("token") or {}).get("mintAuthority") or d.get("mintAuthority"):
        reasons.append("autorité de mint active")
    fee = float((d.get("transferFee") or {}).get("pct") or 0)
    det["taxe_transfert_pct"] = fee
    if fee > cfg["max_tax_pct"]:
        reasons.append(f"taxe de transfert {fee} %")
    for r in d.get("risks") or []:
        if r.get("level") == "danger":
            reasons.append(f"RugCheck : {r.get('name')}")

    # 2. top 10 hors pools / AMM
    pools = {m.get("pubkey") for m in d.get("markets") or []}
    known = d.get("knownAccounts") or {}
    amm = {a for a, v in known.items() if (v or {}).get("type") in ("AMM", "LOCKER")}
    holders = [h for h in d.get("topHolders") or []
               if h.get("owner") not in pools and h.get("owner") not in amm
               and h.get("address") not in amm]
    top10 = sum(float(h.get("pct") or 0) for h in holders[:10])
    det["top10_hors_pool_pct"] = round(top10, 1)
    if top10 >= cfg["max_top10_pct"]:
        reasons.append(f"top 10 = {top10:.1f} %")

    # 3. créateur pas tout vendu
    det["createur_detient"] = float(d.get("creatorBalance") or 0) > 0
    if cfg["require_creator_holding"] and not det["createur_detient"]:
        reasons.append("le créateur a tout vendu")

    # 4. lancement bundlé : part détenue par les réseaux d'initiés
    insiders = sum(float(n.get("currentHolding") or 0) for n in d.get("insiderNetworks") or [])
    ins_pct = 100 * insiders / supply if supply else 0
    det["initiés_pct"] = round(ins_pct, 1)
    if ins_pct >= cfg["max_insiders_pct"]:
        reasons.append(f"bundle / initiés {ins_pct:.1f} %")
    return {"ok": not reasons, "raisons": reasons, "details": det}


def _base(token, cfg):
    reasons, det = [], {}
    # 1. simulation achat/revente
    hp = _get(f"https://api.honeypot.is/v2/IsHoneypot?address={token}&chainID=8453")
    sim = hp.get("simulationResult") or {}
    if not hp.get("simulationSuccess"):
        reasons.append("simulation de revente impossible")
    if (hp.get("honeypotResult") or {}).get("isHoneypot"):
        reasons.append("honeypot")
    tax = max(float(sim.get("buyTax") or 0), float(sim.get("sellTax") or 0),
              float(sim.get("transferTax") or 0))
    det["taxe_max_pct"] = tax
    if tax > cfg["max_tax_pct"]:
        reasons.append(f"taxe {tax} %")

    # 2 et 3. détenteurs et créateur
    gp = _get(f"https://api.gopluslabs.io/api/v1/token_security/8453?contract_addresses={token.lower()}")
    r = next(iter((gp.get("result") or {}).values()), None)
    if not r:
        reasons.append("GoPlus sans données")
    else:
        if r.get("is_honeypot") == "1" or r.get("cannot_sell_all") == "1":
            reasons.append("revente bloquée (GoPlus)")
        if r.get("is_mintable") == "1" or r.get("hidden_owner") == "1" or r.get("transfer_pausable") == "1":
            reasons.append("contrat à risque (mint / propriétaire caché / pause)")
        pools = {(x.get("pair") or "").lower() for x in r.get("dex") or []}
        holders = [h for h in r.get("holders") or []
                   if (h.get("address") or "").lower() not in pools and not h.get("is_locked")
                   and "pool" not in (h.get("tag") or "").lower()]
        top10 = 100 * sum(float(h.get("percent") or 0) for h in holders[:10])
        det["top10_hors_pool_pct"] = round(top10, 1)
        if top10 >= cfg["max_top10_pct"]:
            reasons.append(f"top 10 = {top10:.1f} %")
        det["createur_detient"] = float(r.get("creator_percent") or 0) > 0
        if cfg["require_creator_holding"] and not det["createur_detient"]:
            reasons.append("le créateur a tout vendu")
    det["bundle"] = "non vérifiable sur Base (pas de source gratuite)"
    return {"ok": not reasons, "raisons": reasons, "details": det}
