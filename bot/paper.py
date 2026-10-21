"""Paper trading (trading fictif) : aucune transaction réelle, aucun wallet.

Ouvre une position fictive sur chaque signal, applique les règles de sortie de
config.json (stop initial, TP1, stop remonté, TP2, moonbag), compte les frais,
et sauvegarde l'état pour survivre aux redémarrages.
"""
import json, os, time


class Paper:
    def __init__(self, data_dir, cfg):
        self.path = os.path.join(data_dir, "paper_state.json")
        self.cfg = cfg
        self.state = {"cash": cfg["capital_usd"], "open": {}, "closed": [],
                      "started": time.time()}
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                self.state = json.load(f)

    # ---------- utilitaires ----------
    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f)
        os.replace(tmp, self.path)

    def fee(self):
        return (self.cfg["fee_pct"] + self.cfg["slippage_pct"]) / 100

    def open_keys(self):
        return [tuple(k.split(":", 1)) for k in self.state["open"]]

    # ---------- entrées ----------
    def enter(self, row, strategy, events):
        key = f"{row['chain']}:{row['token']}"
        price = float(row["price_usd"] or 0)
        size = self.cfg["position_usd"]
        entered = self.state.setdefault("entered", [])
        if key in self.state["open"] or key in entered or price <= 0:
            return
        if len(self.state["open"]) >= self.cfg["max_open"] or self.state["cash"] < size:
            events.append({"type": "skip", "key": key, "reason": "capital ou max positions"})
            return
        qty = size * (1 - self.fee()) / price
        self.state["cash"] -= size
        entered.append(key)
        self.state["open"][key] = {
            "chain": row["chain"], "token": row["token"], "symbol": row["symbol"],
            "strategy": strategy, "url": row.get("url"),
            "entry_t": time.time(), "entry_price": price, "cost_usd": size,
            "qty": qty, "qty0": qty, "proceeds_usd": 0.0,
            "tp1": False, "tp2": False, "stop": price * (1 + self.cfg["stop_initial_pct"] / 100),
            "peak": price, "last_price": price, "missing": 0,
            "entry_liq": row.get("liq_usd") or 0, "exits": [],
        }
        events.append({"type": "buy", "key": key, "symbol": row["symbol"],
                       "strategy": strategy, "price": price, "usd": size})

    # ---------- sorties ----------
    def _sell(self, pos, frac_of_initial, price, reason, events, key):
        qty = min(pos["qty"], pos["qty0"] * frac_of_initial) if frac_of_initial < 1 else pos["qty"]
        usd = qty * price * (1 - self.fee())
        pos["qty"] -= qty
        pos["proceeds_usd"] += usd
        self.state["cash"] += usd
        pos["exits"].append({"t": time.time(), "price": price, "usd": round(usd, 2), "reason": reason})
        events.append({"type": "sell", "key": key, "symbol": pos["symbol"], "price": price,
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
        events.append({"type": "close", "key": key, "symbol": pos["symbol"],
                       "pnl_usd": trade["pnl_usd"], "pnl_pct": trade["pnl_pct"], "reason": reason})

    def update(self, key, pair, events):
        """pair = meilleure paire DexScreener du token, ou None si introuvable."""
        self._update(key, pair, events)
        pos = self.state["open"].get(key)
        if pos and pos["qty"] <= pos["qty0"] * 1e-9:
            self._close(key, "vendu", events)

    def _update(self, key, pair, events):
        c, pos = self.cfg, self.state["open"][key]
        if pair is None:
            pos["missing"] += 1
            if pos["missing"] >= c["missing_loops_rug"]:
                self._sell(pos, 1, 0.0, "disparu (rug présumé)", events, key)
                self._close(key, "rug", events)
            return
        pos["missing"] = 0
        price = float(pair.get("priceUsd") or 0)
        liq = (pair.get("liquidity") or {}).get("usd") or 0
        if price <= 0:
            return
        pos["last_price"] = price
        pos["peak"] = max(pos["peak"], price)
        gain = (price / pos["entry_price"] - 1) * 100

        if pos["entry_liq"] and liq < pos["entry_liq"] * c["rug_liq_ratio"]:
            self._sell(pos, 1, price, "liquidité retirée", events, key)
            return self._close(key, "rug", events)

        if not pos["tp1"]:
            if price <= pos["stop"]:
                self._sell(pos, 1, price, "stop initial", events, key)
                return self._close(key, "stop", events)
            if gain >= c["tp1_pct"]:
                self._sell(pos, c["tp1_sell_frac"], price, f"TP1 +{c['tp1_pct']}%", events, key)
                pos["tp1"] = True
                pos["stop"] = pos["entry_price"] * (1 + c["stop_after_tp1_pct"] / 100)
            elif time.time() - pos["entry_t"] > c["max_hold_hours"] * 3600:
                self._sell(pos, 1, price, "durée max sans TP1", events, key)
                return self._close(key, "temps", events)
            return

        if not pos["tp2"]:
            if price <= pos["stop"]:
                self._sell(pos, 1, price, f"stop +{c['stop_after_tp1_pct']}%", events, key)
                return self._close(key, "stop_tp1", events)
            if gain >= c["tp2_pct"]:
                keep = c["moonbag_frac"]
                frac = max(0.0, pos["qty"] / pos["qty0"] - keep)
                if frac > 0:
                    self._sell(pos, frac, price, f"TP2 +{c['tp2_pct']}% (moonbag gardée)", events, key)
                pos["tp2"] = True
            return

        # moonbag : stop suiveur depuis le plus haut, jamais sous le stop +8 %
        trail = max(pos["stop"], pos["peak"] * (1 - c["moonbag_trail_pct"] / 100))
        if price <= trail:
            self._sell(pos, 1, price, "stop suiveur moonbag", events, key)
            return self._close(key, "moonbag", events)

    # ---------- résumé public ----------
    def summary(self):
        st, c = self.state, self.cfg
        open_value = sum(p["qty"] * p["last_price"] * (1 - self.fee()) for p in st["open"].values())
        equity = st["cash"] + open_value
        by = {}
        for t in st["closed"]:
            s = by.setdefault(t["strategy"], {"trades": 0, "wins": 0, "pnl_usd": 0.0})
            s["trades"] += 1
            s["wins"] += t["pnl_usd"] > 0
            s["pnl_usd"] = round(s["pnl_usd"] + t["pnl_usd"], 2)
        for s in by.values():
            s["win_rate_pct"] = round(100 * s["wins"] / s["trades"], 1)
        return {
            "capital_depart_usd": c["capital_usd"], "equity_usd": round(equity, 2),
            "pnl_total_pct": round(100 * (equity / c["capital_usd"] - 1), 2),
            "cash_usd": round(st["cash"], 2), "positions_ouvertes": len(st["open"]),
            "trades_clos": len(st["closed"]), "par_strategie": by,
            "ouvertes": [{"symbol": p["symbol"], "chain": p["chain"], "strategy": p["strategy"],
                          "entree": p["entry_price"], "actuel": p["last_price"],
                          "gain_pct": round((p["last_price"] / p["entry_price"] - 1) * 100, 1),
                          "tp1": p["tp1"], "tp2": p["tp2"], "url": p["url"]}
                         for p in st["open"].values()],
            "derniers_trades": st["closed"][-20:][::-1],
            "regles": {k: c[k] for k in c if k != "enabled"},
        }
