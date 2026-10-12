"""Authentification multi-tenant : clé API tenant + HMAC-SHA256 + timestamp.

- X-API-Key identifie le tenant (lookup par hash dans le registre Redis)
- X-HMAC-Signature = HMAC-SHA256(body + timestamp, tenant.hmac_secret)
- X-Admin-Key pour les routes /v1/admin/* (clé maître, variable ADMIN_API_KEY)
"""

import hashlib
import hmac
import time
from typing import Callable

from fastapi import Depends, Header, HTTPException, Request
from fastapi.security import APIKeyHeader

from src.config import Settings, get_settings
from src.core.tenants import Tenant, TenantStore

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
admin_key_header = APIKeyHeader(name="X-Admin-Key", auto_error=False)


def _signature_for_body_and_timestamp(body: bytes, timestamp: str, secret: str) -> str:
    message = body + timestamp.encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _verify_hmac(body: bytes, timestamp: str, signature: str, secret: str) -> bool:
    expected = _signature_for_body_and_timestamp(body, timestamp, secret)
    return hmac.compare_digest(expected, signature)


async def resolve_tenant(request: Request) -> Tenant:
    """Résout le tenant appelant et valide sa signature HMAC."""
    api_key = request.headers.get("X-API-Key", "")
    timestamp = request.headers.get("X-Timestamp", "")
    signature = request.headers.get("X-HMAC-Signature", "")

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
    if not _verify_hmac(body, timestamp, signature, tenant.hmac_secret):
        raise HTTPException(status_code=403, detail="Invalid signature")

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
    if not admin_key or not hmac.compare_digest(admin_key, settings.admin_api_key):
        raise HTTPException(status_code=401, detail="Unauthorized")


# Alias utilisable sur les routers
require_service_auth: Callable = Depends(get_tenant)
require_admin: Callable = Depends(verify_admin)
