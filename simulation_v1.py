# ==========================================
# MON PREMIER ROBOT DE TRADING - SIMULATION
# Version 1
# ==========================================

import time
from datetime import datetime

# Capital virtuel de départ
capital = 1000.0

# Prix virtuel de départ
prix = 100.0

# Position actuelle : 0 = aucune position
position = 0

print("==========================================")
print("🤖 ROBOT DE TRADING - MODE SIMULATION")
print("==========================================")
print(f"💰 Capital de départ : {capital:.2f} €")
print("🛡️ Aucun argent réel utilisé")
print("📊 Le robot est en cours de démarrage...")
print("------------------------------------------")

while True:
    maintenant = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

    print(f"[{maintenant}]")
    print(f"Prix simulé : {prix:.2f} €")

    if position == 0:
        print("📭 Aucune position ouverte")
    else:
        print("📈 Position virtuelle ouverte")

    print("------------------------------------------")

    # Pour l'instant, le robot attend 10 secondes
    time.sleep(10)

    # Petite variation virtuelle du prix
    prix += 0.10

    # Sécurité : éviter que le prix dépasse une valeur arbitraire
    if prix > 110:
        prix = 100
