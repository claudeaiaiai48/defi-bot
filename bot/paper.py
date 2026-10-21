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
- risque : 3 positions max, 10 trades par jour max, arrêt des entrées à -30 % du capital.
"""
import json, os, time
from datetime import datetime, timezone


def today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class Paper:
    def __init__(self, data_dir, cfg):
        self.path = os.path.join(data_dir, "paper_state.json")
        self.cfg = cfg
        self.state = None
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                self.state = json.load(f)
        if not self.state or self.state.get("reset_id") != cfg.get("reset_id"):
            self.state = {"reset_id": cfg.get("reset_id"), "cash": cfg["capital_usd"],
                          "open": {}, "closed": [], "pending": {}, "missed": [],
                          "entered": [], "trades_per_day": {}, "halted": False,
                          "started": time.time()}

    # ---------- utilitaires ----------
    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f)
        os.replace(tmp, self.path)

    def fee(self):
        return (self.cfg["fee_pct"] + self.cfg["slippage_pct"]) / 100

    def watched_keys(self):
        keys = list(self.state["open"]) + list(self.state["pending"])
        return [tuple(k.split(":", 1)) for k in keys]

    def equity(self):
        return self.state["cash"] + sum(p["qty"] * p["last_price"] * (1 - self.fee())
                                        for p in self.state["open"].values())

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
        st["pending"][key] = {"row": row, "strategy": strategy, "t": time.time(),
                              "high": price, "prev": price, "last": price}
        events.append({"type": "attente", "key": key, "symbol": row["symbol"],
                       "strategy": strategy, "price": price})

    def _can_enter(self):
        c, st = self.cfg, self.state
        if st["halted"]:
            return "arrêt -30 %"
        if len(st["open"]) >= c["max_open"]:
            return "3 positions ouvertes"
        if st["trades_per_day"].get(today(), 0) >= c["max_trades_per_day"]:
            return "10 trades aujourd'hui"
        if st["cash"] < c["position_usd"]:
            return "capital insuffisant"
        return None

    def update_pending(self, key, pair, events):
        c, st = self.cfg, self.state
        p = st["pending"][key]
        price = float((pair or {}).get("priceUsd") or 0)
        if price > 0:
            candle = (price / p["last"] - 1) * 100
            p["prev"], p["last"] = p["last"], price
            p["high"] = max(p["high"], price)
            drop = round((1 - price / p["high"]) * 100, 6)
            if (c["pullback_min_pct"] <= drop <= c["pullback_max_pct"]
                    and candle <= c["green_candle_max_pct"]):
                blocked = self._can_enter()
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
                                             "high": p["high"], "t": time.time()}])[-100:]
            events.append({"type": "pas de repli", "key": key, "symbol": p["row"]["symbol"]})

    def _buy(self, key, p, price, pair, events):
        c, st = self.cfg, self.state
        size = c["position_usd"]
        qty = size * (1 - self.fee()) / price
        st["cash"] -= size
        st["entered"].append(key)
        st["trades_per_day"][today()] = st["trades_per_day"].get(today(), 0) + 1
        row = p["row"]
        st["open"][key] = {
            "chain": row["chain"], "token": row["token"], "symbol": row["symbol"],
            "strategy": p["strategy"], "url": row.get("url"),
            "entry_t": time.time(), "entry_price": price, "cost_usd": size,
            "qty": qty, "qty0": qty, "proceeds_usd": 0.0, "sells": 0,
            "tp1": False, "tp2": False, "moon_steps": [], "missing": 0,
            "stop": price * (1 + c["stop_initial_pct"] / 100),
            "peak": price, "last_price": price, "exits": [],
        }
        events.append({"type": "achat", "key": key, "symbol": row["symbol"],
                       "strategy": p["strategy"], "price": price, "usd": size,
                       "repli_pct": round((1 - price / p["high"]) * 100, 1)})

    # ---------- sorties ----------
    def _sell(self, key, frac_of_initial, price, reason, events, all_=False, qty=None):
        pos = self.state["open"][key]
        if qty is None:
            qty = pos["qty"] if all_ else pos["qty0"] * frac_of_initial
        qty = min(pos["qty"], qty)
        usd = qty * price * (1 - self.fee()) if price > 0 else 0.0
        pos["qty"] -= qty
        pos["proceeds_usd"] += usd
        pos["sells"] += 1
        self.state["cash"] += usd
        pos["exits"].append({"t": time.time(), "price": price, "usd": round(usd, 2), "reason": reason})
        events.append({"type": "vente", "key": key, "symbol": pos["symbol"], "price": price,
                       "usd": round(usd, 2), "reason": reason})

    def _close(self, key, reason, events):
        pos = self.state["open"].pop(key)
        pnl = pos["proceeds_usd"] - pos["cost_usd"]
        trade = {k: pos[k] for k in ("chain", "token", "symbol", "strategy", "url",
                                     "entry_t", "entry_price", "cost_usd", "exits")}
        trade.update({"close_t": time.time(), "close_reason": reason,
                      "proceeds_usd": round(pos["proceeds_usd"], 2), "pnl_usd": round(pnl, 2),
                      "pnl_pct": round(100 * pnl / pos["cost_usd"], 1)})
        self.state["closed"].append(trade)
        events.append({"type": "clôture", "key": key, "symbol": pos["symbol"],
                       "pnl_usd": trade["pnl_usd"], "pnl_pct": trade["pnl_pct"], "reason": reason})

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
        pos["last_price"] = price
        pos["peak"] = max(pos["peak"], price)
        gain = (price / pos["entry_price"] - 1) * 100
        mult = price / pos["entry_price"]

        if not pos["tp1"]:
            if price <= pos["stop"]:
                self._sell(key, 1, price, f"stop dur {c['stop_initial_pct']} %", events, all_=True)
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
                self._sell(key, 1, price, f"stop +{c['stop_after_tp1_pct']} % (moonbag comprise)",
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

    def risk_check(self, events):
        c, st = self.cfg, self.state
        if not st["halted"] and self.equity() <= c["capital_usd"] * (1 - c["kill_switch_drawdown_pct"] / 100):
            st["halted"] = True
            events.append({"type": "arrêt", "reason": f"capital -{c['kill_switch_drawdown_pct']} %"})

    # ---------- résumé public ----------
    def summary(self):
        st, c = self.state, self.cfg
        equity = self.equity()
        by = {}
        for t in st["closed"]:
            s = by.setdefault(t["strategy"], {"trades": 0, "gagnants": 0, "pnl_usd": 0.0})
            s["trades"] += 1
            s["gagnants"] += t["pnl_usd"] > 0
            s["pnl_usd"] = round(s["pnl_usd"] + t["pnl_usd"], 2)
        for s in by.values():
            s["taux_reussite_pct"] = round(100 * s["gagnants"] / s["trades"], 1)
        return {
            "capital_depart": c["capital_usd"], "valeur": round(equity, 2),
            "pnl_total_pct": round(100 * (equity / c["capital_usd"] - 1), 2),
            "cash": round(st["cash"], 2), "arret_risque": st["halted"],
            "trades_aujourdhui": st["trades_per_day"].get(today(), 0),
            "en_attente_de_repli": [{"symbol": p["row"]["symbol"], "plus_haut": p["high"],
                                     "dernier": p["last"]} for p in st["pending"].values()],
            "positions_ouvertes": [{"symbol": p["symbol"], "strategy": p["strategy"],
                                    "entree": p["entry_price"], "actuel": p["last_price"],
                                    "gain_pct": round((p["last_price"] / p["entry_price"] - 1) * 100, 1),
                                    "tp1": p["tp1"], "tp2": p["tp2"], "url": p["url"]}
                                   for p in st["open"].values()],
            "trades_clos": len(st["closed"]), "par_strategie": by,
            "derniers_trades": st["closed"][-20:][::-1],
            "signaux_sans_repli": len(st["missed"]),
            "regles": {k: c[k] for k in c if k != "enabled"},
        }
