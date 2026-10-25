"""Paper trading (trading fictif) : aucune transaction réelle, aucun wallet.

Règles = document « Bot DeFi – Stratégie v1 » (validé par l'utilisateur), toutes
réglées dans config.json :
- entrée sur repli de 10 à 15 % sous le plus haut, dans une fenêtre de quelques minutes ;
  pas d'achat juste après une bougie d'une minute de plus de +15 % ;
- position principale 90 % : stop dur -20 %, +30 % -> vente 55 % et stop remonté à +8 %
  (s'il est touché avant +80 %, tout sort, moonbag comprise), +80 % -> vente 35 % ;
  sortie forcée si +30 % n'est pas atteint dans le délai ;
- moonbag 10 % (seulement après +80 %), sans stop ni limite de temps :
  50 % à x5, 25 % à x20, 25 % à x100 ; moonbag morte abandonnée, jamais revendue ;
- capital : deux réserves séparées (Solana, Base), frais propres à chaque chaîne ;
- risque : 3 positions max, 10 trades par jour max, arrêt des entrées à -30 % du capital total.
"""
import json, os, time
from datetime import datetime, timezone


def today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class Paper:
    def __init__(self, data_dir, cfg, name=""):
        self.path = os.path.join(data_dir, f"paper_state_{name}.json" if name else "paper_state.json")
        self.cfg = cfg
        self.state = None
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                self.state = json.load(f)
        if not self.state or self.state.get("reset_id") != cfg.get("reset_id"):
            self.state = {"reset_id": cfg.get("reset_id"), "cash": dict(cfg["reserves"]),
                          "open": {}, "closed": [], "pending": {}, "missed": [],
                          "entered": [], "trades_per_day": {}, "halted": False,
                          "started": time.time()}

        self.state.setdefault("suivi", {})
        for t in self.state["closed"][-30:]:      # rattrape les sorties récentes d'avant la fonction
            self._start_suivi(f"{t['chain']}:{t['token']}", t, late=True)

    # ---------- suivi après sortie (la sortie était-elle une bonne décision ?) ----------
    def _suivi_cfg(self):
        c = self.cfg.get("post_exit_track", {})
        return c.get("reasons", ["temps", "stop", "stop_tp1"]), c.get("minutes", 240)

    def _long_min(self):
        """Durée totale du suivi (le verdict court reste à 240 min, le suivi continue ensuite)."""
        return max(self.cfg.get("post_exit_track", {}).get("long_minutes", 10080), self._suivi_cfg()[1])

    def _verdict(self, g, price):
        evol = (price / g["exit_price"] - 1) * 100
        if g["first"] in ("tp1", "cible"):
            return "mauvaise sortie"
        if g["first"] == "stop" or evol <= -10:
            return "bonne sortie"
        return "mauvaise sortie" if evol >= 10 else "neutre"

    def _cible(self, g):
        """Prix dont l'atteinte après la sortie prouve que la sortie était prématurée."""
        c, r = self.cfg, g.get("raison", "temps")
        if r == "stop":
            return g["entry_price"]                                   # le token est revenu à l'entrée
        if r == "stop_tp1":
            return g["entry_price"] * (1 + c["tp2_pct"] / 100)        # il a atteint la cible TP2
        return g["entry_price"] * (1 + c["tp1_pct"] / 100)            # sortie au temps : il a atteint TP1

    def _start_suivi(self, key, trade, late=False):
        reasons, minutes = self._suivi_cfg()
        st = self.state.setdefault("suivi", {})
        if trade.get("close_reason") not in reasons or key in st:
            return
        if any(g.get("close_t") == trade.get("close_t") for g in st.values()):
            return
        if late and time.time() - trade["close_t"] > minutes * 60:
            return                                  # trop ancien : pas d'historique de prix
        exit_price = (trade.get("exits") or [{}])[-1].get("price")
        if not exit_price:
            return
        st[key] = {"chain": trade["chain"], "token": trade["token"], "symbol": trade["symbol"],
                   "strategy": trade["strategy"], "url": trade.get("url"),
                   "entry_price": trade["entry_price"], "exit_price": exit_price,
                   "close_t": trade["close_t"], "pnl_pct": trade["pnl_pct"],
                   "last": exit_price, "max": exit_price, "min": exit_price,
                   "marks": {}, "first": None, "missing": 0, "done": False,
                   "partiel": late, "verdict": None, "raison": trade.get("close_reason")}
        if len(st) > 300:                           # borne la taille de l'état : on garde les plus récents
            for k in sorted(st, key=lambda k: st[k]["close_t"])[:len(st) - 300]:
                del st[k]

    def update_suivi(self, key, pair, events):
        c, g = self.cfg, self.state["suivi"][key]
        _, minutes = self._suivi_cfg()
        long_min = self._long_min()
        if g.get("long_done"):
            return
        price = float((pair or {}).get("priceUsd") or 0)
        dead = False
        if pair is None:
            g["missing"] += 1
            if g["missing"] >= c["missing_loops_dead"]:
                price, dead = 1e-30, True           # token disparu : valeur nulle
            else:
                return
        elif price <= 0:
            return
        else:
            g["missing"] = 0
        g["last"] = price
        g["max"], g["min"] = max(g["max"], price), min(g["min"], price)
        if g["first"] is None:
            if price >= self._cible(g):
                g["first"] = "cible"
            elif g.get("raison", "temps") == "temps" and price <= g["entry_price"] * (1 + c["stop_initial_pct"] / 100):
                g["first"] = "stop"
        elapsed = (time.time() - g["close_t"]) / 60
        for m in (30, 60, 120, 240, 720, 1440, 2880, 4320, 10080):
            if m <= long_min and elapsed >= m and str(m) not in g["marks"]:
                g["marks"][str(m)] = round((price / g["exit_price"] - 1) * 100, 1)
        evol = round((price / g["exit_price"] - 1) * 100, 1)
        if not g["done"] and (elapsed >= minutes or dead):
            g["done"] = True
            g["verdict"] = self._verdict(g, price)
            events.append({"type": "suivi terminé", "key": key, "symbol": g["symbol"],
                           "verdict": g["verdict"], "evolution_pct": evol})
        if g["done"] and (elapsed >= long_min or dead):
            g["long_done"] = True
            g["verdict_long"] = self._verdict(g, price)
            events.append({"type": "suivi long terminé", "key": key, "symbol": g["symbol"],
                           "verdict_court": g["verdict"], "verdict_long": g["verdict_long"],
                           "evolution_pct": evol})

    def _is_rug(self, chain, token, symbol):
        """Token classé « rug » à la main (config post_exit_track.rugs : "chaine:SYMBOLE" ou "chaine:adresse")."""
        rugs = {str(r).lower() for r in self.cfg.get("post_exit_track", {}).get("rugs", [])}
        return f"{chain}:{symbol}".lower() in rugs or f"{chain}:{token}".lower() in rugs

    def rug_list(self):
        """Liste à part des rugs (exclus des stats de sortie), avec le snapshot sécurité pour affiner le filtre."""
        out = []
        for t in self.state["closed"]:
            if self._is_rug(t["chain"], t["token"], t["symbol"]):
                g = next((g for g in self.state.get("suivi", {}).values() if g.get("close_t") == t.get("close_t")), None)
                out.append({"symbol": t["symbol"], "chain": t["chain"], "token": t["token"],
                            "strategie": t["strategy"], "pnl_trade_pct": t["pnl_pct"],
                            "raison_sortie": t.get("close_reason"), "url": t.get("url"),
                            "evolution_depuis_sortie_pct": (round((g["last"] / g["exit_price"] - 1) * 100, 1) if g else None),
                            "securite_a_l_entree": t.get("securite"), "social": t.get("social")})
        return out

    def suivi_summary(self):
        gs = [g for g in self.state.get("suivi", {}).values() if not self._is_rug(g["chain"], g["token"], g["symbol"])]
        fin = [g for g in gs if g["done"]]
        n = lambda v: sum(1 for g in fin if g["verdict"] == v)
        _, minutes = self._suivi_cfg()
        par_raison = {}
        for g in gs:
            b = par_raison.setdefault(g.get("raison", "temps"),
                                      {"suivis": 0, "termines": 0, "bonnes": 0, "mauvaises": 0, "neutres": 0,
                                       "long_termines": 0, "long_bonnes": 0, "long_mauvaises": 0, "long_neutres": 0})
            b["suivis"] += 1
            if g["done"]:
                b["termines"] += 1
                k = {"bonne sortie": "bonnes", "mauvaise sortie": "mauvaises", "neutre": "neutres"}[g["verdict"]]
                b[k] += 1
            if g.get("long_done"):
                b["long_termines"] += 1
                b["long_" + {"bonne sortie": "bonnes", "mauvaise sortie": "mauvaises",
                             "neutre": "neutres"}[g["verdict_long"]]] += 1
        for b in par_raison.values():
            b["part_bonnes_pct"] = round(100 * b["bonnes"] / b["termines"], 1) if b["termines"] else None
            b["long_part_bonnes_pct"] = (round(100 * b["long_bonnes"] / b["long_termines"], 1)
                                         if b["long_termines"] else None)
        return {
            "rugs_a_part": self.rug_list(),
            "par_raison": par_raison,
            "regle_verdict": {"temps": "mauvaise sortie si +30 % vs entree atteint apres", "stop": "mauvaise sortie si retour au prix d'entree",
                              "stop_tp1": "mauvaise sortie si +80 % vs entree atteint", "bonne": "token tombe de 10 % ou plus apres la sortie"},
            "raisons_suivies": self._suivi_cfg()[0], "duree_verdict_court_min": minutes,
            "duree_suivi_long_min": self._long_min(),
            "en_cours": len(gs) - len(fin), "termines": len(fin),
            "bonnes_sorties": n("bonne sortie"), "mauvaises_sorties": n("mauvaise sortie"),
            "neutres": n("neutre"),
            "part_bonnes_pct": round(100 * n("bonne sortie") / len(fin), 1) if fin else None,
            "detail": [{"symbol": g["symbol"], "chain": g["chain"], "raison_sortie": g.get("raison", "temps"),
                        "pnl_trade_pct": g["pnl_pct"],
                        "prix_sortie": g["exit_price"], "prix_actuel": g["last"],
                        "evolution_depuis_sortie_pct": {**g["marks"],
                                                        "actuelle": round((g["last"] / g["exit_price"] - 1) * 100, 1)},
                        "max_depuis_sortie_pct": round((g["max"] / g["exit_price"] - 1) * 100, 1),
                        "min_depuis_sortie_pct": round((g["min"] / g["exit_price"] - 1) * 100, 1),
                        "premier_evenement": {"cible": "cible atteinte apres la sortie", "tp1": "cible atteinte apres la sortie",
                                              "stop": "chute a -20 % vs entree"}.get(g["first"], "aucun"),
                        "etat": "terminé" if g.get("long_done") else ("verdict court rendu, suivi continue" if g["done"] else "en cours"),
                        "verdict": g["verdict"], "verdict_long": g.get("verdict_long"), "suivi_partiel": g["partiel"], "url": g["url"]}
                       for g in gs[-40:][::-1]],
        }

    # ---------- utilitaires ----------
    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f)
        os.replace(tmp, self.path)

    def fee(self, chain):
        return (self.cfg["fee_pct"][chain] + self.cfg["slippage_pct"]) / 100

    def exec_factor(self, usd, liq, side):
        """Impact de marché réaliste (pool x*y=k) : écart entre prix observé et prix réellement obtenu.
        Achat : on paie (1 + mise/réserve) plus cher ; vente : on reçoit 1/(1 + valeur/réserve) de moins.
        réserve = liquidité/2 ; extra_pct = latence / mouvement de prix entre observation et exécution."""
        m = self.cfg.get("impact_marche") or {}
        if not m.get("enabled"):
            return 1.0
        x = float(m.get("extra_pct", 1.0)) / 100
        if liq and liq > 0:
            x += usd / (liq / 2.0)
        return (1 + x) if side == "buy" else 1 / (1 + x)

    def capital(self):
        return sum(self.cfg["reserves"].values())

    def watched_keys(self):
        keys = (list(self.state["open"]) + list(self.state["pending"])
                + [k for k, g in self.state.setdefault("suivi", {}).items() if not g.get("long_done")])
        return [tuple(k.split(":", 1)) for k in keys]

    def equity(self, chain=None):
        cash = self.state["cash"]
        value = sum(p["qty"] * p["last_price"] * (1 - self.fee(p["chain"]))
                    * self.exec_factor(p["qty"] * p["last_price"], p.get("liq"), "sell")
                    for p in self.state["open"].values() if chain in (None, p["chain"]))
        return (sum(cash.values()) if chain is None else cash[chain]) + value

    # ---------- signaux et entrées ----------
    def signal(self, row, strategy, events):
        """Un signal ne déclenche pas l'achat : il met le token en attente d'un repli."""
        c, st = self.cfg, self.state
        key = f"{row['chain']}:{row['token']}"
        price = float(row["price_usd"] or 0)
        if row["chain"] not in c["chains"] or price <= 0:
            return
        if key in st["open"] or key in st["pending"] or key in st["entered"]:
            return
        if any(x in strategy for x in c.get("exclure_strategies", [])):
            return                                  # portefeuilles de test : stratégies écartées
        st["pending"][key] = {"row": row, "strategy": strategy, "t": time.time(),
                              "high": price, "prev": price, "last": price}
        events.append({"type": "attente", "key": key, "symbol": row["symbol"],
                       "strategy": strategy, "price": price})

    def _can_enter(self, chain):
        c, st = self.cfg, self.state
        if st["halted"]:
            return "arrêt -30 %"
        limit = c.get("max_open_per_chain", c.get("max_open"))
        if limit and sum(1 for p in st["open"].values() if p["chain"] == chain) >= limit:
            return f"{limit} positions ouvertes sur {chain}"
        daily = c.get("max_trades_per_day")          # None / 0 = pas de limite journalière
        if daily and st["trades_per_day"].get(today(), 0) >= daily:
            return f"{daily} trades aujourd'hui"
        if self.position_size(chain) < c.get("position_min_usd", 0):
            return f"réserve {chain} insuffisante (gas {self.gas(chain)} $ gardé)"
        return None

    def gas(self, chain):
        return (self.cfg.get("gas_reserve_usd") or {}).get(chain, 0)

    def position_size(self, chain, liq=None):
        """Mise = position_pct % du capital de la chaîne (cash + positions), plafonnée au cash disponible moins le gas."""
        pct = self.cfg.get("position_pct")
        wanted = self.equity(chain) * pct / 100 if pct else self.cfg["position_usd"]
        # si la réserve ne couvre pas la mise, on investit le reste, en gardant un peu pour le gas
        available = self.state["cash"][chain] - self.gas(chain)
        size = max(0.0, min(wanted, available))
        mx = self.cfg.get("position_max_usd")                    # plafond absolu par trade (option, test)
        if mx:
            size = min(size, mx)
        pl = self.cfg.get("position_max_pct_liquidite")          # plafond en % de la liquidité du pool (option, test)
        if pl and liq:
            size = min(size, liq * pl / 100)
        return round(size, 2)

    def update_pending(self, key, pair, events):
        c, st = self.cfg, self.state
        p = st["pending"][key]
        price = float((pair or {}).get("priceUsd") or 0)
        if price > 0:
            candle = (price / p["last"] - 1) * 100
            p["prev"], p["last"] = p["last"], price
            p["high"] = max(p["high"], price)
            drop = round((1 - price / p["high"]) * 100, 6)
            p["maxdrop"] = max(p.get("maxdrop", 0), drop)      # mesure seule : plus forte chute vue pendant l'attente
            if (c["pullback_min_pct"] <= drop <= c["pullback_max_pct"]
                    and candle <= c["green_candle_max_pct"]):
                blocked = self._can_enter(p["row"]["chain"])
                if blocked:
                    st["pending"].pop(key)
                    events.append({"type": "refus", "key": key, "symbol": p["row"]["symbol"],
                                   "reason": blocked})
                    return
                st["pending"].pop(key)
                self._buy(key, p, price, pair, events)
                return
        if time.time() - p["t"] > c["entry_window_min"] * 60:
            st["pending"].pop(key)
            st["missed"] = (st["missed"] + [{"key": key, "symbol": p["row"]["symbol"],
                                             "signal_price": float(p["row"]["price_usd"]),
                                             "high": p["high"], "t": time.time(),
                                             "chute_max_pct": round(p.get("maxdrop", 0), 1)}])[-100:]
            events.append({"type": "pas de repli", "key": key, "symbol": p["row"]["symbol"],
                           "chute_max_pct": round(p.get("maxdrop", 0), 1)})

    @staticmethod
    def _outils_entree(pair):
        """Collecte d'infos (aucun effet sur les décisions) : état du marché au moment précis de l'achat."""
        try:
            from rugtools import market_pressure, late_entry
            return {"marche": market_pressure(pair), "tardif": late_entry(pair)}
        except Exception as e:
            return {"erreur": str(e)[:100]}

    def _buy(self, key, p, price, pair, events):
        c, st = self.cfg, self.state
        chain = p["row"]["chain"]
        liq = float(((pair or {}).get("liquidity") or {}).get("usd") or 0)
        size = self.position_size(chain, liq)
        mf = c.get("mise_fragile")          # mise réduite sur les tokens à structure fragile (gros détenteurs / créateur)
        if mf:
            sec = p["row"].get("securite") or {}
            t10 = sec.get("top10_hors_pool_pct")
            if (t10 is not None and t10 > mf.get("top10_pct", 20)) or (sec.get("createur_detient") and mf.get("createur", True)):
                size = round(size * mf.get("facteur", 0.5), 2)
        imp = self.exec_factor(size, liq, "buy")
        qty = size * (1 - self.fee(chain)) / (price * imp)
        st["cash"][chain] -= size
        st["entered"].append(key)
        st["trades_per_day"][today()] = st["trades_per_day"].get(today(), 0) + 1
        row = p["row"]
        st["open"][key] = {
            "chain": row["chain"], "token": row["token"], "symbol": row["symbol"],
            "strategy": p["strategy"], "url": row.get("url"),
            "entry_t": time.time(), "entry_price": price, "cost_usd": size,
            "qty": qty, "qty0": qty, "proceeds_usd": 0.0, "sells": 0, "liq": liq, "liq_entree": liq,
            "tp1": False, "tp2": False, "moon_steps": [], "missing": 0,
            "stop": price * (1 + c["stop_initial_pct"] / 100),
            "peak": price, "last_price": price, "exits": [],
            "social": row.get("social"), "securite": row.get("securite"),
            "outils": row.get("outils"), "outils_entree": self._outils_entree(pair),
        }
        events.append({"type": "achat", "key": key, "symbol": row["symbol"],
                       "strategy": p["strategy"], "price": price, "usd": size,
                       "repli_pct": round((1 - price / p["high"]) * 100, 1),
                       "impact_achat_pct": round((imp - 1) * 100, 2), "liquidite_usd": round(liq)})

    # ---------- sorties ----------
    def _sell(self, key, frac_of_initial, price, reason, events, all_=False, qty=None):
        pos = self.state["open"][key]
        if qty is None:
            qty = pos["qty"] if all_ else pos["qty0"] * frac_of_initial
        qty = min(pos["qty"], qty)
        usd = (qty * price * (1 - self.fee(pos["chain"])) * self.exec_factor(qty * price, pos.get("liq"), "sell")
               if price > 0 else 0.0)
        pos["qty"] -= qty
        pos["proceeds_usd"] += usd
        pos["sells"] += 1
        self.state["cash"][pos["chain"]] += usd
        pos["exits"].append({"t": time.time(), "price": price, "usd": round(usd, 2), "reason": reason})
        events.append({"type": "vente", "key": key, "symbol": pos["symbol"], "price": price,
                       "usd": round(usd, 2), "reason": reason})

    def _close(self, key, reason, events):
        pos = self.state["open"].pop(key)
        pnl = pos["proceeds_usd"] - pos["cost_usd"]
        trade = {k: pos.get(k) for k in ("chain", "token", "symbol", "strategy", "url",
                                         "entry_t", "entry_price", "cost_usd", "exits", "social", "securite",
                                         "outils", "outils_entree")}
        trade.update({"close_t": time.time(), "close_reason": reason,
                      "proceeds_usd": round(pos["proceeds_usd"], 2), "pnl_usd": round(pnl, 2),
                      "pnl_pct": round(100 * pnl / pos["cost_usd"], 1)})
        self.state["closed"].append(trade)
        self._start_suivi(key, trade)
        events.append({"type": "clôture", "key": key, "symbol": pos["symbol"],
                       "pnl_usd": trade["pnl_usd"], "pnl_pct": trade["pnl_pct"], "reason": reason})

    def _px_stop(self, pos, price):
        """Simulation d'un ordre déclencheur sur la chaîne (option stop_ordre) : exécution au prix du stop moins un
        glissement fixe, au lieu du prix observé après la chute. Borne haute (optimiste en cas de trou de prix)."""
        o = self.cfg.get("stop_ordre")
        if o and pos.get("stop") and price < pos["stop"]:
            return max(price, pos["stop"] * (1 - o.get("glissement_pct", 3) / 100))
        return price

    def update(self, key, pair, events):
        c, pos = self.cfg, self.state["open"][key]
        if pair is None:
            pos["missing"] += 1
            if pos["missing"] >= c["missing_loops_dead"]:
                # token disparu : la position restante vaut zéro (moonbag morte abandonnée)
                pos["qty"] = 0
                self._close(key, "moonbag abandonnée" if pos["tp2"] else "disparu", events)
            return
        pos["missing"] = 0
        price = float(pair.get("priceUsd") or 0)
        if price <= 0:
            return
        # garde-fou : on n'accepte pas une hausse venant d'un pool quasi vide ou d'un saut isolé
        liq_new = float((pair.get("liquidity") or {}).get("usd") or 0)
        ref_liq = float(pos.get("liq") or 0)
        last = pos.get("last_price") or pos["entry_price"]
        if price > last:
            if liq_new < max(2500.0, 0.3 * ref_liq):
                pos["prix_ignores"] = pos.get("prix_ignores", 0) + 1
                return
            if price > 3 * last and not (pos.get("saut_prix") and price > 2.5 * last):
                pos["saut_prix"] = price      # à confirmer par une 2e lecture
                return
        pos["saut_prix"] = None
        pos["last_price"] = price
        pos["liq"] = float((pair.get("liquidity") or {}).get("usd") or pos.get("liq") or 0)
        pos["peak"] = max(pos["peak"], price)
        sf = c.get("sortie_faiblesse")      # sortie anticipée sur signes de faiblesse (chute de liquidité, vagues de ventes)
        if sf and pos["qty"] > 0 and pos.get("liq_entree"):
            liq0, liq_now = float(pos["liq_entree"]), liq_new
            m5 = (pair.get("txns") or {}).get("m5") or {}
            ach, ven = m5.get("buys", 0), m5.get("sells", 0)
            raison = None
            if liq_now and liq_now < liq0 * (1 - sf.get("liq_chute_pct", 30) / 100):
                raison = f"faiblesse : liquidité -{round((1 - liq_now / liq0) * 100)} %"
            elif ven >= sf.get("min_ventes_m5", 15) and ven > sf.get("ventes_sur_achats", 2.5) * max(ach, 1):
                raison = f"faiblesse : {ven} ventes pour {ach} achats (5 min)"
            if raison:
                self._sell(key, 1, price, raison, events, all_=True)
                return self._close(key, "faiblesse", events)
        gain = (price / pos["entry_price"] - 1) * 100
        mult = price / pos["entry_price"]

        if not pos["tp1"]:
            if price <= pos["stop"]:
                self._sell(key, 1, self._px_stop(pos, price), f"stop dur {c['stop_initial_pct']} %", events, all_=True)
                return self._close(key, "stop", events)
            if gain >= c["tp1_pct"]:
                self._sell(key, c["tp1_sell_frac"], price, f"TP1 +{c['tp1_pct']} %", events)
                pos["tp1"] = True
                pos["stop"] = pos["entry_price"] * (1 + c["stop_after_tp1_pct"] / 100)
            elif time.time() - pos["entry_t"] > c["max_minutes_without_tp1"] * 60:
                self._sell(key, 1, price, f"{c['max_minutes_without_tp1']} min sans +{c['tp1_pct']} %",
                           events, all_=True)
                return self._close(key, "temps", events)
            if not pos["tp1"] or gain < c["tp2_pct"]:
                return

        if not pos["tp2"]:
            if price <= pos["stop"]:
                self._sell(key, 1, self._px_stop(pos, price), f"stop +{c['stop_after_tp1_pct']} % (moonbag comprise)",
                           events, all_=True)
                return self._close(key, "stop_tp1", events)
            if gain >= c["tp2_pct"]:
                self._sell(key, c["tp2_sell_frac"], price, f"TP2 +{c['tp2_pct']} %", events)
                pos["tp2"] = True
            return

        # moonbag : aucun stop, aucune limite de temps, ventes aux multiples
        moon0 = pos["qty0"] * c["moonbag_frac"]
        for m, frac in c["moonbag_ladder"]:
            if mult >= m and m not in pos["moon_steps"]:
                pos["moon_steps"].append(m)
                self._sell(key, 0, price, f"moonbag x{m}", events, qty=moon0 * frac)
        if pos["qty"] <= pos["qty0"] * 1e-9:
            self._close(key, "moonbag vendue", events)

    def injected(self, chain=None):
        """Capital total mis dans le compte fictif : réserves de départ + recharges."""
        inj = self.state.setdefault("injected", dict(self.cfg["reserves"]))
        return sum(inj.values()) if chain is None else inj.get(chain, 0)

    def _ajouter(self, split, raison, events):
        """Ajoute du capital fictif par chaîne ({chaîne: montant}) ; la perte totale reste documentée."""
        st = self.state
        eq = self.equity()
        inj = st.setdefault("injected", dict(self.cfg["reserves"]))
        for ch, a in split.items():
            st["cash"][ch] += a
            inj[ch] = inj.get(ch, 0) + a
        added = sum(split.values())
        perte = round(self.injected() - added - eq, 2)
        st.setdefault("refills", []).append({
            "t": time.time(), "valeur_avant_usd": round(eq, 2), "ajoute_usd": added, "raison": raison,
            "capital_total_injecte_usd": round(self.injected(), 2), "perte_totale_usd": perte})
        events.append({"type": "recharge", "raison": raison, "valeur_avant_usd": round(eq, 2),
                       "ajoute_usd": added, "perte_totale_avant_recharge_usd": perte})

    def risk_check(self, events):
        c, st = self.cfg, self.state
        for inj_ in c.get("injections") or []:          # apports ponctuels demandés par l'utilisateur (une seule fois)
            done = st.setdefault("injections_done", [])
            if inj_["id"] not in done:
                done.append(inj_["id"])
                self._ajouter(inj_["repartition"], f"apport {inj_['id']}", events)
        rc = c.get("refill_par_chaine")                 # recharge d'une chaîne qui passe sous le seuil
        if rc:
            for ch in c["reserves"]:
                if self.equity(ch) < rc["seuil_usd"]:
                    self._ajouter({ch: rc["montant_usd"][ch]}, f"recharge {ch} sous {rc['seuil_usd']} $", events)
        cf = c.get("coffre")                            # option (wallets de test) : l'excédent de cash sort du jeu
        if cf and self.equity() > cf["seuil_usd"]:
            cash_tot = sum(st["cash"].values())
            libre = cash_tot - sum(self.gas(ch) for ch in st["cash"])
            exc = min(self.equity() - cf["garder_usd"], libre)
            if exc > 1 and cash_tot > 0:
                for ch in st["cash"]:
                    st["cash"][ch] -= exc * st["cash"][ch] / cash_tot
                st["coffre"] = round(st.get("coffre", 0) + exc, 2)
                events.append({"type": "coffre", "ajoute_usd": round(exc, 2), "total_usd": st["coffre"]})
        rf = c.get("refill")
        if rf and self.equity() < rf["seuil_usd"]:
            # paper trading : le compte est rechargé ; la perte totale reste documentée
            eq = self.equity()
            split = rf.get("repartition") or {ch: rf["montant_usd"] / len(c["reserves"]) for ch in c["reserves"]}
            inj = st.setdefault("injected", dict(c["reserves"]))
            for ch, a in split.items():
                st["cash"][ch] += a
                inj[ch] = inj.get(ch, 0) + a
            added = sum(split.values())
            st.setdefault("refills", []).append({
                "t": time.time(), "valeur_avant_usd": round(eq, 2), "ajoute_usd": added,
                "capital_total_injecte_usd": round(self.injected(), 2),
                "perte_totale_usd": round(self.injected() - added - eq, 2)})
            events.append({"type": "recharge", "valeur_avant_usd": round(eq, 2), "ajoute_usd": added,
                           "perte_totale_avant_recharge_usd": round(self.injected() - added - eq, 2)})
        kill = c.get("kill_switch_drawdown_pct")
        if kill and not st["halted"] and self.equity() <= self.injected() * (1 - kill / 100):
            st["halted"] = True
            events.append({"type": "arrêt", "reason": f"capital -{kill} %"})

    # ---------- résumé public ----------
    def summary(self):
        st, c = self.state, self.cfg
        equity = self.equity() + st.get("coffre", 0)     # le coffre reste compté dans la valeur et le résultat
        by = {}
        chains = {ch: {"reserve_depart": r, "capital_injecte": round(self.injected(ch), 2),
                       "valeur": round(self.equity(ch), 2),
                       "pnl_pct": round(100 * (self.equity(ch) / self.injected(ch) - 1), 2),
                       "cash": round(st["cash"][ch], 2)}
                  for ch, r in c["reserves"].items()}
        injected = self.injected()
        refills = st.get("refills", [])
        recharges = {
            "nombre": len(refills), "montant_total_ajoute_usd": round(sum(r["ajoute_usd"] for r in refills), 2),
            "capital_depart_usd": self.capital(), "capital_total_injecte_usd": round(injected, 2),
            "valeur_actuelle_usd": round(equity, 2),
            "perte_totale_usd": round(injected - equity, 2),
            "perte_totale_pct": round(100 * (injected - equity) / injected, 1),
            "note": "perte_totale = capital total injecte (depart + recharges) - valeur actuelle ; negatif = gain",
            "historique": refills[-20:],
        }
        for t in st["closed"]:
            s = by.setdefault(t["strategy"], {"trades": 0, "gagnants": 0, "pnl_usd": 0.0})
            s["trades"] += 1
            s["gagnants"] += t["pnl_usd"] > 0
            s["pnl_usd"] = round(s["pnl_usd"] + t["pnl_usd"], 2)
        for s in by.values():
            s["taux_reussite_pct"] = round(100 * s["gagnants"] / s["trades"], 1)
        return {
            "capital_depart": self.capital(), "valeur": round(equity, 2),
            "pnl_total_pct": round(100 * (equity / injected - 1), 2),
            "recharges": recharges, "coffre_usd": round(st.get("coffre", 0), 2),
            "par_chaine": chains, "arret_risque": st["halted"],
            "trades_aujourdhui": st["trades_per_day"].get(today(), 0),
            "en_attente_de_repli": [{"symbol": p["row"]["symbol"], "chain": p["row"]["chain"], "plus_haut": p["high"],
                                     "dernier": p["last"]} for p in st["pending"].values()],
            "positions_ouvertes": [{"symbol": p["symbol"], "chain": p["chain"], "strategy": p["strategy"],
                                    "entree": p["entry_price"], "actuel": p["last_price"],
                                    "gain_pct": round((p["last_price"] / p["entry_price"] - 1) * 100, 1),
                                    "tp1": p["tp1"], "tp2": p["tp2"], "url": p["url"],
                                    "mise_usd": round(p.get("cost_usd", 0), 2),
                                    "encaisse_usd": round(p.get("proceeds_usd", 0), 2),
                                    "valeur_restante_usd": round(p["qty"] * p["last_price"], 2),
                                    "ventes": [{"prix": e["price"], "usd": e["usd"], "raison": e["reason"], "t": int(e["t"])} for e in p.get("exits", [])],
                                    "entree_t": int(p.get("entry_t", 0))}
                                   for p in st["open"].values()],
            "trades_clos": len(st["closed"]), "par_strategie": by,
            "derniers_trades": st["closed"][-20:][::-1],
            "signaux_sans_repli": len(st["missed"]),
            "sorties_suivies": self.suivi_summary(),
            "regles": {k: c[k] for k in c if k != "enabled"},
        }
