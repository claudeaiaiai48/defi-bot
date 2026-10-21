#!/usr/bin/env python3
"""defi-bot - mode observation uniquement (aucun achat, aucune vente, aucun wallet).

Surveille les nouveaux tokens Solana et Base via l'API publique DexScreener,
enregistre des instantanés (volume, liquidité, transactions) et signale ceux
qui passent les filtres de config.json. Sortie publique : /var/lib/defi-bot/public.
"""
import json, os, time, urllib.request, urllib.error
from datetime import datetime, timezone
from paper import Paper
import security
import social

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


def append(name, obj):
    day = now().strftime("%Y-%m-%d")
    with open(os.path.join(DATA, f"{name}-{day}.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


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
            by_token.setdefault(p.get("baseToken", {}).get("address"), []).append(p)
        for a in chunk:
            p = best_pair(by_token.get(a))
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


def log(msg):
    print(f"{now().isoformat(timespec='seconds')} {msg}", flush=True)


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
    paper = None
    while True:
        cfg = load_config()
        t0 = time.time()
        pcfg = cfg.get("paper", {})
        if pcfg.get("enabled") and paper is None:
            paper = Paper(DATA, pcfg)
            log("paper trading activé")
        if paper:
            paper.cfg = pcfg
        events = []
        found = discover(cfg["chains"])
        if "base" in cfg["chains"] and cfg.get("clanker", {}).get("enabled"):
            new = [a for a in discover_clanker() if a.lower() not in security.CLANKER]
            for a in new:
                security.CLANKER.add(a.lower())
                found.setdefault(("base", a), set()).add("clanker")
            if new:
                with open(cl_path, "w", encoding="utf-8") as f:
                    json.dump(sorted(security.CLANKER)[-20000:], f)
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
                    if key in paper.state["open"]:
                        paper.update(key, snaps_chain.get(a), events)
                    elif key in paper.state["pending"]:
                        paper.update_pending(key, snaps_chain.get(a), events)
            for addr, p in snaps_chain.items():
                if (chain, addr) not in tracked:
                    continue
                row = compact(chain, addr, p, tracked[(chain, addr)]["sources"])
                append("observations", row)
                snaps += 1
                if (chain, addr) not in signaled and is_signal(chain, p, cfg):
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
                            paper.signal(row, strat, events)
                        else:
                            events.append({"type": "rejet sécurité", "key": f"{chain}:{addr}",
                                           "symbol": row["symbol"], "raisons": sec["raisons"],
                                           "details": sec.get("details")})
        if paper:
            paper.risk_check(events)
        for ev in events:
            ev["t"] = now().isoformat(timespec="seconds")
            append("paper", ev)
            log(f"PAPER {ev}")
        if paper:
            paper.save()
            write_public("paper.json", paper.summary())
        loops += 1
        write_public("status.json", {
            "mode": cfg["mode"], "started": started,
            "last_loop": now().isoformat(timespec="seconds"), "loops": loops,
            "tracked": len(tracked), "snapshots_last_loop": snaps,
            "signals_total": len(signaled), "sources": social.STATUS,
            "cles_presentes": social.keys_present(), "config": cfg,
        })
        write_public("signals.json", recent_signals)
        write_public("securite.json", sec_log)
        time.sleep(max(5, cfg["poll_seconds"] - (time.time() - t0)))


if __name__ == "__main__":
    main()
