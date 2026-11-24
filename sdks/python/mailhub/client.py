"""MailHub — client Python officiel (stdlib pure, aucune dépendance).

    from mailhub import MailHubClient

    client = MailHubClient(
        base_url="https://mail.maboite.com",
        api_key="mk_...",
        hmac_secret="whsec_...",
    )
    res = client.send_otp(
        user_id="usr_123", email="user@ex.com",
        country_code="FR", purpose="login_2fa", language="fr",
    )
    ok = client.verify_otp(res["request_id"], "123456")

Signature : hex(HMAC_SHA256(secret, body + timestamp [+ "|" + nonce])).
Un X-Nonce aléatoire est envoyé sur chaque requête — deux appels
identiques dans la même seconde ne partagent jamais de signature.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
import uuid

__version__ = "1.0.0"


class MailHubError(Exception):
    """Erreur API : status HTTP + détail du serveur."""

    def __init__(self, status: int, detail):
        self.status = status
        self.detail = detail
        super().__init__(f"MailHub API error {status}: {detail}")


class MailHubClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        hmac_secret: str,
        *,
        timeout: float = 15.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._secret = (
            hmac_secret.encode("utf-8")
            if isinstance(hmac_secret, str) else hmac_secret
        )
        self.timeout = timeout

    # ------------------------------------------------------------- core

    def _sign(self, body: bytes) -> dict[str, str]:
        ts = str(int(time.time()))
        nonce = uuid.uuid4().hex
        msg = body + ts.encode() + b"|" + nonce.encode()
        sig = hmac.new(self._secret, msg, hashlib.sha256).hexdigest()
        return {
            "X-API-Key": self.api_key,
            "X-Timestamp": ts,
            "X-Nonce": nonce,
            "X-HMAC-Signature": sig,
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> dict:
        # Le serveur signe le CORPS BRUT : GET = corps vide (jamais "{}").
        body = (
            json.dumps(payload).encode("utf-8")
            if payload is not None else b""
        )
        hdrs = self._sign(body)
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body if method != "GET" else None,
            headers=hdrs, method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("detail")
            except Exception:
                detail = exc.reason
            raise MailHubError(exc.code, detail) from exc
        except urllib.error.URLError as exc:
            raise MailHubError(0, str(exc.reason)) from exc

    # ------------------------------------------------------------- API

    def health(self) -> dict:
        """GET /health — pas d'authentification."""
        req = urllib.request.Request(f"{self.base_url}/health")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def send_otp(
        self,
        *,
        user_id: str,
        email: str,
        country_code: str,
        purpose: str,
        language: str = "en",
        magic_link: bool = False,
        idempotency_key: str | None = None,
    ) -> dict:
        """POST /v1/otp/send → {channel, request_id, expires_in}."""
        payload = {
            "user_id": user_id,
            "email": email,
            "country_code": country_code,
            "purpose": purpose,
            "language": language,
        }
        if magic_link:
            payload["magic_link"] = True
        headers = (
            {"Idempotency-Key": idempotency_key} if idempotency_key else None
        )
        return self._request(
            "POST", "/v1/otp/send", payload, headers=headers)

    def verify_otp(self, request_id: str, code: str) -> dict:
        """POST /v1/otp/verify → {verified, attempts_remaining}."""
        return self._request(
            "POST", "/v1/otp/verify",
            {"request_id": request_id, "code": code},
        )

    def send_email(
        self,
        *,
        to_email: str,
        type: str,
        data: dict | None = None,
        language: str = "en",
        subject: str | None = None,
        text_body: str | None = None,
        html_body: str | None = None,
        send_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        """POST /v1/email/send → {sent, status, message_id, scheduled_for?}.

        type : welcome | notification | transaction | custom
        custom : subject + text_body obligatoires, html_body optionnel.
        send_at : ISO 8601, envoi programmé (≤ 30 jours).
        """
        payload: dict = {
            "to_email": to_email, "type": type, "language": language,
        }
        if data is not None:
            payload["data"] = data
        if subject is not None:
            payload["subject"] = subject
        if text_body is not None:
            payload["text_body"] = text_body
        if html_body is not None:
            payload["html_body"] = html_body
        if send_at is not None:
            payload["send_at"] = send_at
        headers = (
            {"Idempotency-Key": idempotency_key} if idempotency_key else None
        )
        return self._request(
            "POST", "/v1/email/send", payload, headers=headers)

    def email_status(self, message_id: str) -> dict:
        """GET /v1/email/status/{id} → {status, attempts, error, ...}."""
        return self._request("GET", f"/v1/email/status/{message_id}")
