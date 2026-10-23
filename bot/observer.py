#!/usr/bin/env python3
"""defi-bot - mode observation uniquement (aucun achat, aucune vente, aucun wallet).

Surveille les nouveaux tokens Solana et Base via l'API publique DexScreener,
enregistre des instantanés (volume, liquidité, transactions) et signale ceux
qui passent les filtres de config.json. Sortie publique : /var/lib/defi-bot/public.
"""
import gzip, json, os, shutil, threading, time, urllib.request, urllib.error
from datetime import datetime, timezone
from paper import Paper
from multipaper import MultiPaper
import security
import social
import tgwatch
import rugtools

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get("DEFIBOT_DATA", "/var/lib/defi-bot")
PUBLIC = os.path.join(DATA, "public")
API = "https://api.dexscreener.com"
UA = {"User-Agent": "defi-bot-observer/1.0"}


def now():
    return datetime.now(timezone.utc)


def load_config():
    with open(os.path.join(REPO, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def get(path):
    req = urllib.request.Request(API + path, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


EV_LOG = []        # derniers événements des portefeuilles (publiés dans evenements.json pour l'analyse)


def append(name, obj):
    day = now().strftime("%Y-%m-%d")
    with open(os.path.join(DATA, f"{name}-{day}.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    if name == "paper":
        EV_LOG.append(obj)
        del EV_LOG[:-300]
        try:
            write_public("evenements.json", EV_LOG)
        except Exception:
            pass


def write_public(name, obj):
    tmp = os.path.join(PUBLIC, name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(PUBLIC, name))


def discover(chains):
    """Nouveaux tokens (profils + boosts récents) sur les chaînes suivies."""
    found = {}
    for path, source in (("/token-profiles/latest/v1", "profile"), ("/token-boosts/latest/v1", "boost")):
        try:
            for t in get(path) or []:
                chain, addr = t.get("chainId"), t.get("tokenAddress")
                if chain in chains and addr:
                    key = (chain, addr)
                    found.setdefault(key, set()).add(source)
        except Exception as e:
            log(f"discover {path}: {e}")
    return found


def discover_gecko(chains, pages=1):
    """Pools en tendance / nouveaux (GeckoTerminal, gratuit) : source « gecko », réservée aux portefeuilles de test."""
    found = {}
    for chain in chains:
        for kind in ("trending_pools", "new_pools"):
            for page in range(1, pages + 1):
                try:
                    req = urllib.request.Request(
                        f"https://api.geckoterminal.com/api/v2/networks/{chain}/{kind}?page={page}",
                        headers={**UA, "Accept": "application/json"})
                    with urllib.request.urlopen(req, timeout=15) as r:
                        data = json.loads(r.read().decode("utf-8")).get("data") or []
                    for d in data:
                        tid = (((d.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id", "")
                        if tid.startswith(chain + "_"):
                            found.setdefault((chain, tid[len(chain) + 1:]), set()).add("gecko")
                except Exception as e:
                    log(f"gecko {chain} {kind}: {e}")
                time.sleep(0.4)
    return found


def discover_clanker():
    """Derniers lancements Clanker sur Base (flux public clanker.world)."""
    try:
        req = urllib.request.Request("https://www.clanker.world/api/tokens?sort=desc", headers=UA)
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8")).get("data") or []
        return [t["contract_address"] for t in data
                if t.get("chain_id") == 8453 and t.get("contract_address")]
    except Exception as e:
        log(f"clanker: {e}")
        return []


def best_pair(pairs):
    pairs = [p for p in pairs or [] if p.get("liquidity", {}).get("usd")]
    return max(pairs, key=lambda p: p["liquidity"]["usd"]) if pairs else None


def snapshot(chain, addrs):
    out = {}
    for i in range(0, len(addrs), 30):
        chunk = addrs[i:i + 30]
        try:
            pairs = get(f"/tokens/v1/{chain}/{','.join(chunk)}")
        except Exception as e:
            log(f"tokens {chain}: {e}")
            continue
        by_token = {}
        for p in pairs or []:
            ba = p.get("baseToken", {}).get("address")
            by_token.setdefault(ba.lower() if chain == "base" and ba else ba, []).append(p)
        for a in chunk:
            p = best_pair(by_token.get(a.lower() if chain == "base" else a))
            if p:
                out[a] = p
    return out


def is_signal(chain, p, cfg):
    s = cfg["signal"]
    tx = p.get("txns", {}).get("h1", {})
    ok = (p.get("volume", {}).get("h1", 0) >= s["min_volume_h1_usd"]
          and p.get("liquidity", {}).get("usd", 0) >= s["min_liquidity_usd"]
          and tx.get("buys", 0) + tx.get("sells", 0) >= s["min_txns_h1"])
    dex_ids = s.get(f"{chain}_dex_ids") or []
    if dex_ids:
        ok = ok and p.get("dexId") in dex_ids
    return ok


def compact(chain, addr, p, sources):
    tx = p.get("txns", {})
    return {
        "t": now().isoformat(timespec="seconds"), "chain": chain, "token": addr,
        "symbol": p.get("baseToken", {}).get("symbol"), "dex": p.get("dexId"),
        "price_usd": p.get("priceUsd"), "mcap": p.get("marketCap") or p.get("fdv"),
        "liq_usd": p.get("liquidity", {}).get("usd"), "vol": p.get("volume"),
        "txns_h1": tx.get("h1"), "pair_created": p.get("pairCreatedAt"),
        "sources": sorted(sources), "url": p.get("url"),
    }


def compresser_anciens(data_dir):
    """Compresse (gzip, sans perte) les journaux .jsonl des jours précédents ; le fichier du jour reste intact."""
    today = now().strftime("%Y-%m-%d")
    try:
        for fn in sorted(os.listdir(data_dir)):
            if not fn.endswith(".jsonl") or today in fn:
                continue
            src, dst = os.path.join(data_dir, fn), os.path.join(data_dir, fn + ".gz")
            if os.path.exists(dst):
                continue
            with open(src, "rb") as fi, gzip.open(dst + ".tmp", "wb", compresslevel=9) as fo:
                shutil.copyfileobj(fi, fo)
            os.replace(dst + ".tmp", dst)
            os.remove(src)
            log(f"journal compressé : {fn}")
    except Exception as e:
        log(f"compression journaux: {e}")


def log(msg):
    print(f"{now().isoformat(timespec='seconds')} {msg}", flush=True)


def collecter_outils(chain, p, rep, row, cfg):
    """Collecte d'infos anti-rug (n'influence aucune décision). Le dictionnaire est créé tout de suite avec ses clés ;
    le fil d'arrière-plan ne fait que remplacer des valeurs (le trade peut déjà le référencer)."""
    ocfg = cfg.get("outils_collecte") or {}
    if not ocfg.get("enabled"):
        return
    out = row["outils"] = {"statut": "en cours", "marche": None, "tardif": None, "createur": None, "reseau": None}

    def run():
        try:
            helius = bool(ocfg.get("helius", True)) and chain == "solana" and rep is not None
            ev = rugtools.evaluate(p, rep, {"max_wallets": ocfg.get("max_wallets", 10)}, with_helius=helius)
            out["marche"], out["tardif"] = ev["marche"], ev["tardif"]
            out["createur"], out["reseau"] = ev["createur"], ev["reseau"]
            out["statut"] = "ok"
        except Exception as e:
            out["statut"] = f"erreur: {str(e)[:100]}"
    threading.Thread(target=run, daemon=True).start()


PRIX_LOG = []      # derniers relevés Jupiter vs DexScreener (publiés dans prix.json pour l'analyse)


def comparer_prix(paper, addrs, snaps):
    """Collecte seule (aucun effet sur les décisions) : prix Jupiter vs DexScreener des positions Solana ouvertes,
    pour mesurer de combien Jupiter est en avance sur les chutes de prix."""
    try:
        req = urllib.request.Request("https://lite-api.jup.ag/price/v3?ids=" + ",".join(addrs[:50]), headers=UA)
        with urllib.request.urlopen(req, timeout=4) as r:
            jup = json.loads(r.read().decode("utf-8"))
        err = None
    except Exception as e:
        jup, err = {}, str(e)[:60]
    t = now().isoformat(timespec="milliseconds")
    for a in addrs:
        pos = paper.state["open"].get(f"solana:{a}") or {}
        j = jup.get(a) or {}
        row = {"t": t, "token": a, "symbol": pos.get("symbol"), "entree": pos.get("entry_price"),
               "stop": pos.get("stop"), "dex": (snaps.get(a) or {}).get("priceUsd"),
               "jup": j.get("usdPrice"), "jup_bloc": j.get("blockId"), "jup_liq": j.get("liquidity"), "erreur": err}
        append("prix", row)
        PRIX_LOG.append(row)
    del PRIX_LOG[:-600]
    write_public("prix.json", PRIX_LOG)
    return {a: (jup.get(a) or {}).get("usdPrice") for a in addrs}


def signaux_variantes(paper, chain, addr, p, row, cfg, events):
    """Portefeuilles de test avec leurs propres seuils de signal / chaînes / sécurité (config paper.variantes.X)."""
    for name, vp in paper.own_signal_variants().items():
        if chain not in vp.cfg["chains"] or (chain, addr) in paper.seen[name]:
            continue
        if not is_signal(chain, p, {"signal": {**cfg["signal"], **vp.cfg["signal"]}}):
            continue
        scfg = {**cfg.get("security", {}), **(vp.cfg.get("securite") or {})}
        sec = security.check(chain, addr, scfg) if scfg.get("enabled") else {"ok": True}
        if sec.get("raisons") and all(r.startswith(("détenteurs pas encore indexés", "vérification impossible",
                                                    "GoPlus sans données")) for r in sec["raisons"]):
            continue                      # transitoire : on réessaiera
        paper.seen[name].add((chain, addr))
        n = len(events)
        r = dict(row)
        if sec["ok"]:
            r["securite"] = sec.get("details")
            strat = chain + ("-boost" if "boost" in r["sources"] else
                             "-clanker" if addr.lower() in security.CLANKER else "-organique")
            if (sec.get("details") or {}).get("avertissement"):
                strat += "-createurvendu"
            vp.signal(r, strat, events)
        else:
            events.append({"type": "rejet sécurité", "key": f"{chain}:{addr}", "symbol": r["symbol"],
                           "raisons": sec["raisons"], "details": sec.get("details")})
        for ev in events[n:]:
            ev["wallet"] = name


def fast_check(paper):
    """Contrôle rapide des positions OUVERTES uniquement (stops, TP, moonbag) entre deux grandes boucles.
    Les entrées en attente (règle de la bougie) et la découverte restent au rythme normal."""
    keys = [tuple(k.split(":", 1)) for k in paper.open_keys()]
    if not keys:
        return
    events = []
    for chain in sorted({c for c, _ in keys}):
        addrs = [a for c, a in keys if c == chain]
        snaps = snapshot(chain, addrs)
        jup = {}
        if chain == "solana":
            try:
                jup = comparer_prix(paper, addrs, snaps) or {}
            except Exception as e:
                log(f"comparer_prix: {e}")
        for a in addrs:
            key = f"{chain}:{a}"
            if a in snaps:     # donnée absente : on ne compte pas « disparu »
                paper.update_open(key, snaps[a], events, jup.get(a))
    paper.risk_check(events)
    for ev in events:
        ev["t"] = now().isoformat(timespec="seconds")
        append("paper", ev)
        log(f"PAPER {ev}")
    paper.save()
    paper.publish(write_public)


def main():
    os.makedirs(PUBLIC, exist_ok=True)
    cl_path = os.path.join(DATA, "clanker.json")
    if os.path.exists(cl_path):
        with open(cl_path, encoding="utf-8") as f:
            security.CLANKER.update(json.load(f))
    tracked = {}   # (chain, addr) -> {"first": ts, "sources": set}
    signaled = set()
    recent_signals = []
    sec_log = {"acceptes": 0, "rejetes": 0, "raisons": {}, "derniers": []}
    started = now().isoformat(timespec="seconds")
    loops = 0
    log("defi-bot observation démarré")
    last_day = None
    try:
        tgwatch.start()
    except Exception as e:
        log(f"telegram: {e}")
    paper = None
    while True:
        cfg = load_config()
        t0 = time.time()
        if last_day != now().strftime("%Y-%m-%d"):
            last_day = now().strftime("%Y-%m-%d")
            threading.Thread(target=compresser_anciens, args=(DATA,), daemon=True).start()
        pcfg = cfg.get("paper", {})
        if pcfg.get("enabled") and paper is None:
            paper = MultiPaper(DATA, pcfg)
            log("paper trading activé")
        if paper:
            paper.cfg = pcfg
        events = []
        try:
            social.selftest()
        except Exception as e:
            log(f"selftest: {e}")
        found = discover(cfg["chains"])
        if "base" in cfg["chains"] and cfg.get("clanker", {}).get("enabled"):
            new = [a for a in discover_clanker() if a.lower() not in security.CLANKER]
            for a in new:
                security.CLANKER.add(a.lower())
                found.setdefault(("base", a), set()).add("clanker")
            if new:
                with open(cl_path, "w", encoding="utf-8") as f:
                    json.dump(sorted(security.CLANKER)[-20000:], f)
        gk = cfg.get("decouverte_gecko") or {}
        if gk.get("enabled") and loops % 2 == 0:
            lower = {(c, a.lower()): (c, a) for (c, a) in tracked}
            for (c, a), src in discover_gecko(cfg["chains"], gk.get("pages", 1)).items():
                found.setdefault(lower.get((c, a.lower()), (c, a)), set()).update(src)
        for key, src in found.items():
            entry = tracked.setdefault(key, {"first": t0, "sources": set()})
            entry["sources"] |= src
        horizon = cfg["track_hours"] * 3600
        for key in [k for k, v in tracked.items() if t0 - v["first"] > horizon]:
            del tracked[key]
        snaps = 0
        for chain in cfg["chains"]:
            addrs = [a for (c, a) in tracked if c == chain]
            if paper:
                addrs += [a for (c, a) in paper.watched_keys() if c == chain and a not in addrs]
            snaps_chain = snapshot(chain, addrs)
            if paper:
                for (c, a) in paper.watched_keys():
                    if c != chain:
                        continue
                    key = f"{c}:{a}"
                    paper.dispatch(key, snaps_chain.get(a), events)
            for addr, p in snaps_chain.items():
                if (chain, addr) not in tracked:
                    continue
                row = compact(chain, addr, p, tracked[(chain, addr)]["sources"])
                if tracked[(chain, addr)]["sources"] != {"gecko"}:
                    append("observations", row)
                snaps += 1
                if (chain, addr) not in signaled and is_signal(chain, p, cfg) and (tracked[(chain, addr)]["sources"] - {"gecko"}):
                    signaled.add((chain, addr))
                    try:
                        row["social"] = social.enrich(chain, row, cfg)
                    except Exception as e:
                        log(f"social: {e}")
                    append("signals", row)
                    recent_signals = ([row] + recent_signals)[:50]
                    log(f"SIGNAL {chain} {row['symbol']} vol1h={row['vol'].get('h1')} liq={row['liq_usd']}")
                    if paper and chain in paper.cfg["chains"]:
                        strat = chain + ("-boost" if "boost" in row["sources"] else
                                         "-clanker" if addr.lower() in security.CLANKER else "-organique")
                        scfg = cfg.get("security", {})
                        sec = security.check(chain, addr, scfg) if scfg.get("enabled") else {"ok": True}
                        sec_log["acceptes" if sec["ok"] else "rejetes"] += 1
                        for r in sec.get("raisons", []):
                            k = r.split(" =")[0].split(" (")[0]
                            sec_log["raisons"][k] = sec_log["raisons"].get(k, 0) + 1
                        sec_log["derniers"] = ([{"t": row["t"], "chain": chain, "symbol": row["symbol"],
                                                 "ok": sec["ok"], "raisons": sec.get("raisons", []),
                                                 "details": sec.get("details", {})}]
                                               + sec_log["derniers"])[:30]
                        transient = sec.get("raisons") and all(
                            r.startswith(("détenteurs pas encore indexés", "vérification impossible",
                                          "GoPlus sans données")) for r in sec["raisons"])
                        if transient:
                            signaled.discard((chain, addr))   # on réessaiera (cache de 10 min)
                        if sec["ok"]:
                            row["securite"] = sec.get("details")      # gardé avec le trade
                            collecter_outils(chain, p, security.REPORTS.get(addr), row, cfg)
                            if (sec.get("details") or {}).get("avertissement"):
                                strat += "-createurvendu"   # pour comparer avec / sans créateur
                            paper.signal(row, strat, events)
                        else:
                            events.append({"type": "rejet sécurité", "key": f"{chain}:{addr}",
                                           "symbol": row["symbol"], "raisons": sec["raisons"],
                                           "details": sec.get("details")})
                if paper:
                    signaux_variantes(paper, chain, addr, p, row, cfg, events)
        if paper:
            paper.risk_check(events)
        for ev in events:
            ev["t"] = now().isoformat(timespec="seconds")
            append("paper", ev)
            log(f"PAPER {ev}")
        if paper:
            paper.save()
            paper.publish(write_public)
        loops += 1
        write_public("status.json", {
            "mode": cfg["mode"], "started": started,
            "last_loop": now().isoformat(timespec="seconds"), "loops": loops,
            "tracked": len(tracked), "snapshots_last_loop": snaps,
            "signals_total": len(signaled), "sources": {**social.STATUS, "telegram": tgwatch.STATE},
            "cles_presentes": social.keys_present(), "config": cfg,
        })
        write_public("signals.json", recent_signals)
        write_public("securite.json", sec_log)
        fast = pcfg.get("fast_seconds") or 0
        deadline = t0 + cfg["poll_seconds"]
        if paper and fast:
            while time.time() + fast < deadline:
                time.sleep(fast)
                try:
                    fast_check(paper)
                except Exception as e:
                    log(f"fast_check: {e}")
        time.sleep(max(5, deadline - time.time()))


if __name__ == "__main__":
    main()
