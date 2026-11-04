"""Journal permanent des tokens refusés par les filtres anti-arnaque, et suivi de ce qu'ils deviennent.

- noter(ev) : appelé pour chaque refus / rejet de sécurité ; ajoute une ligne datée à refus.jsonl (dossier de données)
  et crée une fiche par token dans refus_suivi.json (dossier public).
- un fil d'arrière-plan mesure ensuite chaque token refusé sur DexScreener (prix, liquidité) à 0, 30, 60, 120, 240 et
  480 min après le premier refus, puis donne un verdict : le token a-t-il été retiré / s'est-il effondré (le filtre
  avait raison) ou est-il resté sain (le filtre a peut-être raté une occasion) ?
"""
import json, os, threading, time, urllib.request
from datetime import datetime, timezone

MOTS = ("honeypot", "liquidité", "rug", "taxe", "vente impossible", "tests impossibles", "transfert",
        "Jupiter", "sens unique")
ETAPES = (0, 30, 60, 120, 240, 480)          # minutes après le premier refus
GARDER_JOURS = 10
DETAILS = ("top10_hors_pool_pct", "top1_pct", "detenteurs", "holders_total", "taxe_max_pct", "clanker",
           "createur_detient")

_lock = threading.Lock()
_etat = {}
_dossiers = {}
_demarre = False


def _now():
    return datetime.now(timezone.utc)


def _iso():
    return _now().isoformat(timespec="seconds")


def _age_min(f):
    return (_now() - datetime.fromisoformat(f["premier_refus"])).total_seconds() / 60


def _demarrer(data, public):
    global _demarre
    if _demarre:
        return
    _dossiers["data"], _dossiers["public"] = data, public
    try:
        with open(os.path.join(public, "refus_suivi.json"), encoding="utf-8") as fh:
            _etat.update(json.load(fh).get("tokens", {}))
    except Exception:
        pass
    _demarre = True
    threading.Thread(target=_boucle, daemon=True).start()


def noter(ev, data, public):
    t = ev.get("type")
    raisons = ev.get("raisons") or ([ev.get("reason")] if ev.get("reason") else [])
    if t == "refus":
        if not any(m in str(raisons[0] if raisons else "") for m in MOTS):
            return
    elif t != "rejet sécurité":
        return
    key = ev.get("key")
    if not key or ":" not in key:
        return
    _demarrer(data, public)
    raison = " | ".join(str(x) for x in raisons)
    wallet = ev.get("wallet")
    with _lock:
        f = _etat.get(key)
        if f is None:
            f = _etat[key] = {"symbole": ev.get("symbol"), "premier_refus": _iso(), "refus": [], "mesures": {}}
        if len(f["refus"]) < 40 and not any(x["wallet"] == wallet and x["raison"] == raison for x in f["refus"]):
            f["refus"].append({"t": _iso(), "wallet": wallet, "raison": raison})
            d = ev.get("details") or {}
            if d and "details" not in f:
                f["details"] = {k: d[k] for k in DETAILS if k in d}
            try:
                with open(os.path.join(data, "refus.jsonl"), "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"t": _iso(), "key": key, "symbole": ev.get("symbol"), "wallet": wallet,
                                         "raison": raison}, ensure_ascii=False) + "\n")
            except Exception:
                pass


def _dex(key):
    chain, addr = key.split(":", 1)
    req = urllib.request.Request("https://api.dexscreener.com/latest/dex/tokens/" + addr,
                                 headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.loads(r.read().decode("utf-8"))
    ps = [p for p in d.get("pairs") or [] if p.get("chainId") == chain]
    if not ps:
        return {"absent": True}
    p = max(ps, key=lambda x: (x.get("liquidity") or {}).get("usd") or 0)
    return {"prix": float(p.get("priceUsd") or 0), "liq": (p.get("liquidity") or {}).get("usd") or 0,
            "mcap": p.get("marketCap"), "vol_h1": (p.get("volume") or {}).get("h1")}


def _verdict(f):
    ms = f["mesures"]
    if "0" not in ms or ms["0"].get("absent") or not ms["0"].get("prix"):
        return "inconnu (pas de mesure initiale)"
    if _age_min(f) < 120 or len(ms) < 3:
        return "trop tôt"
    der = ms[max(ms, key=lambda k: int(k))]
    if (der.get("vol_h1") == 0 and (ms["0"].get("vol_h1") or 0) > 0 and len(ms) >= 3
            and int(max(ms, key=lambda k: int(k))) >= 60):
        return "mort après refus : volume à zéro (liquidité retirée ou token abandonné, filtre probablement juste)"
    l0, p0 = ms["0"].get("liq") or 0, ms["0"]["prix"]
    if der.get("absent") or (l0 and (der.get("liq") or 0) < 0.2 * l0) or der.get("prix", 0) < 0.2 * p0:
        return "arnaque ou effondrement confirmé (filtre juste)"
    if (der.get("liq") or 0) >= 0.7 * l0 and der.get("prix", 0) >= 0.5 * p0:
        return "resté sain (filtre peut-être trop strict)"
    return "douteux (baisse partielle)"


def _ecrire():
    out = {"mis_a_jour": _iso(), "legende": "mesures : minutes après le premier refus ; liq et prix en USD",
           "tokens": {}}
    with _lock:
        for k, f in _etat.items():
            f["verdict"] = _verdict(f)
            out["tokens"][k] = f
    p = os.path.join(_dossiers["public"], "refus_suivi.json")
    with open(p + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    os.replace(p + ".tmp", p)


def _boucle():
    while True:
        try:
            with _lock:
                for k in [k for k, f in _etat.items() if _age_min(f) > GARDER_JOURS * 1440]:
                    del _etat[k]
                due = []
                for k, f in _etat.items():
                    age = _age_min(f)
                    for e in ETAPES:
                        if str(e) not in f["mesures"] and age >= e:
                            due.append((k, e))
                            break
            for k, e in due[:20]:
                try:
                    m = _dex(k)
                except Exception:
                    continue
                m["t"] = _iso()
                with _lock:
                    _etat[k]["mesures"][str(e)] = m
                time.sleep(0.4)
            _ecrire()
        except Exception:
            pass
        time.sleep(60)
