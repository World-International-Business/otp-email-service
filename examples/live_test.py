"""Test de bout en bout de l'API déployée sur https://mail.aiforge.world.

Les credentials se lisent dans l'environnement (jamais en clair dans le repo) :

    export OTP_API_KEY=... OTP_HMAC_SECRET=... OTP_ADMIN_KEY=...
    python examples/live_test.py
"""
import hashlib
import hmac
import json
import os
import socket
import sys
import time
import urllib.request
import uuid

# Forcer IPv4 : un vieil enregistrement AAAA pointe encore vers le parking IONOS
_orig = socket.getaddrinfo
socket.getaddrinfo = lambda *a, **k: [r for r in _orig(*a, **k) if r[0] == socket.AF_INET]

BASE = os.environ.get("OTP_BASE_URL", "https://mail.aiforge.world")
API_KEY = os.environ.get("OTP_API_KEY", "")
HMAC_SECRET = os.environ.get("OTP_HMAC_SECRET", "")
ADMIN_KEY = os.environ.get("OTP_ADMIN_KEY", "")

if not API_KEY or not HMAC_SECRET:
    sys.exit("Définir OTP_API_KEY et OTP_HMAC_SECRET dans l'environnement.")


def call(method, path, body=None, admin=False):
    data = json.dumps(body).encode() if body else b""
    req = urllib.request.Request(BASE + path, data=data or None, method=method)
    if admin:
        req.add_header("X-Admin-Key", ADMIN_KEY)
    else:
        ts = str(int(time.time()))
        sig = hmac.new(HMAC_SECRET.encode(), data + ts.encode(), hashlib.sha256).hexdigest()
        req.add_header("X-API-Key", API_KEY)
        req.add_header("X-Timestamp", ts)
        req.add_header("X-HMAC-Signature", sig)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


print("== /health ==", *call("GET", "/health"))
print("\n== OTP send (FR, safe country) ==")
s, r = call("POST", "/v1/otp/send", {"user_id": str(uuid.uuid4()), "email": "test@example.com",
                                     "country_code": "FR", "purpose": "login_2fa", "language": "fr"})
print(s, r)
rid = r.get("request_id")

print("\n== OTP send (CN -> sms_fallback) ==")
print(*call("POST", "/v1/otp/send", {"user_id": str(uuid.uuid4()), "email": "t@ex.com",
                                     "country_code": "CN", "purpose": "login_2fa", "language": "en"}))

print("\n== OTP send (purpose refusé) ==")
print(*call("POST", "/v1/otp/send", {"user_id": str(uuid.uuid4()), "email": "t@ex.com",
                                     "country_code": "FR", "purpose": "transaction_confirmation"}))

print("\n== OTP verify (mauvais code) ==")
if rid:
    print(*call("POST", "/v1/otp/verify", {"request_id": rid, "code": "000000"}))

print("\n== Sans signature (doit être 401) ==")
req = urllib.request.Request(BASE + "/v1/otp/send", data=b"{}", method="POST",
                             headers={"Content-Type": "application/json"})
try:
    urllib.request.urlopen(req)
except urllib.error.HTTPError as e:
    print(e.code)

print("\n== Admin: liste tenants ==")
print(*call("GET", "/v1/admin/tenants", admin=True))
