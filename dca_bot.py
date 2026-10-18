#!/usr/bin/env python3
"""
BOT DCA MULTI-ACTIFS + COFFRE + RÉINVESTISSEMENT
Achat périodique automatique (testnet Binance)
=====================================================================
Achète un montant fixe à intervalle régulier (DCA) sur PLUSIEURS actifs,
sans essayer de timer le marché.

COFFRE (prise de profit) : quand le gain non réalisé dépasse un seuil,
vend une FRACTION de la position et met le produit de côté (coffre).

RÉINVESTISSEMENT : à chaque achat DCA, 2% du coffre est réinjecté
dans l'achat pour profiter du compound interest (backtesté optimal).

⚠️ Tourne sur testnet.binance.vision : argent fictif, aucun risque réel.

Usage :
    python dca_bot.py            # lance le bot en continu
    python dca_bot.py status     # affiche l'état actuel sans rien lancer
"""

import urllib.request
import json
import time
import hmac
import hashlib
import os
import sys
from datetime import datetime

try:
    from config_testnet import API_KEY, API_SECRET
except ImportError:
    print("ERREUR : config_testnet.py introuvable.")
    raise SystemExit(1)

BASE_URL = "https://testnet.binance.vision/api/v3"
MAINNET_URL = "https://api.binance.com/api/v3"

# ---- Paramètres DCA (multi-actifs) ----
ACTIFS = [
    {"symbol": "BNBUSDT", "montant": 20.0, "frequence_jours": 7},
    {"symbol": "ETHUSDT", "montant": 20.0, "frequence_jours": 7},
    {"symbol": "BTCUSDT", "montant": 20.0, "frequence_jours": 7},
]

# ---- Paramètres coffre (prise de profit) ----
SEUIL_PRISE_PROFIT = 0.075
FRACTION_PRISE_PROFIT = 0.25
COOLDOWN_PROFIT_JOURS = 14

# ---- Paramètres de réinvestissement du coffre ----
TAUX_REINVEST = 0.02
MONTANT_MIN_REINVEST = 1.0

CHECK_INTERVAL_S = 6 * 3600
FICHIER_ETAT = "etat_dca.json"
FICHIER_LOG = "historique_dca.txt"

FILTRES_CACHE = {}


def log(message):
    ligne = f"[{datetime.now().strftime('%d/%m %H:%M')}] {message}"
    print(ligne, flush=True)
    with open(FICHIER_LOG, "a") as f:
        f.write(ligne + "\n")


def sign(params):
    return hmac.new(API_SECRET.encode('utf-8'), params.encode('utf-8'), hashlib.sha256).hexdigest()


def api_request(endpoint, params="", method="GET"):
    url = f"{BASE_URL}{endpoint}"
    if method == "GET" and params:
        url += "?" + params
    elif method == "POST":
        url += "?" + params + "&signature=" + sign(params)
    req = urllib.request.Request(url, method=method)
    req.add_header('X-MBX-APIKEY', API_KEY)
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return json.loads(response.read().decode())
    except Exception as e:
        log(f"[!] ERREUR API: {e}")
        return None


def get_server_time():
    res = api_request("/time")
    return res["serverTime"] if res else int(time.time() * 1000)


def get_symbol_filters(symbol):
    res = api_request("/exchangeInfo", f"symbol={symbol}")
    if not res or "symbols" not in res or not res["symbols"]:
        return {"step_size": 0.001, "min_notional": 10.0}
    filtres = res["symbols"][0]["filters"]
    step_size, min_notional = 0.001, 10.0
    for f in filtres:
        if f["filterType"] == "LOT_SIZE":
            step_size = float(f["stepSize"])
        if f["filterType"] in ("MIN_NOTIONAL", "NOTIONAL"):
            min_notional = float(f.get("minNotional", 10.0))
    return {"step_size": step_size, "min_notional": min_notional}


def get_filters(symbol):
    if symbol not in FILTRES_CACHE:
        FILTRES_CACHE[symbol] = get_symbol_filters(symbol)
    return FILTRES_CACHE[symbol]


def arrondir_quantite(quantite, step_size):
    from decimal import Decimal
    q = Decimal(str(quantite))
    step = Decimal(str(step_size))
    pas = (q // step) * step
    return float(pas)


def get_prix_actuel(symbol):
    try:
        url = f"{MAINNET_URL}/ticker/price?symbol={symbol}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=15) as response:
            data = json.loads(response.read().decode())
            return float(data["price"])
    except Exception as e:
        log(f"[!] Erreur prix {symbol}: {e}")
        return None


def get_balance(asset="USDT"):
    ts = get_server_time()
    params = f"timestamp={ts}"
    url = f"{BASE_URL}/account?{params}&signature={sign(params)}"

    req = urllib.request.Request(url)
    req.add_header('X-MBX-APIKEY', API_KEY)
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            data = json.loads(response.read().decode())
            for bal in data.get("balances", []):
                if bal["asset"] == asset:
                    return float(bal["free"])
    except Exception as e:
        log(f"[!] ERREUR solde: {e}")
    return 0.0


def place_order(symbol, side, quantity):
    ts = get_server_time()
    params = f"symbol={symbol}&side={side}&type=MARKET&quantity={quantity}&timestamp={ts}"
    log(f"[ORDRE] {side} {quantity} {symbol.replace('USDT','')}...")
    res = api_request("/order", params, "POST")

    if res and "orderId" in res and res.get("status") == "FILLED":
        fills = res.get("fills", [])
        prix = float(fills[0].get("price", 0)) if fills else 0.0
        log(f"[ORDRE OK] ID: {res['orderId']} | Prix reel: {prix:.4f}")
        return prix
    log(f"[!] ECHEC ORDRE : {res}")
    return None


def charger_etat():
    if os.path.exists(FICHIER_ETAT):
        try:
            with open(FICHIER_ETAT, "r") as f:
                d = json.load(f)
            if "positions" not in d:
                log("[MIGRATION] Ancien format mono-actif detecte -> conversion multi-actifs.")
                positions = {}
                dernier_achat = {}
                if d.get("qty", 0.0) > 0:
                    positions["BNBUSDT"] = {
                        "qty": d.get("qty", 0.0),
                        "total_investi": d.get("total_investi", 0.0),
                        "coffre_usdt": d.get("coffre_usdt", 0.0),
                        "nb_achats": d.get("nb_achats", 0),
                        "nb_prises_profit": d.get("nb_prises_profit", 0),
                    }
                    dernier_achat["BNBUSDT"] = d.get("dernier_achat_time", 0)
                d = {
                    "positions": positions,
                    "dernier_achat_time": dernier_achat,
                    "dernier_profit_time": {},
                    "historique": d.get("historique", []),
                }
            return d
        except Exception:
            log("[!] Fichier d'etat corrompu, on repart de zero.")
    return {
        "positions": {},
        "dernier_achat_time": {},
        "dernier_profit_time": {},
        "historique": [],
    }


def sauver_etat(etat):
    with open(FICHIER_ETAT, "w") as f:
        json.dump(etat, f, indent=2)


def acheter(etat, filtres, actif):
    symbol = actif["symbol"]
    asset = symbol.replace("USDT", "")
    solde = get_balance("USDT")

    # --- Préparation du réinvestissement du coffre ---
    pos_temp = etat["positions"].get(symbol, {"coffre_usdt": 0.0})
    coffre_dispo = pos_temp.get("coffre_usdt", 0.0)
    montant_dca = actif["montant"]
    montant_reinvest = 0.0
    if coffre_dispo >= MONTANT_MIN_REINVEST:
        montant_reinvest = coffre_dispo * TAUX_REINVEST
        log(f"   🔄 Réinvestissement prévu : {montant_reinvest:.2f}$ du coffre {asset}")

    montant_total = montant_dca + montant_reinvest
    montant = min(montant_total, solde)

    # [CORRECTIF] Si le solde limite l'achat, la part financée par le coffre
    # doit être réduite dans la même proportion, sinon le coffre serait débité
    # d'un montant supérieur à ce qui a réellement été utilisé pour l'achat.
    if montant < montant_total and montant_total > 0:
        ratio = montant / montant_total
        montant_reinvest_avant = montant_reinvest
        montant_reinvest *= ratio
        if montant_reinvest_avant > 0:
            log(f"   ⚠️ Solde limité : réinvestissement réduit de {montant_reinvest_avant:.2f}$ à {montant_reinvest:.2f}$")

    if montant < filtres["min_notional"]:
        log(f"[!] Solde insuffisant pour {asset} ({montant:.2f}$ < {filtres['min_notional']}$).")
        return

    prix = get_prix_actuel(symbol)
    if not prix:
        return

    quantite = arrondir_quantite(montant / prix, filtres["step_size"])
    if quantite <= 0:
        return

    prix_reel = place_order(symbol, "BUY", quantite)
    if not prix_reel:
        return

    montant_reel = quantite * prix_reel

    pos = etat["positions"].setdefault(symbol, {
        "qty": 0.0, "total_investi": 0.0, "coffre_usdt": 0.0,
        "nb_achats": 0, "nb_prises_profit": 0,
    })

    if montant_reinvest > 0:
        decrement = min(montant_reinvest, pos["coffre_usdt"])
        pos["coffre_usdt"] -= decrement
        log(f"   💸 Coffre {asset} débité de {decrement:.2f}$ (reste: {pos['coffre_usdt']:.2f}$)")

    pos["qty"] += quantite
    pos["total_investi"] += montant_reel
    pos["nb_achats"] += 1
    etat.setdefault("dernier_achat_time", {})[symbol] = time.time()

    if montant_reinvest > 0:
        log(f"✅ ACHAT DCA {asset} #{pos['nb_achats']} : {quantite} @ {prix_reel:.4f}$ "
            f"(DCA: {montant_dca:.2f}$ + Coffre: {montant_reinvest:.2f}$ = {montant_reel:.2f}$)")
    else:
        log(f"✅ ACHAT DCA {asset} #{pos['nb_achats']} : {quantite} @ {prix_reel:.4f}$ ({montant_reel:.2f}$)")

    etat["historique"].append({
        "type": "ACHAT_DCA", "symbol": symbol, "time": datetime.now().isoformat(),
        "qty": quantite, "prix": prix_reel, "montant": montant_reel,
        "reinvest": montant_reinvest,
    })
    sauver_etat(etat)


def prise_de_profit(etat, filtres, actif):
    symbol = actif["symbol"]
    asset = symbol.replace("USDT", "")
    pos = etat["positions"].get(symbol)
    if not pos or pos["qty"] <= 0 or pos["total_investi"] <= 0:
        return

    prix = get_prix_actuel(symbol)
    if not prix:
        return

    valeur = pos["qty"] * prix
    gain_pct = (valeur - pos["total_investi"]) / pos["total_investi"]

    if gain_pct < SEUIL_PRISE_PROFIT:
        return

    dernier_profit = etat.get("dernier_profit_time", {}).get(symbol, 0)
    if time.time() - dernier_profit < COOLDOWN_PROFIT_JOURS * 86400:
        return

    qty_a_vendre = arrondir_quantite(pos["qty"] * FRACTION_PRISE_PROFIT, filtres["step_size"])
    if qty_a_vendre <= 0:
        return

    prix_vente = place_order(symbol, "SELL", qty_a_vendre)
    if not prix_vente:
        return

    produit = qty_a_vendre * prix_vente
    pos["coffre_usdt"] += produit

    ratio_reel = qty_a_vendre / pos["qty"]
    pos["total_investi"] *= (1 - ratio_reel)
    pos["qty"] -= qty_a_vendre

    pos["nb_prises_profit"] += 1
    etat.setdefault("dernier_profit_time", {})[symbol] = time.time()

    etat["historique"].append({
        "type": "PRISE_PROFIT", "symbol": symbol, "time": datetime.now().isoformat(),
        "qty": qty_a_vendre, "prix": prix_vente, "produit": produit, "gain_pct": gain_pct * 100,
    })
    log(f"💰 PRISE DE PROFIT {asset} #{pos['nb_prises_profit']} : vente de {qty_a_vendre} @ {prix_vente:.4f}$ (gain: {gain_pct*100:+.1f}%)")
    log(f"   +{produit:.2f}$ transferes au coffre. Coffre {asset}: {pos['coffre_usdt']:.2f}$")
    sauver_etat(etat)


def afficher_status(etat):
    solde = get_balance("USDT")

    print("=" * 70)
    print("STATUT DU BOT DCA (MULTI-ACTIFS + RÉINVESTISSEMENT)")
    print(f"Solde USDT disponible : {solde:.2f}$")
    print(f"Réinvestissement      : {TAUX_REINVEST*100:.0f}% du coffre à chaque DCA")
    print("=" * 70)

    for actif in ACTIFS:
        symbol = actif["symbol"]
        asset = symbol.replace("USDT", "")

        print(f"\n{asset} ({symbol}) — {actif['montant']}$ / {actif['frequence_jours']}j")
        print("-" * 70)

        pos = etat["positions"].get(symbol)
        if not pos:
            print("  Aucun achat effectue")
            continue

        prix = get_prix_actuel(symbol)

        print(f"  Achats effectues     : {pos['nb_achats']}")
        print(f"  Position actuelle    : {pos['qty']:.5f} {asset}")
        print(f"  Capital investi (net): {pos['total_investi']:.2f}$")

        if prix:
            valeur = pos["qty"] * prix
            gain = valeur - pos["total_investi"] if pos["total_investi"] > 0 else 0
            gain_pct = (gain / pos["total_investi"] * 100) if pos["total_investi"] > 0 else 0
            print(f"  Prix actuel          : {prix:.4f}$")
            print(f"  Valeur position      : {valeur:.2f}$ ({gain:+.2f}$ / {gain_pct:+.1f}%)")

        print(f"  Coffre (profits pris): {pos['coffre_usdt']:.2f}$")
        print(f"  Prises de profit     : {pos['nb_prises_profit']}")

    print("\n" + "=" * 70)
    n = len(etat["historique"])
    print(f"\nDerniers evenements ({min(10, n)} sur {n}) :")
    for h in etat["historique"][-10:]:
        asset = h.get("symbol", "?").replace("USDT", "")
        if h["type"] == "ACHAT_DCA":
            reinvest = h.get("reinvest", 0)
            if reinvest > 0:
                print(f"  [{h['time'][:16]}] ACHAT  {asset} {h['qty']:.5f} @ {h['prix']:.4f}$ "
                      f"(DCA + {reinvest:.2f}$ coffre = {h['montant']:.2f}$)")
            else:
                print(f"  [{h['time'][:16]}] ACHAT  {asset} {h['qty']:.5f} @ {h['prix']:.4f}$ ({h['montant']:.2f}$)")
        else:
            print(f"  [{h['time'][:16]}] PROFIT {asset} {h['qty']:.5f} @ {h['prix']:.4f}$ (+{h['produit']:.2f}$, {h['gain_pct']:+.1f}%)")


def verifier():
    etat = charger_etat()
    log("--- Verification ---")
    for actif in ACTIFS:
        symbol = actif["symbol"]
        asset = symbol.replace("USDT", "")
        filtres = get_filters(symbol)
        prix = get_prix_actuel(symbol)

        if prix is None:
            log(f"[!] Prix {symbol} indisponible, on reessaiera au prochain cycle.")
            continue

        dernier_achat = etat.get("dernier_achat_time", {}).get(symbol, 0)
        if time.time() - dernier_achat >= actif["frequence_jours"] * 86400:
            acheter(etat, filtres, actif)
        else:
            prochain = actif["frequence_jours"] * 86400 - (time.time() - dernier_achat)
            log(f"Prochain achat {asset} dans {int(prochain // 3600)}h")

        prise_de_profit(etat, filtres, actif)
        pos = etat["positions"].get(symbol)
        if pos:
            valeur = pos["qty"] * prix
            log(f"{asset}: {pos['qty']:.5f} ({valeur:.2f}$) | Coffre: {pos['coffre_usdt']:.2f}$ | Investi: {pos['total_investi']:.2f}$")

    sauver_etat(etat)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        afficher_status(charger_etat())
        return

    print("=" * 60)
    print("BOT DCA MULTI-ACTIFS + COFFRE + RÉINVESTISSEMENT (testnet)")
    for a in ACTIFS:
        print(f"  {a['symbol']}: {a['montant']}$ tous les {a['frequence_jours']}j")
    print(f"Prise de profit: {FRACTION_PRISE_PROFIT*100:.0f}% de la position des +{SEUIL_PRISE_PROFIT*100:.1f}%")
    print(f"Réinvestissement: {TAUX_REINVEST*100:.0f}% du coffre à chaque DCA")
    print("=" * 60)

    log("Test de connexion...")
    solde = get_balance("USDT")
    if solde <= 0:
        log("Solde = 0. Demande des fonds sur testnet.binance.vision")
        return
    log(f"Connexion OK. Solde USDT: {solde:.2f}")
    verifier()

    try:
        while True:
            time.sleep(CHECK_INTERVAL_S)
            verifier()
    except KeyboardInterrupt:
        print("\n[!] Arret manuel.")


if __name__ == "__main__":
    main()
