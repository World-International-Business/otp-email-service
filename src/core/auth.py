"""Authentification multi-tenant : clé API tenant + HMAC-SHA256 + timestamp.

- X-API-Key identifie le tenant (lookup par hash dans le registre Redis)
- X-HMAC-Signature = HMAC-SHA256(body + timestamp, tenant.hmac_secret)
- X-Admin-Key pour les routes /v1/admin/* (clé maître, variable ADMIN_API_KEY)
"""

import hashlib
import hmac
import time
from typing import Callable

from fastapi import Depends, HTTPException, Request
from fastapi.security import APIKeyHeader

from src.config import Settings, get_settings
from src.core.security import hash_code
from src.core.tenants import Tenant, TenantStore

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
admin_key_header = APIKeyHeader(name="X-Admin-Key", auto_error=False)


def _signature_for_body_and_timestamp(
    body: bytes, timestamp: str, secret: str, nonce: str = ""
) -> str:
    # X-Nonce (optionnel, rétrocompatible) : sans lui, deux requêtes au
    # corps identique dans la même seconde produisent la même signature
    # et la seconde est rejetée comme replay — ex. un client qui poll un
    # statut GET. Un nonce unique par requête lève cette contrainte.
    message = body + timestamp.encode("utf-8")
    if nonce:
        message += b"|" + nonce.encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _verify_hmac(
    body: bytes, timestamp: str, signature: str, secret: str, nonce: str = ""
) -> bool:
    expected = _signature_for_body_and_timestamp(body, timestamp, secret, nonce)
    return hmac.compare_digest(expected, signature)


async def resolve_tenant(request: Request) -> Tenant:
    """Résout le tenant appelant et valide sa signature HMAC."""
    api_key = request.headers.get("X-API-Key", "")
    timestamp = request.headers.get("X-Timestamp", "")
    signature = request.headers.get("X-HMAC-Signature", "")
    nonce = request.headers.get("X-Nonce", "")

    if not api_key or not timestamp or not signature:
        raise HTTPException(status_code=401, detail="Not authenticated")

    store = TenantStore(request.app.state.redis)
    tenant = await store.get_by_api_key(api_key)

    if tenant is None or not tenant.is_active:
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        ts = int(timestamp)
    except ValueError:
        raise HTTPException(status_code=403, detail="Invalid timestamp")

    if abs(int(time.time()) - ts) > 300:
        raise HTTPException(status_code=403, detail="Timestamp too old")

    body = await request.body()
    if not _verify_hmac(body, timestamp, signature, tenant.hmac_secret, nonce):
        raise HTTPException(status_code=403, detail="Invalid signature")

    # Anti-rejeu : une signature n'est utilisable qu'une seule fois dans la
    # fenêtre ±5 min. Un client légitime génère un nouveau timestamp à chaque
    # requête, donc sa signature change toujours.
    nonce_key = f"{tenant.key_prefix()}nonce:{hash_code(signature)}"
    fresh = await request.app.state.redis.set(nonce_key, "1", nx=True, ex=310)
    if not fresh:
        raise HTTPException(status_code=403, detail="Replay detected")

    return tenant


async def get_tenant(request: Request) -> Tenant:
    """Dépendance FastAPI : tenant authentifié ou 401/403."""
    tenant = await resolve_tenant(request)
    request.state.tenant = tenant
    return tenant


async def verify_admin(
    admin_key: str = Depends(admin_key_header),
    settings: Settings = Depends(get_settings),
) -> None:
    """Authentification des routes d'administration des tenants."""
    # compare_digest sur bytes : accepte toute entrée (les str non-ASCII
    # lèvent TypeError sinon -> 500 / DoS trivial via header forgé).
    if (
        not settings.admin_api_key
        or not admin_key
        or not hmac.compare_digest(
            admin_key.encode("utf-8", "ignore"), settings.admin_api_key.encode()
        )
    ):
        raise HTTPException(status_code=401, detail="Unauthorized")


# Alias utilisable sur les routers
require_service_auth: Callable = Depends(get_tenant)
require_admin: Callable = Depends(verify_admin)
