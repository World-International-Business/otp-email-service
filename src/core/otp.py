"""Génération et stockage des OTP dans Redis — isolés par tenant."""

import json
from datetime import datetime, timezone

from redis.asyncio import Redis

from src.core.security import compare_codes, generate_request_id, generate_secure_code, hash_code
from src.core.tenants import Tenant


class OTPStore:
    def __init__(self, redis: Redis, tenant: Tenant):
        self.redis = redis
        self.tenant = tenant
        self.ttl = tenant.otp_ttl_seconds

    def _key(self, request_id: str) -> str:
        return f"{self.tenant.key_prefix()}otp:{request_id}"

    async def create(self, user_id: str) -> tuple[str, str, str]:
        """Crée un OTP et retourne (code, request_id, code_hash)."""
        code = generate_secure_code(6)
        request_id = generate_request_id()
        code_hash = hash_code(code)

        payload = {
            "code_hash": code_hash,
            "user_id_hash": hash_code(user_id),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        await self.redis.set(
            self._key(request_id), json.dumps(payload), ex=self.ttl
        )
        return code, request_id, code_hash

    def _attempts_key(self, request_id: str) -> str:
        return f"{self._key(request_id)}:attempts"

    async def get(self, request_id: str) -> dict | None:
        raw = await self.redis.get(self._key(request_id))
        if raw is None:
            return None
        return json.loads(raw)

    async def delete(self, request_id: str) -> None:
        await self.redis.delete(self._key(request_id), self._attempts_key(request_id))


async def verify_otp(
    redis: Redis,
    tenant: Tenant,
    request_id: str,
    code: str,
) -> tuple[bool, int]:
    """Vérifie un OTP et retourne (succès, tentatives restantes).

    Le compteur de tentatives est incrémenté via INCR (atomique) dans une
    clé séparée alignée sur le TTL de l'OTP : contrairement à une
    réécriture du payload, les essais ne prolongent pas la validité du
    code et les requêtes parallèles ne peuvent pas dépasser le maximum.
    """
    store = OTPStore(redis, tenant)
    data = await store.get(request_id)

    if data is None:
        return False, 0

    attempts_key = store._attempts_key(request_id)
    attempts = int(await redis.incr(attempts_key) or 0)
    if attempts == 1:
        # Aligne l'expiration du compteur sur celle de l'OTP
        ttl = await redis.ttl(store._key(request_id))
        if ttl > 0:
            await redis.expire(attempts_key, ttl)

    if attempts > tenant.max_verify_attempts:
        return False, 0

    if compare_codes(code, data["code_hash"]):
        # DEL atomique : seule la première requête gagnante consomme le code
        if await redis.delete(store._key(request_id)):
            await redis.delete(attempts_key)
            return True, 0
        return False, 0

    remaining = max(0, tenant.max_verify_attempts - attempts)
    return False, remaining
