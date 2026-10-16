"""Routes d'administration des tenants — protégées par X-Admin-Key."""

from fastapi import APIRouter, HTTPException, Request

from src.core.auth import require_admin
from src.core.models import (
    TenantCreateRequest,
    TenantCreateResponse,
    TenantInfoResponse,
    TenantUpdateRequest,
)
from src.core.tenants import (
    Tenant,
    TenantStore,
    generate_api_key,
    generate_hmac_secret,
    hash_api_key,
)

admin_router = APIRouter(prefix="/v1/admin", dependencies=[require_admin])


def _info(t: Tenant) -> TenantInfoResponse:
    return TenantInfoResponse(
        tenant_id=t.tenant_id,
        name=t.name,
        brand_name=t.brand_name,
        from_email=t.from_email,
        rollout_percentage=t.rollout_percentage,
        allowed_purposes=t.allowed_purposes,
        webhook_url=t.webhook_url,
        is_active=t.is_active,
    )


@admin_router.post(
    "/tenants",
    response_model=TenantCreateResponse,
    summary="Créer un tenant (nouveau client de l'API)",
)
async def create_tenant(
    payload: TenantCreateRequest, request: Request
) -> TenantCreateResponse:
    store = TenantStore(request.app.state.redis)
    if await store.get(payload.tenant_id):
        raise HTTPException(status_code=409, detail="tenant_id already exists")

    api_key = generate_api_key()
    hmac_secret = generate_hmac_secret()

    tenant = Tenant(
        tenant_id=payload.tenant_id,
        name=payload.name,
        api_key_hash=hash_api_key(api_key),
        hmac_secret=hmac_secret,
        brand_name=payload.brand_name,
        from_email=str(payload.from_email) if payload.from_email else None,
        from_name=payload.from_name,
        smtp_user=payload.smtp_user,
        smtp_password=payload.smtp_password,
        risky_countries=[c.upper() for c in payload.risky_countries],
        rollout_percentage=payload.rollout_percentage,
        allowed_purposes=payload.allowed_purposes,
        webhook_url=payload.webhook_url,
        max_sends_per_user_per_10min=payload.max_sends_per_user_per_10min,
        max_sends_per_ip_per_10min=payload.max_sends_per_ip_per_10min,
        cooldown_seconds=payload.cooldown_seconds,
        max_verify_attempts=payload.max_verify_attempts,
        otp_ttl_seconds=payload.otp_ttl_seconds,
        is_active=True,
    )
    await store.create(tenant)

    return TenantCreateResponse(
        tenant_id=tenant.tenant_id,
        api_key=api_key,
        hmac_secret=hmac_secret,
    )


@admin_router.get(
    "/tenants",
    response_model=list[TenantInfoResponse],
    summary="Lister tous les tenants",
)
async def list_tenants(request: Request) -> list[TenantInfoResponse]:
    store = TenantStore(request.app.state.redis)
    tenants = await store.list()
    return [_info(t) for t in tenants]


@admin_router.get(
    "/tenants/{tenant_id}",
    response_model=TenantInfoResponse,
    summary="Détails d'un tenant",
)
async def get_tenant(tenant_id: str, request: Request) -> TenantInfoResponse:
    store = TenantStore(request.app.state.redis)
    tenant = await store.get(tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return _info(tenant)


@admin_router.patch(
    "/tenants/{tenant_id}",
    response_model=TenantInfoResponse,
    summary="Modifier la config d'un tenant",
)
async def update_tenant(
    tenant_id: str, payload: TenantUpdateRequest, request: Request
) -> TenantInfoResponse:
    store = TenantStore(request.app.state.redis)
    updates = payload.model_dump(exclude_none=True)
    if "from_email" in updates and updates["from_email"]:
        updates["from_email"] = str(updates["from_email"])
    if "risky_countries" in updates and updates["risky_countries"]:
        updates["risky_countries"] = [c.upper() for c in updates["risky_countries"]]

    tenant = await store.update(tenant_id, updates)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return _info(tenant)


@admin_router.delete(
    "/tenants/{tenant_id}",
    summary="Désactiver un tenant",
)
async def deactivate_tenant(tenant_id: str, request: Request):
    store = TenantStore(request.app.state.redis)
    ok = await store.deactivate(tenant_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return {"deactivated": True, "tenant_id": tenant_id}
