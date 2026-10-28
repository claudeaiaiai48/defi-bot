"""Backtest hors ligne sur les journaux d'observation du bot (/var/lib/defi-bot/observations-*.jsonl[.gz]).

Rejoue, pour chaque variante de backtest_grid.json, le cycle complet : signal -> attente d'un repli -> achat
-> sorties (stop, TP1, stop remonté, TP2, moonbag, limite de temps), avec le même modèle de coûts que le paper trading
(frais + glissement + impact de marché selon la liquidité). Aucun effet sur le bot ni sur les wallets :
lecture des journaux, écriture d'un seul fichier public (backtest.json).

Limites (documentées dans le résultat) :
- relevés toutes les ~30 s : un stop peut être exécuté plus bas que son niveau (comme en réel) ;
- contrôles de sécurité (GoPlus, honeypot, top 10) non rejoués : ils ne sont pas dans les journaux ;
- au-delà de 6 h de suivi, le token n'est plus journalisé : position close au dernier prix connu.
"""
import glob, gzip, json, os, statistics, sys, time
from datetime import datetime, timezone

DATA = os.environ.get("DEFIBOT_DATA", "/var/lib/defi-bot")
PUBLIC = os.path.join(DATA, "public")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HORIZON_S = 6 * 3600
TROU_MORT_S = 600          # 20 boucles sans relevé pendant le suivi : token disparu (valeur nulle)


def _f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def _ts(s):
    return datetime.fromisoformat(s).timestamp()


PROGRES = {"phase": "démarrage", "lignes": 0, "t0": time.time()}


def progres(**kw):
    """Fichier public de suivi du calcul (phase, lignes lues, mémoire) : permet de voir où il en est."""
    PROGRES.update(kw)
    try:
        import resource
        PROGRES["memoire_max_mo"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)
    except Exception:
        pass
    PROGRES["maj"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    PROGRES["duree_s"] = round(time.time() - PROGRES["t0"])
    try:
        os.makedirs(PUBLIC, exist_ok=True)
        tmp = os.path.join(PUBLIC, "backtest_progress.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({k: v for k, v in PROGRES.items() if k != "t0"}, f, ensure_ascii=False)
        os.replace(tmp, os.path.join(PUBLIC, "backtest_progress.json"))
    except OSError:
        pass


def lignes(fichiers):
    n = 0
    for fn in fichiers:
        op = gzip.open if fn.endswith(".gz") else open
        progres(fichier=os.path.basename(fn))
        try:
            with op(fn, "rt", encoding="utf-8") as f:
                for l in f:
                    n += 1
                    if n % 200000 == 0:
                        progres(lignes=n)
                    try:
                        yield json.loads(l)
                    except ValueError:
                        continue
        except (OSError, EOFError):
            continue


def releve(r):
    v, tx = r.get("vol") or {}, r.get("txns_h1") or {}
    return (_ts(r["t"]), _f(r.get("price_usd")), _f(r.get("mcap")), _f(r.get("liq_usd")),
            _f(v.get("h1")), _f(v.get("m5")), _f(tx.get("buys")), _f(tx.get("sells")),
            _f(r.get("pair_created")) / 1000.0, r.get("dex"))


T, PX, MC, LIQ, VH1, VM5, B1, S1, CREE, DEX = range(10)


def signal_ok(p, sig, chain):
    if p[VH1] < sig["min_volume_h1_usd"] or p[LIQ] < sig["min_liquidity_usd"] or p[B1] + p[S1] < sig["min_txns_h1"]:
        return False
    dex = sig.get(f"{chain}_dex_ids") or []
    return not dex or p[DEX] in dex


def filtre_ok(p, fe):
    if not fe:
        return True
    if fe.get("mcap_min_usd") and p[MC] < fe["mcap_min_usd"]:
        return False
    if fe.get("mcap_max_usd") and p[MC] > fe["mcap_max_usd"]:
        return False
    acc = (p[VM5] * 12 / p[VH1]) if p[VH1] else 0
    if fe.get("acceleration_min") and acc < fe["acceleration_min"]:
        return False
    if fe.get("age_max_min") and p[CREE] and (p[T] - p[CREE]) / 60 > fe["age_max_min"]:
        return False
    if fe.get("achats_ventes_h1_min") and (p[B1] / max(p[S1], 1)) < fe["achats_ventes_h1_min"]:
        return False
    if fe.get("liq_min_usd") and p[LIQ] < fe["liq_min_usd"]:
        return False
    return True


def simuler(serie, chain, r, fin_globale=None):
    """Un trade au plus par token et par variante (comme le bot). Renvoie None ou un dict de trade."""
    sig = r["signal"]
    i0 = next((i for i, p in enumerate(serie) if signal_ok(p, sig, chain)), None)
    if i0 is None:
        return None
    debut_suivi = serie[0][T]
    t_sig, haut, prec = serie[i0][T], serie[i0][PX], serie[i0][PX]
    entree = None
    for i in range(i0 + 1, len(serie)):
        p = serie[i]
        if p[T] - t_sig > r["entry_window_min"] * 60:
            return None
        if p[PX] <= 0:
            continue
        bougie = (p[PX] / prec - 1) * 100 if prec else 0
        prec = p[PX]
        haut = max(haut, p[PX])
        chute = (1 - p[PX] / haut) * 100
        if r["pullback_min_pct"] <= chute <= r["pullback_max_pct"] and bougie <= r["green_candle_max_pct"]:
            if not filtre_ok(p, r.get("filtre_entree")):
                return {"refus": True}
            entree = i
            break
    if entree is None:
        return None

    fee = (r["fee_pct"][chain] + r["slippage_pct"]) / 100
    extra = r["impact_extra_pct"] / 100
    mise = r["mise_usd"]

    def impact(usd, liq):
        return extra + (usd / (liq / 2.0) if liq > 0 else 0)

    p0 = serie[entree]
    px_e = p0[PX] * (1 + impact(mise, p0[LIQ]))
    qty0 = mise * (1 - fee) / px_e
    qty, recu, tp1, tp2, moon = qty0, 0.0, False, False, set()
    stop = p0[PX] * (1 + r["stop_initial_pct"] / 100)
    entry_px = p0[PX]

    def vendre(q, p):
        nonlocal qty, recu
        q = min(q, qty)
        if q <= 0:
            return
        val = q * p[PX]
        recu += val * (1 - fee) / (1 + impact(val, p[LIQ]))
        qty -= q

    raison, dernier = "fin des données", p0
    trail = r.get("trail_apres_tp1_pct")           # piste 1 : stop suiveur après TP1 (sous le plus haut)
    sr = r.get("sortie_rapide")                     # piste 3 : sortie si pas de +X % en N minutes
    plus_haut, gain_max = entry_px, 0.0
    for j in range(entree + 1, len(serie)):
        p, pp = serie[j], serie[j - 1]
        if p[T] - pp[T] > TROU_MORT_S and p[T] - debut_suivi < HORIZON_S:
            qty, raison = 0, ("moonbag abandonnée" if tp2 else "disparu")
            break
        if p[PX] <= 0:
            continue
        dernier = p
        gain = (p[PX] / entry_px - 1) * 100
        plus_haut, gain_max = max(plus_haut, p[PX]), max(gain_max, gain)
        if not tp1:
            if p[PX] <= stop:
                vendre(qty, p); raison = "stop"; break
            if sr and p[T] - p0[T] > sr["minutes"] * 60 and gain_max < sr["gain_min_pct"]:
                vendre(qty, p); raison = "temps court"; break
            if gain >= r["tp1_pct"]:
                vendre(qty0 * r["tp1_sell_frac"], p); tp1 = True
                stop = entry_px * (1 + r["stop_after_tp1_pct"] / 100)
            elif p[T] - p0[T] > r["max_minutes_without_tp1"] * 60:
                vendre(qty, p); raison = "temps"; break
            if not tp1 or gain < r["tp2_pct"]:
                continue
        if not tp2:
            if trail:
                stop = max(stop, plus_haut * (1 - trail / 100))
            if p[PX] <= stop:
                vendre(qty, p); raison = "stop_tp1"; break
            if gain >= r["tp2_pct"]:
                vendre(qty0 * r["tp2_sell_frac"], p); tp2 = True
            continue
        mult = p[PX] / entry_px
        for m, frac in r["moonbag_ladder"]:
            if mult >= m and m not in moon:
                moon.add(m)
                vendre(qty0 * r["moonbag_frac"] * frac, p)
        if qty <= qty0 * 1e-9:
            raison = "moonbag vendue"; break
    if qty > 0 and raison == "fin des données":
        limite = debut_suivi + HORIZON_S
        if fin_globale:
            limite = min(limite, fin_globale)
        if serie[-1][T] < limite - TROU_MORT_S:
            qty, raison = 0, ("moonbag abandonnée" if tp2 else "disparu")      # plus aucun relevé : pool mort
        else:
            vendre(qty, dernier)
    return {"t": p0[T], "roi": recu / mise - 1, "pnl": recu - mise, "raison": raison,
            "mcap": p0[MC], "age_min": round((p0[T] - p0[CREE]) / 60, 1) if p0[CREE] else None}


def resume(trades, refus):
    if not trades:
        return {"trades": 0, "refusés_par_filtre": refus}
    rois = [t["roi"] for t in trades]
    jours = {}
    for t in trades:
        d = datetime.fromtimestamp(t["t"], timezone.utc).strftime("%Y-%m-%d")
        j = jours.setdefault(d, [0, 0.0, 0])
        j[0] += 1; j[1] += t["pnl"]; j[2] += t["roi"] > 0
    raisons = {}
    for t in trades:
        raisons[t["raison"]] = raisons.get(t["raison"], 0) + 1
    moitie = len(trades) // 2
    st = sorted(trades, key=lambda t: t["t"])
    m = lambda g: round(100 * sum(x["roi"] for x in g) / len(g), 1) if g else None
    return {
        "trades": len(trades), "refusés_par_filtre": refus,
        "gagnants_pct": round(100 * sum(r > 0 for r in rois) / len(rois), 1),
        "roi_moyen_pct": round(100 * statistics.mean(rois), 1),
        "roi_median_pct": round(100 * statistics.median(rois), 1),
        "pnl_total_usd": round(sum(t["pnl"] for t in trades), 2),
        "roi_moyen_1re_moitie_pct": m(st[:moitie]), "roi_moyen_2e_moitie_pct": m(st[moitie:]),
        "par_jour": {d: {"trades": v[0], "pnl_usd": round(v[1], 2), "gagnants_pct": round(100 * v[2] / v[0])}
                     for d, v in sorted(jours.items())},
        "sorties": raisons,
    }


def main():
    t_start = time.time()
    with open(os.path.join(REPO, "backtest_grid.json"), encoding="utf-8") as f:
        grid = json.load(f)
    fichiers = sorted(glob.glob(os.path.join(DATA, "observations-*.jsonl*")))
    base = grid["base_regles"]
    variantes = {}
    for nom, v in grid["variantes"].items():
        r = json.loads(json.dumps(base))
        for k, x in v.items():
            r[k] = x
        variantes[nom] = r
    progres(phase="passe 1/3 : repérage des tokens avec signal", fichiers=len(fichiers))
    # passe 1 : tokens qui franchissent au moins un seuil de signal (allège la mémoire)
    seuils = {}
    for r in variantes.values():
        for ch in r["chains"]:
            seuils.setdefault(ch, []).append(r["signal"])
    gardes = set()
    n_lignes = 0
    for row in lignes(fichiers):
        n_lignes += 1
        ch = row.get("chain")
        if ch not in seuils or (ch, row.get("token")) in gardes:
            continue
        try:
            p = releve(row)
        except Exception:
            continue
        if any(signal_ok(p, s, ch) for s in seuils[ch]):
            gardes.add((ch, row.get("token")))
    progres(phase="passe 2/3 : chargement des séries", tokens_gardes=len(gardes), lignes_total=n_lignes)
    # passe 2 : séries complètes des tokens gardés
    series = {}
    for row in lignes(fichiers):
        k = (row.get("chain"), row.get("token"))
        if k in gardes:
            try:
                series.setdefault(k, []).append(releve(row))
            except Exception:
                pass
    for s in series.values():
        s.sort(key=lambda p: p[T])
    progres(phase="passe 3/3 : simulation des variantes", tokens_series=len(series))
    fin_globale = max((s[-1][T] for s in series.values()), default=None)
    res = {}
    for nom, r in variantes.items():
        trades, refus = [], 0
        for (ch, _), s in series.items():
            if ch not in r["chains"]:
                continue
            t = simuler(s, ch, r, fin_globale)
            if t is None:
                continue
            if t.get("refus"):
                refus += 1
                continue
            trades.append(t)
        out = resume(trades, refus)
        out["description"] = grid["variantes"][nom].get("description", "")
        res[nom] = out
    periode = None
    if series:
        ts = [p[T] for s in series.values() for p in (s[0], s[-1])]
        periode = [datetime.fromtimestamp(min(ts), timezone.utc).isoformat(timespec="minutes"),
                   datetime.fromtimestamp(max(ts), timezone.utc).isoformat(timespec="minutes")]
    sortie = {
        "genere": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "duree_calcul_s": round(time.time() - t_start, 1),
        "fichiers": [os.path.basename(f) for f in fichiers], "lignes_lues": n_lignes,
        "periode": periode,
        "tokens_avec_signal": {ch: sum(1 for k in series if k[0] == ch) for ch in seuils},
        "limites": __doc__.split("Limites (documentées dans le résultat) :")[1].strip(),
        "variantes": res,
    }
    os.makedirs(PUBLIC, exist_ok=True)
    tmp = os.path.join(PUBLIC, "backtest.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sortie, f, ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(PUBLIC, "backtest.json"))
    progres(phase="terminé")
    print(f"backtest : {n_lignes} lignes, {len(series)} tokens, {len(res)} variantes, {sortie['duree_calcul_s']} s")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        progres(phase="erreur", erreur=repr(e)[:300])
        os.makedirs(PUBLIC, exist_ok=True)
        with open(os.path.join(PUBLIC, "backtest.json"), "w", encoding="utf-8") as f:
            json.dump({"erreur": repr(e)[:500], "genere": datetime.now(timezone.utc).isoformat()}, f)
        raise
