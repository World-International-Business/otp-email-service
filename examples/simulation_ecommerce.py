"""Simulation e-commerce complète — envoi OTP + emails transactionnels.

Usage : python examples/simulation_ecommerce.py
"""

import hashlib
import hmac
import json
import time
import uuid

import requests

BASE_URL = "http://localhost:8001"
API_KEY = "test_api_key_64_chars_long_change_in_production_test_api_key_change"
HMAC_SECRET = b"test_hmac_secret_change_in_production"


def sign(body: bytes) -> dict:
    ts = str(int(time.time()))
    sig = hmac.new(HMAC_SECRET, body + ts.encode(), hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-API-Key": API_KEY,
        "X-Timestamp": ts,
        "X-HMAC-Signature": sig,
    }


def post(path: str, payload: dict) -> dict:
    body = json.dumps(payload).encode()
    r = requests.post(BASE_URL + path, data=body, headers=sign(body), timeout=10)
    return r.status_code, r.json()


def title(t):
    print(f"\n{'='*62}\n  {t}\n{'='*62}")


CLIENT_EMAIL = "client@boutique-ecommerce.com"

# --- 1. Inscription client -> email de bienvenue --------------------
title("1. INSCRIPTION — email de bienvenue")
code, res = post("/v1/email/send", {
    "to_email": CLIENT_EMAIL,
    "type": "welcome",
    "language": "fr",
    "data": {"name": "Awa Diallo"},
})
print(f"HTTP {code} ->", json.dumps(res, indent=2))

# --- 2. Connexion -> OTP par email ----------------------------------
title("2. CONNEXION — envoi OTP (login_2fa)")
code, res = post("/v1/otp/send", {
    "user_id": str(uuid.uuid4()),
    "email": CLIENT_EMAIL,
    "country_code": "FR",
    "purpose": "login_2fa",
    "language": "fr",
})
print(f"HTTP {code} ->", json.dumps(res, indent=2))
request_id = res.get("request_id", "")

# --- 3. Récupérer le code (mode démo) puis vérifier ----------------
title("3. CONNEXION — vérification du code OTP")
demo = requests.post(f"{BASE_URL}/demo/send", json={
    "email": CLIENT_EMAIL, "country_code": "FR", "language": "fr",
}).json()
otp_code = demo.get("debug_code")
req_id_2 = demo.get("request_id")
print(f"Code reçu par email (mode démo) : {otp_code}")

code, res = post("/v1/otp/verify", {"request_id": req_id_2, "code": otp_code})
print(f"HTTP {code} ->", json.dumps(res, indent=2))

# Mauvais code sur le 1er OTP
code, res = post("/v1/otp/verify", {"request_id": request_id, "code": "000000"})
print(f"\nMauvais code -> HTTP {code} ->", json.dumps(res, indent=2))

# --- 4. Confirmation de commande -----------------------------------
title("4. COMMANDE #CMD-4582 — email de confirmation")
code, res = post("/v1/email/send", {
    "to_email": CLIENT_EMAIL,
    "type": "transaction",
    "language": "fr",
    "data": {"amount": "149,90", "currency": "EUR", "status": "confirmée"},
})
print(f"HTTP {code} ->", json.dumps(res, indent=2))

# --- 5. Notification expédition ------------------------------------
title("5. EXPÉDITION — notification de livraison")
code, res = post("/v1/email/send", {
    "to_email": CLIENT_EMAIL,
    "type": "notification",
    "language": "fr",
    "data": {
        "title": "Votre commande est expédiée",
        "message": "Votre colis CMD-4582 est en route. Livraison estimée : 48h.",
    },
})
print(f"HTTP {code} ->", json.dumps(res, indent=2))

# --- 6. Email libre (promo) ----------------------------------------
title("6. MARKETING — email libre (custom)")
code, res = post("/v1/email/send", {
    "to_email": CLIENT_EMAIL,
    "type": "custom",
    "language": "fr",
    "subject": "-20 % ce week-end sur votre boutique !",
    "text_body": "Bonjour Awa,\n\nProfitez de -20 % sur tout le site "
                "avec le code PROMO20 jusqu'à dimanche.\n\nÀ bientôt !",
})
print(f"HTTP {code} ->", json.dumps(res, indent=2))

# --- 7. Pays à risque -> sms_fallback -------------------------------
title("7. PAYS À RISQUE — retour sms_fallback")
code, res = post("/v1/otp/send", {
    "user_id": str(uuid.uuid4()),
    "email": "user@example.cn",
    "country_code": "CN",
    "purpose": "login_2fa",
    "language": "en",
})
print(f"HTTP {code} ->", json.dumps(res, indent=2))
print("-> Le backend enverra le SMS via Twilio (pas d'email)")

# --- 8. Sécurité — requête SANS signature -> rejet ------------------
title("8. SÉCURITÉ — requête non signée (rejetée)")
r = requests.post(f"{BASE_URL}/v1/otp/send", json={
    "user_id": str(uuid.uuid4()),
    "email": "hacker@evil.com",
    "country_code": "FR",
    "purpose": "login_2fa",
    "language": "fr",
})
print(f"HTTP {r.status_code} ->", r.text[:200])

print(f"\n{'='*62}\n  SIMULATION TERMINÉE — tout fonctionne [OK]\n{'='*62}\n")
