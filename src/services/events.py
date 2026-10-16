"""Journal d'événements par tenant — liste Redis bornée pour l'espace admin.

Chaque tenant a une clé ``t:{tenant_id}:events`` contenant les N dernières
interactions (envois OTP, vérifications, emails, blocages rate-limit…).
"""

import json

from src.core.security import now_utc
from src.core.tenants import Tenant

EVENTS_MAX = 200


def mask_email(email: str) -> str:
    """Masque une adresse email pour les logs admin : te***@domaine.com."""
    local, sep, domain = email.partition("@")
    if not sep:
        return "***"
    return f"{local[:2]}***@{domain}"


async def log_event(redis, tenant: Tenant, type_: str, **fields) -> None:
    """Ajoute un événement en tête de liste (plafonné à EVENTS_MAX)."""
    entry = {"ts": now_utc().isoformat(timespec="seconds"), "type": type_, **fields}
    key = f"{tenant.key_prefix()}events"
    pipe = redis.pipeline()
    pipe.lpush(key, json.dumps(entry, default=str, ensure_ascii=False))
    pipe.ltrim(key, 0, EVENTS_MAX - 1)
    await pipe.execute()


async def get_events(redis, tenant: Tenant, limit: int = 50) -> list[dict]:
    """Retourne les `limit` derniers événements du tenant (plus récent d'abord)."""
    raw = await redis.lrange(f"{tenant.key_prefix()}events", 0, limit - 1)
    return [json.loads(e) for e in raw]
