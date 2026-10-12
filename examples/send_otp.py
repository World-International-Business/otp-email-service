"""Test rapide : envoyer un OTP depuis la ligne de commande.

Usage :
    python examples/send_otp.py test@example.com FR fr
    python examples/send_otp.py test@example.com CN fr   # → sms_fallback
"""

import hashlib
import hmac
import json
import sys
import time
import uuid

import requests

BASE_URL = "http://localhost:8001"
API_KEY = "test_api_key_64_chars_long_change_in_production_test_api_key_change"
HMAC_SECRET = b"test_hmac_secret_change_in_production"


def signed_headers(body: bytes) -> dict:
    ts = str(int(time.time()))
    sig = hmac.new(HMAC_SECRET, body + ts.encode(), hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-API-Key": API_KEY,
        "X-Timestamp": ts,
        "X-HMAC-Signature": sig,
    }


def main():
    email = sys.argv[1] if len(sys.argv) > 1 else "test@example.com"
    country = sys.argv[2] if len(sys.argv) > 2 else "FR"
    language = sys.argv[3] if len(sys.argv) > 3 else "fr"

    payload = {
        "user_id": str(uuid.uuid4()),
        "email": email,
        "country_code": country,
        "purpose": "login_2fa",
        "language": language,
    }
    body = json.dumps(payload).encode()

    resp = requests.post(
        f"{BASE_URL}/v1/otp/send",
        data=body,
        headers=signed_headers(body),
        timeout=10,
    )
    print(f"HTTP {resp.status_code}")
    print(json.dumps(resp.json(), indent=2))

    if resp.status_code == 200 and resp.json().get("request_id"):
        print("\n→ Regarde les logs du serveur pour voir l'email (provider console).")
        print("→ Puis vérifie avec :")
        print(
            f'   python examples/verify_otp.py {resp.json()["request_id"]} <code>'
        )


if __name__ == "__main__":
    main()
