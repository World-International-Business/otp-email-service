"""Journal d'audit des actions d'administration — traçabilité plateforme.

Toute action sensible passée par X-Admin-Key (création/suspension de
tenant, changement de plan, rotation de clés, export RGPD…) est consignée
dans ``admin:audit`` — liste bornée, plus récent en tête. L'acteur est
identifié par son IP source (la clé admin est partagée) — jamais la clé
elle-même ni aucun secret dans le détail.
"""

import json
import time

AUDIT_KEY = "admin:audit"
AUDIT_MAX = 500


async def log_admin_action(
    redis,
    action: str,
    *,
    actor_ip: str = "",
    tenant_id: str | None = None,
    detail: dict | None = None,
) -> None:
    """Consigne une action admin. `detail` : champs non sensibles seulement
    (ex. {"plan": "free->pro"}, {"fields": ["smtp_host"]})."""
    entry = {
        "ts": int(time.time()),
        "action": action,
        "actor_ip": actor_ip,
        "tenant_id": tenant_id,
        "detail": detail or {},
    }
    pipe = redis.pipeline()
    pipe.lpush(AUDIT_KEY, json.dumps(entry, ensure_ascii=False))
    pipe.ltrim(AUDIT_KEY, 0, AUDIT_MAX - 1)
    await pipe.execute()


async def get_admin_audit(redis, limit: int = 100) -> list[dict]:
    """Les `limit` dernières actions admin, plus récente d'abord."""
    raw = await redis.lrange(AUDIT_KEY, 0, max(0, limit - 1))
    return [json.loads(e) for e in raw]
