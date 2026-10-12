"""Exemple de client Python pour appeler le microservice OTP."""

import hashlib
import hmac
import json
import time
import uuid

import httpx

API_KEY = "test_api_key_64_chars_long_change_in_production_test_api_key_change"
HMAC_SECRET = b"test_hmac_secret_change_in_production"
BASE_URL = "http://localhost:8000"


def sign_body(body: bytes) -> dict[str, str]:
    timestamp = str(int(time.time()))
    message = body + timestamp.encode("utf-8")
    signature = hmac.new(HMAC_SECRET, message, hashlib.sha256).hexdigest()
    return {
        "X-API-Key": API_KEY,
        "X-Timestamp": timestamp,
        "X-HMAC-Signature": signature,
    }


def send_otp(email: str, country_code: str = "FR", language: str = "fr") -> dict:
    payload = {
        "user_id": str(uuid.uuid4()),
        "email": email,
        "country_code": country_code,
        "purpose": "login_2fa",
        "language": language,
    }
    body = json.dumps(payload).encode("utf-8")
    headers = sign_body(body)
    response = httpx.post(f"{BASE_URL}/v1/otp/send", content=body, headers=headers)
    response.raise_for_status()
    return response.json()


def verify_otp(request_id: str, code: str) -> dict:
    payload = {"request_id": request_id, "code": code}
    body = json.dumps(payload).encode("utf-8")
    headers = sign_body(body)
    response = httpx.post(f"{BASE_URL}/v1/otp/verify", content=body, headers=headers)
    response.raise_for_status()
    return response.json()


if __name__ == "__main__":
    result = send_otp("user@example.com", country_code="FR", language="fr")
    print("Send response:", result)

    if result.get("channel") == "email":
        code = input("Entrez le code reçu par email : ")
        print("Verify response:", verify_otp(result["request_id"], code))
