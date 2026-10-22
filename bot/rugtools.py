"""Outils d'analyse anti-rug (Solana), utilisés en COLLECTE D'INFOS uniquement : ils n'influencent aucune décision du bot.

Chaque outil est une fonction autonome qui renvoie des mesures (jamais de décision) :
  1. creator_history()   historique du créateur : combien de tokens lancés, combien sont morts
  2. wallet_clusters()   wallets des gros détenteurs financés par la même source (réseau lié)
  3. market_pressure()   volume / liquidité, acheteurs / vendeurs, accélération (données DexScreener)
  4. late_entry()        âge du token, hausse déjà faite, capitalisation / liquidité
  evaluate()             agrège les 4 mesures + des drapeaux indicatifs (seuils provisoires, `cfg`)

Les outils 1 et 2 appellent l'API Helius (clé HELIUS_API_KEY) ; les formats de réponse Helius sont à valider
avec la vraie clé avant tout usage (le code est tolérant : en cas de doute il renvoie « inconnu », jamais une erreur).
Les outils 3 et 4 sont des fonctions pures sur la paire DexScreener (format vérifié).
"""
import json, os, time, urllib.request, urllib.error, urllib.parse

UA = {"User-Agent": "defi-bot-rugtools", "Accept": "application/json"}
DEX = "https://api.dexscreener.com"
HELIUS_REST = "https://api.helius.xyz"

# seuils indicatifs, à régler sur les données réelles (voir scripts/analyse-rugs.py) ; ne décident de rien
DEFAULTS = {
    "creator_dead_share_max": 0.6,     # part de tokens précédents du créateur « morts » au-delà de laquelle on signale
    "creator_min_tokens": 3,           # nombre minimum de tokens précédents pour que la part soit significative
    "cluster_pct_max": 15.0,           # % de l'offre détenu par un même groupe de wallets liés
    "vol_liq_h1_max": 30.0,            # volume 1 h / liquidité (au-dessus : volume suspect)
    "buy_sell_h1_max": 4.0,            # acheteurs / vendeurs 1 h (au-dessus : pression d'achat artificielle ?)
    "age_min_max": 180,                # minutes depuis la création de la paire (au-delà : plus tout jeune)
    "pump_h1_max_pct": 300.0,          # hausse 1 h déjà réalisée (au-delà : entrée tardive)
    "mcap_liq_max": 15.0,              # capitalisation / liquidité
    "max_wallets": 10,                 # détenteurs examinés pour les wallets liés (coût Helius)
}


def _get_json(url, timeout=20):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _num(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- 3. pression du marché (pur)
def market_pressure(pair):
    """Mesures de volume et d'équilibre achats/ventes sur une paire DexScreener."""
    if not pair:
        return {"disponible": False}
    liq = _num((pair.get("liquidity") or {}).get("usd"))
    vol = pair.get("volume") or {}
    tx = pair.get("txns") or {}

    def bs(w):
        b, s = _num((tx.get(w) or {}).get("buys")), _num((tx.get(w) or {}).get("sells"))
        return b, s, (round(b / s, 2) if s else None)

    b1, s1, r1 = bs("h1")
    b5, s5, r5 = bs("m5")
    v1, v5 = _num(vol.get("h1")), _num(vol.get("m5"))
    n1 = b1 + s1
    return {
        "disponible": True,
        "vol_liq_h1": round(v1 / liq, 2) if liq else None,
        "achats_h1": int(b1), "ventes_h1": int(s1), "achats_sur_ventes_h1": r1,
        "achats_sur_ventes_m5": r5,
        "ticket_moyen_h1_usd": round(v1 / n1, 1) if n1 else None,
        # >1 : le rythme des 5 dernières minutes dépasse la moyenne de l'heure (accélération)
        "acceleration_m5_sur_h1": round((v5 * 12) / v1, 2) if v1 else None,
    }


# ---------------------------------------------------------------- 4. entrée tardive (pur)
def late_entry(pair, now=None):
    """Âge du token, hausse déjà réalisée et valorisation relative à la liquidité."""
    if not pair:
        return {"disponible": False}
    now = now or time.time()
    created = _num(pair.get("pairCreatedAt")) / 1000.0
    pc = pair.get("priceChange") or {}
    liq = _num((pair.get("liquidity") or {}).get("usd"))
    mcap = _num(pair.get("marketCap") or pair.get("fdv"))
    return {
        "disponible": True,
        "age_min": round((now - created) / 60, 1) if created else None,
        "hausse_m5_pct": pc.get("m5"), "hausse_h1_pct": pc.get("h1"),
        "hausse_h6_pct": pc.get("h6"), "hausse_h24_pct": pc.get("h24"),
        "mcap_usd": round(mcap) if mcap else None,
        "mcap_sur_liquidite": round(mcap / liq, 1) if liq and mcap else None,
    }


# ---------------------------------------------------------------- 1. historique du créateur
def _helius_key(key=None):
    return key or os.environ.get("HELIUS_API_KEY", "")


def creator_mints(creator, key=None, pages=2, per_page=100):
    """Tokens pump.fun déjà lancés par ce wallet (Helius, transactions enrichies). Renvoie None si indisponible."""
    key = _helius_key(key)
    if not creator or not key:
        return None
    mints, before = set(), None
    try:
        for _ in range(pages):
            q = {"api-key": key, "limit": per_page}
            if before:
                q["before"] = before
            txs = _get_json(f"{HELIUS_REST}/v0/addresses/{creator}/transactions?" + urllib.parse.urlencode(q))
            if not txs:
                break
            for tx in txs:
                src = (tx.get("source") or "").upper()
                typ = (tx.get("type") or "").upper()
                if "PUMP" in src and typ in ("TOKEN_MINT", "CREATE", "CREATE_POOL", "UNKNOWN") \
                        and tx.get("feePayer") == creator:
                    for tt in tx.get("tokenTransfers") or []:
                        m = tt.get("mint")
                        if m and m.endswith("pump"):
                            mints.add(m)
            before = txs[-1].get("signature")
            if len(txs) < per_page:
                break
    except Exception:
        return None
    return sorted(mints)


def _is_dead(pair, min_liq=1000.0):
    if not pair:
        return True
    liq = _num((pair.get("liquidity") or {}).get("usd"))
    h24 = _num((pair.get("priceChange") or {}).get("h24"), 0.0)
    return liq < min_liq or h24 <= -90


def creator_history(creator, current_mint=None, key=None, fetch_pairs=None):
    """Combien de tokens précédents du créateur sont aujourd'hui morts (liquidité < 1 000 $ ou -90 % sur 24 h).
    `fetch_pairs(chain, [mints]) -> {mint: pair}` est injectable pour les tests."""
    mints = creator_mints(creator, key)
    if mints is None:
        return {"disponible": False, "raison": "Helius indisponible ou clé absente"}
    prev = [m for m in mints if m != current_mint]
    if not prev:
        return {"disponible": True, "tokens_precedents": 0, "morts": 0, "part_morts": None}
    fetch_pairs = fetch_pairs or dex_pairs
    pairs = {}
    for i in range(0, len(prev), 30):
        pairs.update(fetch_pairs("solana", prev[i:i + 30]))
    dead = sum(1 for m in prev if _is_dead(pairs.get(m)))
    return {"disponible": True, "tokens_precedents": len(prev), "morts": dead,
            "part_morts": round(dead / len(prev), 2)}


def dex_pairs(chain, mints):
    """Meilleure paire (plus grosse liquidité) par token, via DexScreener."""
    out = {}
    try:
        data = _get_json(f"{DEX}/tokens/v1/{chain}/{','.join(mints)}")
    except Exception:
        return out
    for p in data or []:
        a = (p.get("baseToken") or {}).get("address")
        if a and _num((p.get("liquidity") or {}).get("usd")) > _num(((out.get(a) or {}).get("liquidity") or {}).get("usd")):
            out[a] = p
    return out


# ---------------------------------------------------------------- 2. wallets liés
def funded_by(wallet, key=None):
    """Wallet qui a financé `wallet` pour la première fois (API Helius Wallet). None si inconnu."""
    key = _helius_key(key)
    if not wallet or not key:
        return None
    try:
        d = _get_json(f"{HELIUS_REST}/v1/wallet/{wallet}/funded-by?api-key={urllib.parse.quote(key)}")
    except urllib.error.HTTPError:
        return None
    except Exception:
        return None
    f = d.get("funder") if isinstance(d, dict) else None
    if not f:
        return None
    kind = (d.get("funderType") or d.get("funder_type") or "").lower()
    return {"funder": f, "exchange": "exchange" in kind}


def wallet_clusters(holders, key=None, max_wallets=15, funder_lookup=None, pause=0.2):
    """Regroupe les gros détenteurs par wallet financeur commun.
    `holders` : liste {owner, pct} (RugCheck topHolders, hors AMM/LP). Les financeurs de type « exchange » sont ignorés
    (des milliers de wallets légitimes y sont financés)."""
    funder_lookup = funder_lookup or (lambda w: funded_by(w, key))
    hs = [h for h in holders if h.get("owner") and not h.get("is_lp")][:max_wallets]
    if not hs:
        return {"disponible": False, "raison": "aucun détenteur"}
    groups, inconnus = {}, 0
    for h in hs:
        f = funder_lookup(h["owner"])
        if f is None:
            inconnus += 1
        elif not f.get("exchange"):
            groups.setdefault(f["funder"], []).append(h)
        if pause:
            time.sleep(pause)
    if inconnus == len(hs):
        return {"disponible": False, "raison": "financeurs introuvables"}
    best = max(groups.values(), key=lambda g: sum(_num(x.get("pct")) for x in g), default=[])
    multi = [g for g in groups.values() if len(g) >= 2]
    return {"disponible": True, "wallets_analyses": len(hs), "financeurs_inconnus": inconnus,
            "groupes_lies": len(multi),
            "plus_gros_groupe_wallets": len(best) if len(best) >= 2 else 0,
            "plus_gros_groupe_pct_offre": round(sum(_num(x.get("pct")) for x in best), 2) if len(best) >= 2 else 0.0}


def holders_from_rugcheck(report):
    """Extrait [{owner, pct, is_lp}] d'un rapport RugCheck (marché AMM exclu via knownAccounts)."""
    known = (report or {}).get("knownAccounts") or {}
    out = []
    for h in (report or {}).get("topHolders") or []:
        owner = h.get("owner") or h.get("address")
        kind = (known.get(owner) or {}).get("type")
        out.append({"owner": owner, "pct": h.get("pct"), "is_lp": kind in ("AMM", "LOCKER")})
    return out


# ---------------------------------------------------------------- agrégat
def evaluate(pair, report=None, cfg=None, key=None, with_helius=True, now=None):
    """Mesures des 4 outils + drapeaux indicatifs. Aucun effet sur le bot."""
    c = {**DEFAULTS, **(cfg or {})}
    mp, le = market_pressure(pair), late_entry(pair, now)
    res = {"marche": mp, "tardif": le, "createur": {"disponible": False}, "reseau": {"disponible": False}}
    if with_helius and report:
        res["createur"] = creator_history(report.get("creator"), report.get("mint"), key)
        res["reseau"] = wallet_clusters(holders_from_rugcheck(report), key, max_wallets=c["max_wallets"])
    flags = []
    cr, rs = res["createur"], res["reseau"]
    if cr.get("disponible") and (cr.get("tokens_precedents") or 0) >= c["creator_min_tokens"] \
            and (cr.get("part_morts") or 0) >= c["creator_dead_share_max"]:
        flags.append(f"créateur : {cr['morts']}/{cr['tokens_precedents']} tokens précédents morts")
    if rs.get("disponible") and rs.get("plus_gros_groupe_pct_offre", 0) >= c["cluster_pct_max"]:
        flags.append(f"{rs['plus_gros_groupe_wallets']} wallets liés détiennent {rs['plus_gros_groupe_pct_offre']} % de l'offre")
    if mp.get("vol_liq_h1") and mp["vol_liq_h1"] >= c["vol_liq_h1_max"]:
        flags.append(f"volume 1 h = {mp['vol_liq_h1']}× la liquidité")
    if mp.get("achats_sur_ventes_h1") and mp["achats_sur_ventes_h1"] >= c["buy_sell_h1_max"]:
        flags.append(f"achats/ventes 1 h = {mp['achats_sur_ventes_h1']}")
    if le.get("age_min") is not None and le["age_min"] >= c["age_min_max"]:
        flags.append(f"token déjà âgé de {le['age_min']} min")
    if _num(le.get("hausse_h1_pct")) >= c["pump_h1_max_pct"]:
        flags.append(f"déjà +{le['hausse_h1_pct']} % sur 1 h")
    if _num(le.get("mcap_sur_liquidite")) >= c["mcap_liq_max"]:
        flags.append(f"capitalisation = {le['mcap_sur_liquidite']}× la liquidité")
    res["drapeaux"] = flags
    return res
