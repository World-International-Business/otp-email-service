"""Test rapide : vérifier un OTP depuis la ligne de commande.

Usage :
    python examples/verify_otp.py req_xxxxxxxx 123456
"""

import hashlib
import hmac
import json
import sys
import time

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
    if len(sys.argv) < 3:
        print("Usage: python examples/verify_otp.py <request_id> <code>")
        sys.exit(1)

    request_id, code = sys.argv[1], sys.argv[2]
    payload = {"request_id": request_id, "code": code}
    body = json.dumps(payload).encode()

    resp = requests.post(
        f"{BASE_URL}/v1/otp/verify",
        data=body,
        headers=signed_headers(body),
        timeout=10,
    )
    print(f"HTTP {resp.status_code}")
    print(json.dumps(resp.json(), indent=2))


if __name__ == "__main__":
    main()
