"""Liste de suppression — ne plus écrire aux adresses bounce/plainte.

Deux niveaux :
- ``t:{tenant_id}:suppress`` — suppression propre au tenant (hash email →
  {reason, ts, source}).
- ``t:*:suppress`` — suppression GLOBALE : un hard bounce signifie que la
  boîte n'existe pas — elle est invalide pour tous les tenants.

Un envoi vers une adresse supprimée est refusé AVANT l'appel SMTP (422) :
chaque email évité vers une boîte morte préserve la réputation d'envoi.
"""

import json
import time

GLOBAL = "*"


def _key(tenant_id: str) -> str:
    return f"t:{tenant_id}:suppress"


async def add_suppression(
    redis, tenant_id: str, email: str, reason: str, source: str = "event"
) -> None:
    e = email.strip().lower()
    if not e or "@" not in e:
        return
    await redis.hset(
        _key(tenant_id), e,
        json.dumps({"reason": reason, "source": source,
                    "ts": int(time.time())}),
    )


async def suppression_reason(redis, tenant_id: str, email: str) -> str | None:
    """Raison de suppression (tenant d'abord, puis globale) — None si clean."""
    e = email.strip().lower()
    raw = await redis.hget(_key(tenant_id), e)
    if raw is None and tenant_id != GLOBAL:
        raw = await redis.hget(_key(GLOBAL), e)
    if raw is None:
        return None
    try:
        return json.loads(raw).get("reason") or "suppressed"
    except (ValueError, AttributeError):
        return str(raw)


async def list_suppressions(redis, tenant_id: str) -> list[dict]:
    """Adresses supprimées du tenant (pour affichage admin)."""
    raw = await redis.hgetall(_key(tenant_id))
    out = []
    for email, meta in raw.items():
        try:
            m = json.loads(meta)
        except (ValueError, TypeError):
            m = {"reason": "suppressed"}
        out.append({"email": email, **m})
    out.sort(key=lambda x: x.get("ts", 0), reverse=True)
    return out


async def remove_suppression(redis, tenant_id: str, email: str) -> bool:
    e = email.strip().lower()
    return bool(await redis.hdel(_key(tenant_id), e))
