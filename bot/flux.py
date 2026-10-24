"""Flux temps réel PumpPortal (MESURE SEULE, aucun effet sur les décisions).

Écoute les migrations pump.fun -> PumpSwap et les nouveaux tokens, puis compare l'heure de l'événement
avec l'heure à laquelle l'observateur découvre le token (flux.json). Sert à chiffrer le gain possible
d'une détection plus précoce avant de toucher aux achats.
"""
import json, threading, time
from datetime import datetime, timezone

STATE = {"connecte": False, "erreur": None, "evenements": 0, "nouveaux": 0, "migrations": 0, "reconnexions": 0}
MIGRES = {}          # mint -> heure (epoch) de la migration vue sur le flux
RECENT = []          # derniers événements (migrations)
RETARDS = []         # migrations ensuite découvertes par l'observateur : retard de découverte en secondes


def _iso(t=None):
    return datetime.fromtimestamp(t or time.time(), timezone.utc).isoformat(timespec="seconds")


def _traiter(msg):
    d = json.loads(msg)
    mint = d.get("mint")
    if not mint:
        return
    STATE["evenements"] += 1
    tx = d.get("txType")
    if tx == "create":
        STATE["nouveaux"] += 1
    elif tx == "migrate" or d.get("pool") == "pump-amm":
        STATE["migrations"] += 1
        MIGRES[mint] = time.time()
        RECENT.append({"t": _iso(), "mint": mint, "symbol": d.get("symbol")})
        del RECENT[:-100]
        if len(MIGRES) > 3000:
            for k in sorted(MIGRES, key=MIGRES.get)[:1000]:
                MIGRES.pop(k, None)


def _run():
    try:
        import websocket
    except Exception as e:
        STATE["erreur"] = "bibliothèque websocket absente: " + str(e)[:80]
        return
    pause = 5
    while True:
        try:
            ws = websocket.create_connection("wss://pumpportal.fun/api/data", timeout=45)
            ws.send(json.dumps({"method": "subscribeNewToken"}))
            ws.send(json.dumps({"method": "subscribeMigration"}))
            STATE["connecte"], STATE["erreur"], pause = True, None, 5
            while True:
                try:
                    msg = ws.recv()
                except websocket.WebSocketTimeoutException:
                    ws.ping()
                    continue
                if msg:
                    try:
                        _traiter(msg)
                    except Exception:
                        pass
        except Exception as e:
            STATE["connecte"] = False
            STATE["erreur"] = str(e)[:100]
            STATE["reconnexions"] += 1
            time.sleep(pause)
            pause = min(pause * 2, 120)


def start():
    threading.Thread(target=_run, daemon=True, name="flux").start()


def noter_decouverte(chain, addr, t):
    """Appelé quand l'observateur découvre un token : si le flux l'avait vu migrer, on mémorise le retard."""
    if chain != "solana":
        return
    tm = MIGRES.get(addr)
    if tm is not None and t >= tm:
        RETARDS.append({"mint": addr, "migre_t": _iso(tm), "decouvert_t": _iso(t), "retard_s": round(t - tm, 1)})
        del RETARDS[:-300]


def resume():
    r = sorted(x["retard_s"] for x in RETARDS)
    med = r[len(r) // 2] if r else None
    return {**STATE, "t": _iso(), "retards_n": len(r), "retard_median_s": med,
            "migrations_recentes": RECENT[-30:], "retards": RETARDS[-60:]}
