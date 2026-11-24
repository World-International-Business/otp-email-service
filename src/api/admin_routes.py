"""Routes d'administration des tenants — protégées par X-Admin-Key."""

import time

from fastapi import APIRouter, HTTPException, Request

from src.api.dependencies import get_client_ip
from src.core.auth import require_admin
from src.core.models import (
    TenantCreateRequest,
    TenantCreateResponse,
    TenantInfoResponse,
    TenantUpdateRequest,
)
from src.core.tenants import (
    KEY_ROTATION_GRACE_SECONDS,
    PLANS,
    Tenant,
    TenantStore,
    generate_api_key,
    generate_hmac_secret,
    hash_api_key,
    plan_quota_updates,
)
from src.services.audit import get_admin_audit, log_admin_action

admin_router = APIRouter(prefix="/v1/admin", dependencies=[require_admin])


def _info(t: Tenant) -> TenantInfoResponse:
    return TenantInfoResponse(
        tenant_id=t.tenant_id,
        name=t.name,
        brand_name=t.brand_name,
        from_email=t.from_email,
        rollout_percentage=t.rollout_percentage,
        allowed_purposes=t.allowed_purposes,
        plan=t.plan,
        brand_color=t.brand_color,
        email_logo_url=t.email_logo_url,
        email_footer=t.email_footer,
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
    if payload.plan not in PLANS:
        raise HTTPException(
            status_code=422, detail=f"Unknown plan '{payload.plan}'"
        )

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
        smtp_host=payload.smtp_host,
        smtp_port=payload.smtp_port,
        smtp_user=payload.smtp_user,
        smtp_password=payload.smtp_password,
        smtp_use_ssl=payload.smtp_use_ssl,
        risky_countries=[c.upper() for c in payload.risky_countries],
        rollout_percentage=payload.rollout_percentage,
        allowed_purposes=payload.allowed_purposes,
        plan=payload.plan,
        brand_color=payload.brand_color,
        email_logo_url=payload.email_logo_url,
        email_footer=payload.email_footer,
        max_sends_per_user_per_10min=payload.max_sends_per_user_per_10min,
        max_sends_per_ip_per_10min=payload.max_sends_per_ip_per_10min,
        cooldown_seconds=payload.cooldown_seconds,
        max_verify_attempts=payload.max_verify_attempts,
        otp_ttl_seconds=payload.otp_ttl_seconds,
        is_active=True,
    )
    if payload.plan != "free":
        # Un plan non gratuit applique ses quotas par défaut (affinables via PATCH)
        for field, value in plan_quota_updates(payload.plan).items():
            setattr(tenant, field, value)
    await store.create(tenant)
    await log_admin_action(
        request.app.state.redis, "tenant.create",
        actor_ip=get_client_ip(request), tenant_id=tenant.tenant_id,
        detail={"plan": payload.plan},
    )

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
    # exclude_unset : seuls les champs fournis sont modifiés ;
    # un null explicite efface le champ (retour au fallback global).
    updates = payload.model_dump(exclude_unset=True)
    if "from_email" in updates and updates["from_email"]:
        updates["from_email"] = str(updates["from_email"])
    if "risky_countries" in updates and updates["risky_countries"]:
        updates["risky_countries"] = [c.upper() for c in updates["risky_countries"]]
    if "plan" in updates:
        if updates["plan"] not in PLANS:
            raise HTTPException(
                status_code=422, detail=f"Unknown plan '{updates['plan']}'"
            )
        # Changer de plan applique les quotas du plan au tenant
        updates.update(plan_quota_updates(updates["plan"]))

    before = await store.get(tenant_id)
    tenant = await store.update(tenant_id, updates)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    # Noms des champs modifiés seulement — jamais les valeurs (smtp_password…)
    detail = {"fields": sorted(payload.model_fields_set)}
    if "plan" in updates and before and before.plan != tenant.plan:
        detail["plan"] = f"{before.plan}->{tenant.plan}"
    await log_admin_action(
        request.app.state.redis, "tenant.update",
        actor_ip=get_client_ip(request), tenant_id=tenant_id, detail=detail,
    )
    return _info(tenant)


@admin_router.post(
    "/tenants/{tenant_id}/rotate-key",
    summary="Régénérer la clé API + secret HMAC d'un tenant",
    response_description="Nouvelles credentials — affichées une seule fois",
)
async def rotate_tenant_key(
    tenant_id: str, request: Request,
    grace_seconds: int = KEY_ROTATION_GRACE_SECONDS,
):
    """Rotation sans coupure : l'ancienne clé et l'ancien secret restent
    valables `grace_seconds` (défaut 24 h, max 7 j ; 0 = invalidation
    immédiate) — le client déploie la nouvelle config avant l'expiration
    de l'ancienne."""
    store = TenantStore(request.app.state.redis)
    tenant, api_key, hmac_secret = await store.rotate_keys(
        tenant_id, grace_seconds=max(0, min(grace_seconds, 7 * 86400))
    )
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    grace = max(0, min(grace_seconds, 7 * 86400))
    await log_admin_action(
        request.app.state.redis, "tenant.rotate_key",
        actor_ip=get_client_ip(request), tenant_id=tenant_id,
        detail={"grace_seconds": grace},
    )
    return {
        "tenant_id": tenant_id,
        "api_key": api_key,
        "hmac_secret": hmac_secret,
        "grace_seconds": grace,
    }


@admin_router.delete(
    "/tenants/{tenant_id}",
    summary="Désactiver un tenant",
)
async def deactivate_tenant(tenant_id: str, request: Request):
    store = TenantStore(request.app.state.redis)
    ok = await store.deactivate(tenant_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Tenant not found")
    await log_admin_action(
        request.app.state.redis, "tenant.deactivate",
        actor_ip=get_client_ip(request), tenant_id=tenant_id,
    )
    return {"deactivated": True, "tenant_id": tenant_id}


@admin_router.get(
    "/audit",
    summary="Journal des actions d'administration",
    response_description="Actions récentes, plus récente d'abord",
)
async def admin_audit(request: Request, limit: int = 100):
    """Traçabilité : qui a suspendu un tenant, changé un plan, roté une clé…"""
    return await get_admin_audit(
        request.app.state.redis, limit=max(1, min(limit, 500)))


# ---------------------------------------------------------------------
# Export RGPD — toutes les données d'un tenant (sans les secrets)
# ---------------------------------------------------------------------

# Champs exclus de l'export : secrets non exportables par design — une
# migration se fait en régénérant les credentials, pas en les copiant.
EXPORT_SECRET_FIELDS = (
    "api_key_hash", "hmac_secret", "hmac_secret_prev", "smtp_password",
)


@admin_router.get(
    "/tenants/{tenant_id}/export",
    summary="Exporter toutes les données d'un tenant (RGPD / migration)",
    response_description=(
        "JSON : config sans secrets + templates + suppressions + stats "
        "+ paiements + comptes web"
    ),
)
async def export_tenant(tenant_id: str, request: Request):
    """Export complet tenant (art. 20 RGPD — portabilité). Permet aussi de
    migrer un branding/templates d'un environnement à un autre : réinjecter
    les champs via PATCH /tenants/{id}. Les secrets (clé API, secret HMAC,
    mot de passe SMTP) ne sont JAMAIS exportés."""
    redis = request.app.state.redis
    store = TenantStore(redis)
    tenant = await store.get(tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")

    from src.services.custom_templates import list_templates
    from src.services.events import monthly_stat
    from src.services.payments import list_payments
    from src.services.quotas import QUOTA_FIELDS
    from src.services.suppression import list_suppressions
    from src.web.users import WebUserStore

    config = tenant.model_dump()
    for f in EXPORT_SECRET_FIELDS:
        config.pop(f, None)

    month_usage = {
        metric: await monthly_stat(redis, tenant, metric)
        for metric in QUOTA_FIELDS
    }
    users = await WebUserStore(redis).list_by_tenant(tenant_id)

    await log_admin_action(
        redis, "tenant.export",
        actor_ip=get_client_ip(request), tenant_id=tenant_id,
    )
    return {
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "secrets_excluded": list(EXPORT_SECRET_FIELDS),
        "tenant": config,
        "templates": await list_templates(redis, tenant_id),
        "suppressions": await list_suppressions(redis, tenant_id),
        "stats": {
            "month": time.strftime("%Y-%m", time.gmtime()),
            "monthly_usage": month_usage,
            "otps_sent_total": int(
                await redis.get(f"{tenant.key_prefix()}stats:otps_sent") or 0),
            "emails_sent_total": int(
                await redis.get(f"{tenant.key_prefix()}stats:emails_sent") or 0),
        },
        "payments": await list_payments(redis, tenant_id, limit=100),
        "web_users": [
            {"email": u.email, "app_name": u.app_name,
             "created_at": u.created_at}
            for u in users
        ],
    }
