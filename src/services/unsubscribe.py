"""Lien de désinscription signé — opt-out destinataire par tenant.

Un clic sur le lien (ou un POST RFC 8058 `List-Unsubscribe=One-Click`)
ajoute l'adresse à la suppression list du tenant : les envois suivants
sont refusés en amont (422) — réputation d'envoi préservée.

La signature HMAC-SHA256(tenant_id|email, secret plateforme) garantit
qu'on ne peut désinscrire que via un lien réellement émis par le service —
pas de suppression arbitraire d'adresses tierces.
"""

import hashlib
import hmac
from urllib.parse import quote


def unsubscribe_sig(settings, tenant_id: str, email: str) -> str:
    secret = settings.hmac_secret
    key = secret if isinstance(secret, bytes) else secret.encode("utf-8")
    msg = f"{tenant_id}|{email.strip().lower()}".encode("utf-8")
    # 128 bits tronqués : token capacité suffisant pour un opt-out.
    return hmac.new(key, msg, hashlib.sha256).hexdigest()[:32]


def verify_sig(settings, tenant_id: str, email: str, sig: str) -> bool:
    return hmac.compare_digest(
        unsubscribe_sig(settings, tenant_id, email), sig or ""
    )


def unsubscribe_url(settings, tenant_id: str, email: str) -> str:
    """URL signée `/v1/unsubscribe?…` — "" si PUBLIC_BASE_URL absent."""
    base = (settings.public_base_url or "").rstrip("/")
    if not base:
        return ""
    e = email.strip().lower()
    sig = unsubscribe_sig(settings, tenant_id, e)
    return f"{base}/v1/unsubscribe?t={tenant_id}&e={quote(e)}&sig={sig}"
