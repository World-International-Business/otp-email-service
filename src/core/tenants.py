"""Modèle Tenant et stockage multi-tenant dans Redis.

Chaque tenant = un client/application qui utilise le service :
- Sa propre clé API (hashée) et son propre secret HMAC
- Son branding (nom, expéditeur, logo)
- Ses règles (pays à risque, rollout, purposes autorisés, quotas)
"""

import hashlib
import json
import secrets
from typing import Any

from pydantic import BaseModel, Field


class Tenant(BaseModel):
    tenant_id: str
    name: str
    api_key_hash: str
    hmac_secret: str
    brand_name: str = "SendBID"
    from_email: str | None = None  # None = fallback global
    from_name: str | None = None
    smtp_host: str | None = None   # None = fallback global
    smtp_port: int | None = None
    smtp_user: str | None = None   # None = fallback global
    smtp_password: str | None = None
    smtp_use_ssl: bool | None = None  # None = auto (SSL si port 465)
    risky_countries: list[str] = Field(default_factory=list)
    rollout_percentage: int = Field(default=100, ge=0, le=100)
    allowed_purposes: list[str] = Field(default_factory=lambda: ["login_2fa"])
    plan: str = "free"
    max_sends_per_user_per_10min: int = 3
    max_sends_per_ip_per_10min: int = 3
    cooldown_seconds: int = 60
    max_verify_attempts: int = 5
    otp_ttl_seconds: int = 300
    is_active: bool = True

    def key_prefix(self) -> str:
        return f"t:{self.tenant_id}:"


def generate_api_key() -> str:
    return secrets.token_urlsafe(48)


def generate_hmac_secret() -> str:
    return secrets.token_urlsafe(48)


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------
# Plans d'abonnement (déclaratifs) — les quotas du plan sont appliqués
# aux champs du tenant lors du changement de plan ; l'admin peut ensuite
# ajuster chaque quota individuellement.
# ---------------------------------------------------------------------

PLANS: dict[str, dict] = {
    "free": {
        "label": "Starter",
        "price": "0€",
        "desc": "Pour tester et prototyper",
        "max_sends_per_user_per_10min": 3,
        "max_sends_per_ip_per_10min": 3,
        "cooldown_seconds": 60,
        "max_verify_attempts": 5,
        "otp_ttl_seconds": 300,
    },
    "pro": {
        "label": "Pro",
        "price": "Sur mesure",
        "desc": "Pour les apps en production",
        "max_sends_per_user_per_10min": 20,
        "max_sends_per_ip_per_10min": 60,
        "cooldown_seconds": 10,
        "max_verify_attempts": 10,
        "otp_ttl_seconds": 300,
    },
    "enterprise": {
        "label": "Enterprise",
        "price": "Sur mesure",
        "desc": "Volume élevé & conformité",
        "max_sends_per_user_per_10min": 100,
        "max_sends_per_ip_per_10min": 300,
        "cooldown_seconds": 1,
        "max_verify_attempts": 10,
        "otp_ttl_seconds": 600,
    },
}

# Champs du tenant pilotés par le plan
PLAN_QUOTA_FIELDS = (
    "max_sends_per_user_per_10min",
    "max_sends_per_ip_per_10min",
    "cooldown_seconds",
    "max_verify_attempts",
    "otp_ttl_seconds",
)


def plan_quota_updates(plan: str) -> dict:
    """Quotas du plan sous forme de dict pour TenantStore.update()."""
    p = PLANS.get(plan) or PLANS["free"]
    return {f: p[f] for f in PLAN_QUOTA_FIELDS}


class TenantStore:
    """Registre des tenants dans Redis.

    Clés :
      tenant:{tenant_id}            -> JSON du tenant
      tenant:bykey:{api_key_hash}   -> tenant_id
      tenant:index                  -> set des tenant_id
    """

    def __init__(self, redis):
        self.redis = redis

    def _tenant_key(self, tenant_id: str) -> str:
        return f"tenant:{tenant_id}"

    def _bykey(self, api_key_hash: str) -> str:
        return f"tenant:bykey:{api_key_hash}"

    async def create(self, tenant: Tenant) -> Tenant:
        pipe = self.redis.pipeline()
        pipe.set(self._tenant_key(tenant.tenant_id), tenant.model_dump_json())
        pipe.set(self._bykey(tenant.api_key_hash), tenant.tenant_id)
        pipe.sadd("tenant:index", tenant.tenant_id)
        await pipe.execute()
        return tenant

    async def get(self, tenant_id: str) -> Tenant | None:
        raw = await self.redis.get(self._tenant_key(tenant_id))
        if raw is None:
            return None
        return Tenant.model_validate(json.loads(raw))

    async def get_by_api_key(self, api_key: str) -> Tenant | None:
        api_key_hash = hash_api_key(api_key)
        tenant_id = await self.redis.get(self._bykey(api_key_hash))
        if tenant_id is None:
            return None
        return await self.get(tenant_id)

    async def list(self) -> list[Tenant]:
        ids = await self.redis.smembers("tenant:index")
        tenants = []
        for tid in sorted(ids):
            t = await self.get(tid)
            if t:
                tenants.append(t)
        return tenants

    async def update(self, tenant_id: str, updates: dict[str, Any]) -> Tenant | None:
        """Applique `updates` tel quel — un `None` explicite efface le champ.

        Les appelants qui veulent « champ vide = inchangé » doivent exclure
        les clés du dict plutôt que passer None.
        """
        tenant = await self.get(tenant_id)
        if tenant is None:
            return None
        data = tenant.model_dump()
        data.update(updates)
        updated = Tenant.model_validate(data)
        await self.redis.set(self._tenant_key(tenant_id), updated.model_dump_json())
        return updated

    async def deactivate(self, tenant_id: str) -> bool:
        updated = await self.update(tenant_id, {"is_active": False})
        return updated is not None


async def seed_default_tenant(redis, settings) -> Tenant:
    """Crée le tenant par défaut depuis les variables d'env (rétrocompatibilité)."""
    store = TenantStore(redis)
    api_key_hash = hash_api_key(settings.api_key)
    hmac_secret = (
        settings.hmac_secret.decode("utf-8")
        if isinstance(settings.hmac_secret, bytes)
        else settings.hmac_secret
    )

    existing = await store.get("default")
    if existing:
        # Le tenant "default" reflète la config .env : on re-synchronise à
        # chaque démarrage les champs issus de l'env (sinon SMTP_USER,
        # FROM_*, quotas et pays à risque restent figés à la première
        # exécution). Les champs non issus de l'env — allowed_purposes,
        # is_active, smtp_host/port/use_ssl — restent modifiables par
        # l'admin et ne sont pas écrasés.
        if existing.api_key_hash != api_key_hash:
            await redis.delete(store._bykey(existing.api_key_hash))
            await redis.set(store._bykey(api_key_hash), existing.tenant_id)
        await store.update(
            existing.tenant_id,
            {
                "api_key_hash": api_key_hash,
                "hmac_secret": hmac_secret,
                "brand_name": settings.from_name,
                "from_email": settings.from_email,
                "from_name": settings.from_name,
                "smtp_user": settings.smtp_user,
                "smtp_password": settings.smtp_password,
                "risky_countries": sorted(settings.risky_countries_set),
                "rollout_percentage": settings.rollout_percentage,
                "max_sends_per_user_per_10min": settings.max_sends_per_user_per_10min,
                "max_sends_per_ip_per_10min": settings.max_sends_per_ip_per_10min,
                "cooldown_seconds": settings.cooldown_seconds,
                "max_verify_attempts": settings.max_verify_attempts,
                "otp_ttl_seconds": settings.otp_ttl_seconds,
            },
        )
        return await store.get(existing.tenant_id)

    tenant = Tenant(
        tenant_id="default",
        name="Default tenant (env config)",
        api_key_hash=api_key_hash,
        hmac_secret=hmac_secret,
        brand_name=settings.from_name,
        from_email=settings.from_email,
        from_name=settings.from_name,
        smtp_user=settings.smtp_user,
        smtp_password=settings.smtp_password,
        risky_countries=sorted(settings.risky_countries_set),
        rollout_percentage=settings.rollout_percentage,
        allowed_purposes=["login_2fa"],
        max_sends_per_user_per_10min=settings.max_sends_per_user_per_10min,
        max_sends_per_ip_per_10min=settings.max_sends_per_ip_per_10min,
        cooldown_seconds=settings.cooldown_seconds,
        max_verify_attempts=settings.max_verify_attempts,
        otp_ttl_seconds=settings.otp_ttl_seconds,
        is_active=True,
    )
    await store.create(tenant)
    return tenant
