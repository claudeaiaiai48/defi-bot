"""Recherche continue des meilleurs réglages (stop, TP, sorties, entrées) sur les journaux d'observation.

Tourne en boucle sur le serveur, en tâche de fond à faible priorité. Ne touche ni au bot ni aux wallets :
lit les journaux, écrit son état (DATA/recherche_etat.json) et un résumé public (public/recherche_continue.json).

Méthode, pour limiter le risque de « coller au passé » :
- chaque configuration est rejouée sur tous les tokens, puis ses trades sont répartis en 3 tranches de temps ;
- score = rendement moyen de la PIRE tranche (une config n'est bonne que si elle tient sur les 3 périodes) ;
- 30 % des essais sont tirés au hasard (exploration), 70 % sont des variations d'une des meilleures configs
  (on change un seul réglage) ;
- les données sont rechargées toutes les 6 h et le classement est alors recalculé sur les nouvelles données.
"""
import hashlib, json, os, random, sys, time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest as bt

bt.progres = lambda **kw: None          # ne pas écraser le suivi du backtest périodique

ETAT = os.path.join(bt.DATA, "recherche_etat.json")
SORTIE = os.path.join(bt.PUBLIC, "recherche_continue.json")
RECHARGE_S = 6 * 3600
PUBLIE_S = 120
T = bt.T
RESERVE = {}          # période mise de côté (voir charger_donnees)


def maintenant():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def charger_grille():
    with open(os.path.join(bt.REPO, "backtest_grid.json"), encoding="utf-8") as f:
        return json.load(f)


def charger_donnees(espace, reserve_jours=0):
    """Deux passes sur les journaux : tokens qui franchissent au moins un des signaux de l'espace, puis leurs séries."""
    import glob
    fichiers = sorted(glob.glob(os.path.join(bt.DATA, "observations-*.jsonl*")))
    seuils = {ch: sigs for ch, sigs in espace["signaux"].items()}
    gardes, premier_vu = {}, {}
    for row in bt.lignes(fichiers):
        ch = row.get("chain")
        if ch not in seuils:
            continue
        k = (ch, row.get("token"))
        try:
            p = bt.releve(row)
        except Exception:
            continue
        premier_vu[k] = min(premier_vu.get(k, p[T]), p[T])
        if k not in gardes and any(bt.signal_ok(p, s, ch) for s in seuils[ch]):
            gardes[k] = p[T]
    series = {}
    for row in bt.lignes(fichiers):
        k = (row.get("chain"), row.get("token"))
        if k in gardes:
            try:
                p = bt.releve(row)
            except Exception:
                continue
            if p[T] >= gardes[k] - 60:
                series.setdefault(k, []).append(p)
    for s in series.values():
        s.sort(key=lambda p: p[T])
    fin = max((s[-1][T] for s in series.values()), default=None)
    # période mise de côté : les tokens apparus pendant les derniers reserve_jours ne sont JAMAIS vus par la recherche ;
    # seules les meilleures configs y sont rejouées, pour vérification (voir publier)
    limite = (fin - reserve_jours * 86400) if (fin and reserve_jours) else None
    par_chaine, reserve = {}, {}
    for (ch, tok), s in series.items():
        cible = reserve if (limite and s[0][T] >= limite) else par_chaine
        cible.setdefault(ch, []).append((tok, s))
    RESERVE.clear()
    RESERVE.update({"limite": limite, "series": reserve})
    # bornes des 3 tranches de temps, propres à chaque chaîne (autant de tokens dans chaque tranche)
    tranches = {}
    for ch, lst in par_chaine.items():
        ts = sorted(s[0][T] for _, s in lst)
        tranches[ch] = (ts[len(ts) // 3], ts[2 * len(ts) // 3]) if ts else (0, 0)
    return par_chaine, premier_vu, fin, tranches, len(fichiers)


def tirer(rng, esp, base, chaine):
    """Configuration complète tirée au hasard dans l'espace (entrée + filtre + sorties)."""
    r = json.loads(json.dumps(base))
    r["chains"] = [chaine]
    r["signal"] = rng.choice(esp["signaux"][chaine])
    ent = rng.choice(esp["entrees"])
    if ent["type"] == "repli":
        r["entree"] = None
        for k in ("pullback_min_pct", "pullback_max_pct", "entry_window_min"):
            r[k] = rng.choice(ent[k])
    else:
        r["entree"] = {k: (rng.choice(v) if isinstance(v, list) else v) for k, v in ent.items()}
    r["filtre_entree"] = rng.choice(esp["filtres"])
    for k, vals in esp["sorties"].items():
        r[k] = rng.choice(vals)
    return corriger(r)


def corriger(r):
    if r.get("entree") is None and r["pullback_min_pct"] >= r["pullback_max_pct"]:
        r["pullback_max_pct"] = r["pullback_min_pct"] + 10
    if r["tp2_pct"] <= r["tp1_pct"]:
        r["tp2_pct"] = r["tp1_pct"] * 2
    if r["tp1_sell_frac"] + r["tp2_sell_frac"] > 1:
        r["tp2_sell_frac"] = round(1 - r["tp1_sell_frac"], 2)
    return r


def muter(rng, esp, cfg):
    """Variation d'une bonne config : un seul réglage change (le plus souvent une sortie)."""
    r = json.loads(json.dumps(cfg))
    choix = rng.random()
    if choix < 0.75:
        k = rng.choice(list(esp["sorties"]))
        r[k] = rng.choice(esp["sorties"][k])
    elif choix < 0.9:
        r["filtre_entree"] = rng.choice(esp["filtres"])
    else:
        ent = r.get("entree")
        if ent:
            modele = next((e for e in esp["entrees"] if e["type"] == ent["type"]), None)
            if modele:
                k = rng.choice([k for k, v in modele.items() if isinstance(v, list)])
                ent[k] = rng.choice(modele[k])
        else:
            modele = next(e for e in esp["entrees"] if e["type"] == "repli")
            k = rng.choice(["pullback_min_pct", "pullback_max_pct", "entry_window_min"])
            r[k] = rng.choice(modele[k])
    return corriger(r)


CLES = ("signal", "entree", "pullback_min_pct", "pullback_max_pct", "entry_window_min", "filtre_entree",
        "stop_initial_pct", "tp1_pct", "tp1_sell_frac", "stop_after_tp1_pct", "trail_apres_tp1_pct",
        "tp2_pct", "tp2_sell_frac", "max_minutes_without_tp1", "sortie_rapide")


def empreinte(r):
    return hashlib.sha1(json.dumps({k: r.get(k) for k in CLES}, sort_keys=True).encode()).hexdigest()[:16]


def evaluer(r, chaine, donnees, mini):
    par_chaine, premier_vu, fin, tranches, _ = donnees
    c1, c2 = tranches.get(chaine, (0, 0))
    trades = []
    for tok, s in par_chaine.get(chaine, []):
        t = bt.simuler(s, chaine, r, fin, premier_vu.get((chaine, tok)))
        if t and not t.get("refus"):
            trades.append(t)
    parts = [[t for t in trades if t["t"] < c1], [t for t in trades if c1 <= t["t"] < c2],
             [t for t in trades if t["t"] >= c2]]
    st = [bt.stats(p) for p in parts]
    total = bt.stats(trades)
    ok = all(x["n"] >= mini for x in st)
    score = min(x["roi_moyen_pct"] for x in st) if ok else None
    return {"score": score, "total": total, "tranches": st}


def charger_etat():
    try:
        with open(ETAT, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"debut": maintenant(), "essais": 0, "chaines": {}}


def sauver(chemin, obj):
    tmp = chemin + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, chemin)


def resume_cfg(r):
    c = {k: r.get(k) for k in CLES}
    if c["entree"] is None:
        c["entree"] = {"type": "repli", "pullback_min_pct": r.get("pullback_min_pct"),
                       "pullback_max_pct": r.get("pullback_max_pct"), "entry_window_min": r.get("entry_window_min")}
    for k in ("pullback_min_pct", "pullback_max_pct", "entry_window_min"):
        c.pop(k, None)
    return c


def publier(etat, donnees, rech):
    out = {"mis_a_jour": maintenant(), "debut": etat["debut"], "configurations_essayees": etat["essais"],
           "donnees_chargees": etat.get("donnees_chargees"), "fichiers_journal": donnees[4],
           "methode": __doc__.split("Méthode, pour limiter le risque de « coller au passé » :")[1].strip(),
           "chaines": {}}
    for ch, e in etat["chaines"].items():
        cl, deja = [], set()
        for x in e["classement"]:          # une seule ligne par résultat identique (réglages sans effet)
            sig = (x["res"]["score"], json.dumps(x["res"]["total"], sort_keys=True))
            if x["res"]["score"] is not None and sig not in deja:
                deja.add(sig)
                cl.append(x)
        tranches = donnees[3].get(ch)
        verif = []
        res_series = (RESERVE.get("series") or {}).get(ch, [])
        if res_series:
            for x in cl[:10]:
                tr = []
                for tok, s in res_series:
                    t = bt.simuler(s, ch, x["cfg"], donnees[2], donnees[1].get((ch, tok)))
                    if t and not t.get("refus"):
                        tr.append(t)
                verif.append({"rang": cl.index(x) + 1, "score_recherche": x["res"]["score"],
                              "periode_mise_de_cote": bt.stats(tr)})
        out["chaines"][ch] = {
            "verification_sur_periode_mise_de_cote": {
                "debut": datetime.fromtimestamp(RESERVE["limite"], timezone.utc).isoformat(timespec="minutes")
                if RESERVE.get("limite") else None,
                "tokens": len(res_series), "top10": verif},
            "tokens": len(donnees[0].get(ch, [])),
            "tranches": [datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="minutes") for t in tranches]
            if tranches else None,
            "configs_evaluables": e.get("evaluables", 0),
            "configs_positives_sur_3_tranches": sum(1 for x in cl if x["res"]["score"] > 0),
            "meilleures": [{"score_pire_tranche_pct": x["res"]["score"], "total": x["res"]["total"],
                            "tranches": x["res"]["tranches"], "config": resume_cfg(x["cfg"])} for x in cl[:20]],
        }
    sauver(SORTIE, out)


def main():
    os.makedirs(bt.PUBLIC, exist_ok=True)
    rng = random.Random()
    etat = charger_etat()
    donnees, t_charge, t_pub = None, 0, 0
    while True:
        grid = charger_grille()
        rech = grid.get("recherche_continue")
        if not rech:
            time.sleep(600)
            continue
        esp, base, mini = rech["espace"], grid["base_regles"], rech.get("min_trades_par_tranche", 15)
        if donnees is None or time.time() - t_charge > RECHARGE_S:
            donnees, t_charge = charger_donnees(esp, rech.get("reserve_jours", 0)), time.time()
            etat["donnees_chargees"] = maintenant()
            for ch, e in etat["chaines"].items():          # nouvelles données : on recalcule le classement
                if ch in esp["signaux"]:
                    for x in e["classement"]:
                        x["res"] = evaluer(x["cfg"], ch, donnees, mini)
                    e["classement"].sort(key=lambda x: (x["res"]["score"] is not None, x["res"]["score"] or -1e9),
                                         reverse=True)
        if not any(donnees[0].get(ch) for ch in rech["chaines"]):
            time.sleep(600)                 # pas encore de données : on attendra le prochain rechargement
            t_charge = 0
            continue
        for ch in rech["chaines"]:
            if not donnees[0].get(ch):
                continue
            e = etat["chaines"].setdefault(ch, {"classement": [], "vus": [], "evaluables": 0})
            bons = [x for x in e["classement"] if x["res"]["score"] is not None][:20]
            if bons and rng.random() < 0.7:
                r = muter(rng, esp, rng.choice(bons)["cfg"])
            else:
                r = tirer(rng, esp, base, ch)
            h = empreinte(r)
            if h in e["vus"]:
                continue
            e["vus"] = (e["vus"] + [h])[-20000:]
            res = evaluer(r, ch, donnees, mini)
            etat["essais"] += 1
            if res["score"] is not None:
                e["evaluables"] += 1
                e["classement"].append({"cfg": r, "res": res})
                e["classement"].sort(key=lambda x: (x["res"]["score"] is not None, x["res"]["score"] or -1e9),
                                     reverse=True)
                del e["classement"][200:]
        if time.time() - t_pub > PUBLIE_S:
            sauver(ETAT, etat)
            publier(etat, donnees, rech)
            t_pub = time.time()


if __name__ == "__main__":
    main()
