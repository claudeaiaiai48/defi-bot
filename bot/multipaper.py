"""Portefeuille principal + portefeuilles de test (variantes) alimentés par les mêmes signaux.

Chaque variante est un Paper indépendant (son propre état, son propre capital) dont seules les règles de sortie
changent (config paper.variantes). Les conditions d'entrée (signaux, sécurité, repli) sont identiques.
"""
import copy
from paper import Paper


class MultiPaper:
    def __init__(self, data_dir, pcfg):
        self.data_dir = data_dir
        self.main = Paper(data_dir, pcfg)
        self.variants = {}
        self.seen = {}          # variante -> {(chaine, adresse)} déjà évalués avec ses propres seuils de signal
        self._sync(pcfg)

    # ----- configuration -----
    def _vcfg(self, name, pcfg, over):
        c = copy.deepcopy({k: v for k, v in pcfg.items() if k != "variantes"})
        c.update({k: v for k, v in over.items() if k not in ("enabled", "label")})
        c["reset_id"] = over.get("reset_id", f"variante-{name}-2026-10-23")
        c["post_exit_track"] = {"reasons": []}          # pas de suivi après sortie sur les variantes
        return c

    def _sync(self, pcfg):
        for name, over in (pcfg.get("variantes") or {}).items():
            if not over.get("enabled", True):
                continue
            vcfg = self._vcfg(name, pcfg, over)
            if name in self.variants:
                self.variants[name].cfg = vcfg
            else:
                self.variants[name] = Paper(self.data_dir, vcfg, name=name)
                self.seen[name] = set()

    @property
    def cfg(self):
        return self.main.cfg

    @cfg.setter
    def cfg(self, pcfg):
        self.main.cfg = pcfg
        self._sync(pcfg)

    @property
    def state(self):
        return self.main.state

    def _all(self):
        return [("", self.main)] + list(self.variants.items())

    @staticmethod
    def _tag(events, start, name):
        if name:
            for ev in events[start:]:
                ev["wallet"] = name

    # ----- interface utilisée par l'observateur -----
    def watched_keys(self):
        seen, out = set(), []
        for _, p in self._all():
            for k in p.watched_keys():
                if k not in seen:
                    seen.add(k)
                    out.append(k)
        return out

    def open_keys(self):
        seen = []
        for _, p in self._all():
            for k in p.state["open"]:
                if k not in seen:
                    seen.append(k)
        return seen

    def update_open(self, key, pair, events):
        for name, p in self._all():
            if key in p.state["open"]:
                n = len(events)
                p.update(key, pair, events)
                self._tag(events, n, name)

    def dispatch(self, key, pair, events):
        for name, p in self._all():
            n = len(events)
            if key in p.state["open"]:
                p.update(key, pair, events)
            elif key in p.state["pending"]:
                p.update_pending(key, pair, events)
            elif key in p.state.get("suivi", {}):
                p.update_suivi(key, pair, events)
            self._tag(events, n, name)

    def own_signal_variants(self):
        """Variantes qui ont leurs propres seuils de signal (config paper.variantes.X.signal)."""
        return {n: p for n, p in self.variants.items() if p.cfg.get("signal")}

    def signal(self, row, strat, events):
        for name, p in self._all():
            if name and p.cfg.get("signal"):
                continue          # cette variante reçoit ses propres signaux
            n = len(events)
            p.signal(row, strat, events)
            self._tag(events, n, name)

    def risk_check(self, events):
        for name, p in self._all():
            n = len(events)
            p.risk_check(events)
            self._tag(events, n, name)

    def save(self):
        for _, p in self._all():
            p.save()

    def publish(self, write_public):
        write_public("paper.json", self.main.summary())
        for name, p in self.variants.items():
            s = p.summary()
            s["variante"] = name
            s.pop("sorties_suivies", None)
            write_public(f"paper_{name}.json", s)
