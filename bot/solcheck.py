"""Test de vente Solana par aller-retour Jupiter (lecture seule, devis uniquement : rien n'est signé ni envoyé).
On demande un devis d'achat (USDC -> jeton) puis un devis de revente de ce même montant de jetons (jeton -> USDC).
La perte de l'aller-retour mesure les frais de pool, l'impact et les prélèvements du jeton (frais de transfert Token-2022) ;
une absence de route de revente signifie que le jeton est invendable (perte 100 %)."""
import json, time, urllib.request, urllib.error

UA = {"User-Agent": "defi-bot-observer/1.0"}
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _quote(entree, sortie, montant):
    url = (f"https://lite-api.jup.ag/swap/v1/quote?inputMint={entree}&outputMint={sortie}"
           f"&amount={montant}&slippageBps=100")
    for essai in range(2):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=6) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            corps = e.read().decode("utf-8", "ignore").upper()
            if e.code == 429 and essai == 0:
                time.sleep(1.5)
                continue
            if e.code in (400, 404) and ("ROUTE" in corps or "COULD_NOT_FIND" in corps):
                return None            # aucune route : invendable
            raise


def aller_retour(mint, usd=30):
    """Perte en % d'un aller-retour d'environ usd dollars ; 100.0 si aucune route d'achat ou de revente."""
    q1 = _quote(USDC, mint, int(usd * 1e6))
    if not q1 or not q1.get("outAmount"):
        return 100.0
    q2 = _quote(mint, USDC, int(q1["outAmount"]))
    if not q2 or not q2.get("outAmount"):
        return 100.0
    return round((1 - int(q2["outAmount"]) / (usd * 1e6)) * 100, 1)
