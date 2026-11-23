"""Idempotency-Key — un POST = un seul effet, pendant 24 h.

Le client envoie un header `Idempotency-Key: <clé unique>`. Le premier appel
est exécuté et sa réponse stockée ; tout appel suivant avec la même clé
rejoue la réponse d'origine — un retry réseau ou un double-clic ne produit
jamais un envoi en double (ni un nouvel OTP qui invaliderait le premier).
"""

from __future__ import annotations

import hashlib
import json

TTL = 24 * 3600


def _key(tenant_id: str, idem_key: str) -> str:
    # La clé client est libre (uuid, hash…) : on la borne par SHA-256.
    digest = hashlib.sha256(idem_key.encode()).hexdigest()[:32]
    return f"idem:{tenant_id}:{digest}"


async def idem_get(redis, tenant_id: str, idem_key: str) -> dict | None:
    raw = await redis.get(_key(tenant_id, idem_key))
    return json.loads(raw) if raw else None


async def idem_store(redis, tenant_id: str, idem_key: str, response: dict) -> None:
    await redis.set(
        _key(tenant_id, idem_key),
        json.dumps(response, default=str),
        ex=TTL,
    )
