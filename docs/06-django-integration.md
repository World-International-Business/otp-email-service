# 6. Intégration Django — otp-email-service

Ce document fournit le code exact à copier dans le backend Django de SendBID/PayBID.

---

## 1. Variables d'environnement à ajouter (Django)

```env
# .env du backend Django
OTP_SERVICE_URL=https://otp.sendbid.example.com
OTP_SERVICE_API_KEY=<clé API 64 chars — la même que côté microservice>
OTP_SERVICE_HMAC_SECRET=<secret HMAC — le même que côté microservice>
OTP_SERVICE_TIMEOUT=10
```

---

## 2. Client HTTP sécurisé (`apps/core/otp_client.py`)

```python
"""Client HTTP sécurisé vers otp-email-service (API key + HMAC + timestamp)."""

import hashlib
import hmac
import json
import time

import requests
from django.conf import settings


class OTPServiceError(Exception):
    """Erreur de communication avec le microservice OTP."""


class OTPClient:
    BASE_URL = getattr(settings, "OTP_SERVICE_URL", "http://localhost:8001")
    API_KEY = getattr(settings, "OTP_SERVICE_API_KEY", "")
    HMAC_SECRET = getattr(settings, "OTP_SERVICE_HMAC_SECRET", "").encode()
    TIMEOUT = getattr(settings, "OTP_SERVICE_TIMEOUT", 10)

    @classmethod
    def _headers(cls, body: bytes) -> dict:
        timestamp = str(int(time.time()))
        signature = hmac.new(
            cls.HMAC_SECRET,
            body + timestamp.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {
            "Content-Type": "application/json",
            "X-API-Key": cls.API_KEY,
            "X-Timestamp": timestamp,
            "X-HMAC-Signature": signature,
        }

    @classmethod
    def _post(cls, path: str, payload: dict) -> dict:
        body = json.dumps(payload).encode("utf-8")
        try:
            resp = requests.post(
                f"{cls.BASE_URL}{path}",
                data=body,
                headers=cls._headers(body),
                timeout=cls.TIMEOUT,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            raise OTPServiceError(str(exc)) from exc

    @classmethod
    def send(cls, user_id: str, email: str, country_code: str,
             purpose: str = "login_2fa", language: str = "fr") -> dict:
        """Retourne {"channel": "email"|"sms_fallback", "request_id": str, ...}"""
        return cls._post("/v1/otp/send", {
            "user_id": user_id,
            "email": email,
            "country_code": country_code,
            "purpose": purpose,
            "language": language,
        })

    @classmethod
    def verify(cls, request_id: str, code: str) -> dict:
        """Retourne {"verified": bool, "attempts_remaining": int}"""
        return cls._post("/v1/otp/verify", {
            "request_id": request_id,
            "code": code,
        })
```

---

## 3. Modification de la vue existante (envoi 2FA)

Remplace **uniquement** la partie qui appelle Twilio aujourd'hui :

```python
# apps/accounts/views.py  (exemple — adapter aux noms existants)

from django.http import JsonResponse
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated

from apps.core.otp_client import OTPClient, OTPServiceError
from .twilio_utils import send_sms_otp  # fonction Twilio existante


class SendLoginOTPView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user

        try:
            result = OTPClient.send(
                user_id=str(user.id),
                email=user.email,
                country_code=user.profile.country_code,
                purpose="login_2fa",
                language=getattr(user.profile, "language", "fr"),
            )
        except OTPServiceError:
            # Fallback sécurité : si le microservice est down, on garde Twilio
            code, request_id = send_sms_otp(user)
            return JsonResponse({
                "channel": "sms",
                "request_id": request_id,
                "message": "Code envoyé par SMS",
            })

        if result["channel"] == "sms_fallback":
            # Pays à risque ou hors rollout → SMS Twilio comme avant
            code, request_id = send_sms_otp(user)
            return JsonResponse({
                "channel": "sms",
                "request_id": request_id,
                "message": "Code envoyé par SMS",
            })

        # channel == "email" : l'OTP est géré par le microservice.
        # IMPORTANT : on stocke le request_id lié à l'utilisateur
        # pour le retrouver à l'étape de vérification.
        request.session["otp_request_id"] = result["request_id"]
        return JsonResponse({
            "channel": "email",
            "expires_in": result["expires_in"],
            "message": "Code envoyé par email",
        })
```

```python
# apps/accounts/views.py — vérification

class VerifyLoginOTPView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        code = request.data.get("code", "")
        request_id = request.session.get("otp_request_id")

        if request_id:
            # OTP envoyé par email → vérification microservice
            try:
                result = OTPClient.verify(request_id, code)
            except OTPServiceError:
                return JsonResponse(
                    {"verified": False, "error": "Service indisponible"},
                    status=503,
                )
            if result["verified"]:
                # Marquer l'utilisateur comme 2FA-validé (session, JWT, etc.)
                request.session["twofa_verified"] = True
            return JsonResponse(result)

        # Sinon : OTP SMS Twilio → logique de vérification existante
        ok = verify_twilio_otp(request.user, code)  # fonction existante
        return JsonResponse({"verified": ok})
```

---

## 4. Résumé de la logique côté Django

| Réponse du microservice | Action backend |
|-------------------------|----------------|
| `channel: "email"` + `request_id` | Stocker `request_id` en session ; attendre le code de l'utilisateur |
| `channel: "sms_fallback"` | Appeler Twilio exactement comme avant |
| Erreur réseau / timeout | Fallback Twilio (le 2FA ne doit jamais être bloqué par le microservice) |
| `verified: true` | Valider la session 2FA |
| `verified: false` + `attempts_remaining` | Réafficher le formulaire avec le nombre de tentatives restantes |

---

## 5. Points d'attention pour la mise en prod

1. **Ne jamais appeler le microservice depuis le mobile** — uniquement depuis le backend Django.
2. **Firewall** : n'autoriser que l'IP du backend Django vers le port du service (Nginx `allow/deny` ou security group).
3. **Secrets** : `OTP_SERVICE_API_KEY` et `OTP_SERVICE_HMAC_SECRET` identiques des deux côtés, jamais commités.
4. **Rollback instantané** : si problème en prod, mettre `ROLLOUT_PERCENTAGE=0` côté microservice — tous les appels repassent en SMS sans toucher au code Django.
5. **Monitoring** : surveiller `GET /health` et les logs `otp_email_sent` / `otp_verification_attempt`.
