"""Backtest du copy trading : « acheter quand un wallet smart money achète » (Solana).

1. Historique des swaps des wallets suivis (config.json > smart_money) via l'API Helius (transactions enrichies).
2. Pour chaque token acheté : prix minute par minute après l'achat via GeckoTerminal (OHLCV), mis en cache.
3. Simulation : on achète X minutes après le wallet, au pire prix de la minute (prudent), frais + glissement +
   impact selon la liquidité du pool ; plusieurs règles de sortie, dont « revendre quand le wallet revend ».
Aucun effet sur le bot ni sur les wallets : lit des API publiques, écrit un cache et public/copytrade.json.
"""
import json, os, statistics, sys, time, urllib.parse, urllib.request, urllib.error
from datetime import datetime, timezone

DATA = os.environ.get("DEFIBOT_DATA", "/var/lib/defi-bot")
PUBLIC = os.path.join(DATA, "public")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(DATA, "copytrade_cache")
UA = {"User-Agent": "defi-bot-copytrade/1.0", "Accept": "application/json"}
BASES = {"So11111111111111111111111111111111111111112", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
         "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY9NNHWxTW1Rgtyz"}
JOURS = float(os.environ.get("COPY_JOURS", "3"))
MAX_ACHATS = int(os.environ.get("COPY_MAX_ACHATS", "500"))   # limite GeckoTerminal (~15 appels/min)
PAGES_MAX = int(os.environ.get("COPY_PAGES", "15"))
HORIZON_MIN = 360
FRAIS = (1.75 + 0.5) / 100      # frais + glissement par côté (comme le paper trading Solana)
EXTRA = 2.5 / 100               # latence / mouvement entre observation et exécution
MISE = 30.0
PROG = {}


def progres(**kw):
    PROG.update(kw, maj=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    try:
        ecrire(os.path.join(PUBLIC, "copytrade_progress.json"), PROG)
    except OSError:
        pass


def ecrire(chemin, obj):
    tmp = chemin + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, chemin)


def get_json(url, pause=0.0, essais=5):
    for e in range(essais):
        if pause:
            time.sleep(pause)
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as ex:
            if ex.code in (429, 500, 502, 503, 504) and e < essais - 1:
                time.sleep(5 * (e + 1))
                continue
            if ex.code == 404:
                return None
            raise
        except (urllib.error.URLError, TimeoutError):
            if e < essais - 1:
                time.sleep(5 * (e + 1))
                continue
            raise
    return None


# ---------------------------------------------------------------- 1. swaps des wallets (Helius)
def mouvements(tx, wallet):
    """Variation nette par token (hors SOL/USDC/USDT) et SOL dépensé/reçu pour ce wallet dans une transaction."""
    net = {}
    for t in tx.get("tokenTransfers") or []:
        m, q = t.get("mint"), float(t.get("tokenAmount") or 0)
        if not m or m in BASES or not q:
            continue
        if t.get("toUserAccount") == wallet:
            net[m] = net.get(m, 0) + q
        if t.get("fromUserAccount") == wallet:
            net[m] = net.get(m, 0) - q
    sol = 0.0
    for a in tx.get("accountData") or []:
        if a.get("account") == wallet:
            sol += float(a.get("nativeBalanceChange") or 0) / 1e9
    for t in tx.get("tokenTransfers") or []:          # wSOL / USDC éventuellement utilisés à la place du SOL natif
        if t.get("mint") == "So11111111111111111111111111111111111111112":
            q = float(t.get("tokenAmount") or 0)
            if t.get("fromUserAccount") == wallet:
                sol -= q
            if t.get("toUserAccount") == wallet:
                sol += q
    return net, sol


def swaps_wallet(wallet, key, depuis):
    chemin = os.path.join(CACHE, f"swaps_{wallet}.json")
    try:
        with open(chemin, encoding="utf-8") as f:
            cache = json.load(f)
        if time.time() - cache.get("t", 0) < 6 * 3600:
            return cache["evts"]
    except (OSError, ValueError):
        pass
    evts, avant = [], None
    for page in range(PAGES_MAX):
        url = (f"https://api.helius.xyz/v0/addresses/{wallet}/transactions?api-key={urllib.parse.quote(key)}"
               f"&type=SWAP&limit=100" + (f"&before={avant}" if avant else ""))
        lot = get_json(url, pause=0.3) or []
        if not lot:
            break
        for tx in lot:
            ts = tx.get("timestamp") or 0
            if ts < depuis:
                continue
            net, sol = mouvements(tx, wallet)
            for m, q in net.items():
                if q > 0:
                    evts.append({"t": ts, "mint": m, "sens": "achat", "sol": round(max(-sol, 0), 4)})
                elif q < 0:
                    evts.append({"t": ts, "mint": m, "sens": "vente", "sol": round(max(sol, 0), 4)})
        avant = lot[-1].get("signature")
        if (lot[-1].get("timestamp") or 0) < depuis:
            break
    ecrire(chemin, {"t": time.time(), "evts": evts})
    return evts


# ---------------------------------------------------------------- 2. prix (GeckoTerminal)
_POOLS = {}


def pools_token(mint):
    if mint in _POOLS:
        return _POOLS[mint]
    chemin = os.path.join(CACHE, f"pools_{mint}.json")
    try:
        with open(chemin, encoding="utf-8") as f:
            _POOLS[mint] = json.load(f)
            return _POOLS[mint]
    except (OSError, ValueError):
        pass
    d = get_json(f"https://api.geckoterminal.com/api/v2/networks/solana/tokens/{mint}/pools?page=1", pause=4)
    out = []
    for p in (d or {}).get("data") or []:
        a = p.get("attributes") or {}
        cree = a.get("pool_created_at")
        try:
            tc = datetime.fromisoformat(cree.replace("Z", "+00:00")).timestamp() if cree else 0
        except ValueError:
            tc = 0
        out.append({"pool": a.get("address"), "liq": float(a.get("reserve_in_usd") or 0), "cree_t": tc,
                    "dex": ((p.get("relationships") or {}).get("dex") or {}).get("data", {}).get("id")})
    ecrire(chemin, out)
    _POOLS[mint] = out
    return out


def pool_principal(mint, t0):
    """Pool le plus liquide qui existait déjà au moment de l'achat (sinon le plus liquide tout court)."""
    pools = [p for p in pools_token(mint) if p.get("pool")]
    if not pools:
        return None
    avant = [p for p in pools if p["cree_t"] and p["cree_t"] <= t0]
    return max(avant or pools, key=lambda p: p["liq"])


def bougies(pool, t0):
    """Bougies 1 minute de t0 - 5 min à t0 + HORIZON_MIN (au plus 1000 par appel)."""
    fin = int(t0 + HORIZON_MIN * 60)
    d = get_json(f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/ohlcv/minute"
                 f"?aggregate=1&limit=1000&before_timestamp={fin}&currency=usd", pause=4)
    lst = (((d or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    out = sorted([(int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4])) for c in lst
                  if int(c[0]) >= t0 - 300], key=lambda c: c[0])
    return out


def prix_token(mint, t0):
    chemin = os.path.join(CACHE, f"prix_{mint}_{int(t0) // 3600}.json")
    try:
        with open(chemin, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        pass
    info = pool_principal(mint, t0)
    res = {"info": info, "bougies": bougies(info["pool"], t0) if info and info.get("pool") else []}
    ecrire(chemin, res)
    return res


# ---------------------------------------------------------------- 3. simulation
def simuler(b, t_achat, regle, liq, ventes_wallet):
    """b : bougies (t, o, h, l, c) ; on entre à la première bougie qui commence après t_achat + retard."""
    t_entree = t_achat + regle["retard_s"]
    i0 = next((i for i, c in enumerate(b) if c[0] + 60 > t_entree), None)
    if i0 is None or b[i0][0] - t_entree > 600:
        return None                                   # pas de prix dans les 10 min : trade impossible à simuler
    c0 = b[i0]
    px = max(c0[1], c0[4]) if c0[0] < t_entree else c0[1]     # prudent : pire prix connu de la minute
    imp = EXTRA + (MISE / (liq / 2) if liq else 0.05)
    entree = px * (1 + imp)
    qty = MISE * (1 - FRAIS) / entree
    stop = px * (1 + regle["stop_pct"] / 100)
    tp = px * (1 + regle["tp_pct"] / 100) if regle.get("tp_pct") else None
    haut, raison, sortie = px, "fin (6 h)", None
    vente_w = min((v for v in ventes_wallet if v > t_achat), default=None) if regle.get("suivre_vente") else None
    for c in b[i0 + 1:]:
        t, o, h, l, cl = c
        if t - c0[0] > regle["duree_max_min"] * 60:
            sortie, raison = o, "temps"
            break
        if vente_w and t >= vente_w + regle["retard_s"]:
            sortie, raison = o, "le wallet a revendu"
            break
        if l <= stop:                                # le stop passe en premier si stop et TP dans la même minute
            sortie, raison = min(stop, o) * 0.97, "stop"
            break
        if tp and h >= tp:
            sortie, raison = max(tp, o), "TP"
            break
        if regle.get("suiveur_pct"):
            haut = max(haut, h)
            stop = max(stop, haut * (1 - regle["suiveur_pct"] / 100))
        sortie = cl
    if sortie is None:
        return None
    recu = qty * sortie * (1 - FRAIS) / (1 + imp)
    return {"roi": recu / MISE - 1, "raison": raison}


REGLES = [
    {"nom": "stop -20 / TP +30 / 90 min", "stop_pct": -20, "tp_pct": 30, "duree_max_min": 90},
    {"nom": "stop -15 / TP +50 / 3 h", "stop_pct": -15, "tp_pct": 50, "duree_max_min": 180},
    {"nom": "stop -30 / TP +100 / 6 h", "stop_pct": -30, "tp_pct": 100, "duree_max_min": 360},
    {"nom": "stop -20 / suiveur -20 % / 6 h", "stop_pct": -20, "suiveur_pct": 20, "duree_max_min": 360},
    {"nom": "revendre quand le wallet revend (stop -40 de sécurité)", "stop_pct": -40, "suivre_vente": True,
     "duree_max_min": 360},
]
RETARDS = [30, 90, 180]


def stats(rois):
    if not rois:
        return {"n": 0}
    return {"n": len(rois), "roi_moyen_pct": round(100 * statistics.mean(rois), 1),
            "roi_median_pct": round(100 * statistics.median(rois), 1),
            "gagnants_pct": round(100 * sum(r > 0 for r in rois) / len(rois)),
            "pnl_usd_mise_30": round(MISE * sum(rois), 1)}


def main():
    os.makedirs(CACHE, exist_ok=True)
    os.makedirs(PUBLIC, exist_ok=True)
    key = os.environ.get("HELIUS_API_KEY")
    if not key:
        progres(phase="erreur", erreur="clé Helius absente")
        return
    with open(os.path.join(REPO, "config.json"), encoding="utf-8") as f:
        wallets = [w["address"] if isinstance(w, dict) else w for w in json.load(f).get("smart_money") or []]
    depuis = time.time() - JOURS * 86400
    achats = []                     # (wallet, mint, t, sol)
    ventes = {}                     # (wallet, mint) -> [t]
    par_wallet = {}
    for i, w in enumerate(wallets):
        progres(phase=f"1/3 swaps des wallets {i + 1}/{len(wallets)}")
        try:
            ev = swaps_wallet(w, key, depuis)
        except Exception as e:
            par_wallet[w[:6]] = {"erreur": repr(e)[:120]}
            continue
        a = [e for e in ev if e["sens"] == "achat"]
        par_wallet[w[:6]] = {"achats": len(a), "ventes": len(ev) - len(a)}
        for e in ev:
            if e["sens"] == "achat":
                achats.append((w, e["mint"], e["t"], e["sol"]))
            else:
                ventes.setdefault((w, e["mint"]), []).append(e["t"])
    # premier achat de chaque (wallet, token) seulement : on copie l'entrée, pas les renforcements
    vus, premiers = set(), []
    for a in sorted(achats, key=lambda x: x[2]):
        if (a[0], a[1]) not in vus:
            vus.add((a[0], a[1]))
            premiers.append(a)
    # échantillon régulier si trop d'achats (limite de débit GeckoTerminal) ; les achats « 2 wallets » sont gardés
    total_premiers = len(premiers)
    # signal « 2 wallets » : 2e wallet différent qui achète le même token dans l'heure
    par_token = {}
    for a in premiers:
        par_token.setdefault(a[1], []).append(a)
    doubles = []
    for m, lst in par_token.items():
        lst.sort(key=lambda x: x[2])
        for k in range(1, len(lst)):
            if lst[k][2] - lst[0][2] <= 3600:
                doubles.append(lst[k])
                break
    if len(premiers) > MAX_ACHATS:
        pas = len(premiers) / MAX_ACHATS
        ech = [premiers[int(k * pas)] for k in range(MAX_ACHATS)]
        premiers = sorted(set(ech) | set(doubles), key=lambda x: x[2])
    tokens = sorted({a[1] for a in premiers})
    prix = {}
    for i, a in enumerate(premiers):
        if i % 10 == 0:
            progres(phase=f"2/3 prix GeckoTerminal {i}/{len(premiers)}")
        try:
            prix[(a[1], a[2])] = prix_token(a[1], a[2])
        except Exception as e:
            prix[(a[1], a[2])] = {"erreur": repr(e)[:80]}
    progres(phase="3/3 simulation")

    def lancer(liste):
        res = {}
        for regle in REGLES:
            for retard in RETARDS:
                r = dict(regle, retard_s=retard)
                rois, raisons, frais = [], {}, 0
                for (w, m, t, sol) in liste:
                    p = prix.get((m, t)) or {}
                    b = p.get("bougies") or []
                    if not b:
                        frais += 1
                        continue
                    liq = ((p.get("info") or {}).get("liq")) or 0
                    s = simuler(b, t, r, liq, ventes.get((w, m), []))
                    if s:
                        rois.append(s["roi"])
                        raisons[s["raison"]] = raisons.get(s["raison"], 0) + 1
                st = stats(rois)
                st["sans_prix"] = frais
                st["sorties"] = raisons
                res[f"{regle['nom']} | achat {retard} s après"] = st
        return res

    sortie = {
        "genere": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "periode_jours": JOURS, "wallets_suivis": len(wallets), "detail_wallets": par_wallet,
        "achats_copiables_total": total_premiers, "achats_simules": len(premiers), "tokens_distincts": len(tokens),
        "methode": __doc__.strip(),
        "copie_1_wallet": lancer(premiers),
        "copie_2_wallets_dans_l_heure": lancer(doubles),
        "achats_2_wallets": len(doubles),
    }
    # par wallet (meilleure règle simple, retard 90 s) : certains wallets valent peut-être mieux que d'autres
    r = dict(REGLES[4], retard_s=90)
    pw = {}
    for (w, m, t, sol) in premiers:
        p = prix.get((m, t)) or {}
        b = p.get("bougies") or []
        if b:
            s = simuler(b, t, r, ((p.get("info") or {}).get("liq")) or 0, ventes.get((w, m), []))
            if s:
                pw.setdefault(w[:6], []).append(s["roi"])
    sortie["par_wallet_revendre_avec_lui_90s"] = {w: stats(v) for w, v in sorted(pw.items())}
    ecrire(os.path.join(PUBLIC, "copytrade.json"), sortie)
    progres(phase="terminé")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        progres(phase="erreur", erreur=repr(e)[:300])
        raise
