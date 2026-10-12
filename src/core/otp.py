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
            "attempts": 0,
            "verified": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        await self.redis.setex(self._key(request_id), self.ttl, json.dumps(payload))
        return code, request_id, code_hash

    async def get(self, request_id: str) -> dict | None:
        raw = await self.redis.get(self._key(request_id))
        if raw is None:
            return None
        return json.loads(raw)

    async def increment_attempts(self, request_id: str, data: dict) -> dict:
        data["attempts"] += 1
        await self.redis.setex(
            self._key(request_id),
            self.ttl,
            json.dumps(data),
        )
        return data

    async def mark_verified(self, request_id: str, data: dict) -> None:
        data["verified"] = True
        await self.redis.setex(
            self._key(request_id),
            self.ttl,
            json.dumps(data),
        )

    async def delete(self, request_id: str) -> None:
        await self.redis.delete(self._key(request_id))


async def verify_otp(
    redis: Redis,
    tenant: Tenant,
    request_id: str,
    code: str,
) -> tuple[bool, int]:
    """Vérifie un OTP et retourne (succès, tentatives restantes)."""
    store = OTPStore(redis, tenant)
    data = await store.get(request_id)

    if data is None:
        return False, 0

    if data["verified"]:
        return False, 0

    attempts = data["attempts"]
    if attempts >= tenant.max_verify_attempts:
        return False, 0

    if compare_codes(code, data["code_hash"]):
        await store.mark_verified(request_id, data)
        await store.delete(request_id)
        return True, 0

    updated = await store.increment_attempts(request_id, data)
    remaining = max(0, tenant.max_verify_attempts - updated["attempts"])
    return False, remaining
