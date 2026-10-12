"""Rate limiting Redis pour les envois et vérifications d'OTP — par tenant."""

from src.core.security import hash_code, now_utc
from src.core.tenants import Tenant


class RateLimiter:
    def __init__(self, redis, tenant: Tenant):
        self.redis = redis
        self.tenant = tenant

    def _key(self, *parts: str) -> str:
        return self.tenant.key_prefix() + ":".join(parts)

    async def can_send(self, user_id: str, ip: str) -> tuple[bool, int | None]:
        """Vérifie si un envoi est autorisé.

        Retourne (autorisé, retry_after_en_secondes).
        """
        cooldown_key = self._key("send", "cooldown", hash_code(user_id))
        if await self.redis.exists(cooldown_key):
            ttl = await self.redis.ttl(cooldown_key)
            return False, max(ttl, 1)

        user_key = self._key("send", "count", "user", hash_code(user_id))
        ip_key = self._key("send", "count", "ip", hash_code(ip))

        user_count = int(await self.redis.get(user_key) or 0)
        ip_count = int(await self.redis.get(ip_key) or 0)

        if user_count >= self.tenant.max_sends_per_user_per_10min:
            ttl = await self.redis.ttl(user_key)
            return False, max(ttl, 1)

        if ip_count >= self.tenant.max_sends_per_ip_per_10min:
            ttl = await self.redis.ttl(ip_key)
            return False, max(ttl, 1)

        return True, None

    async def record_send(self, user_id: str, ip: str) -> None:
        """Incrémente les compteurs d'envoi et positionne le cooldown."""
        window = 600
        user_key = self._key("send", "count", "user", hash_code(user_id))
        ip_key = self._key("send", "count", "ip", hash_code(ip))
        cooldown_key = self._key("send", "cooldown", hash_code(user_id))

        pipe = self.redis.pipeline()
        pipe.incr(user_key)
        pipe.expire(user_key, window)
        pipe.incr(ip_key)
        pipe.expire(ip_key, window)
        pipe.setex(cooldown_key, self.tenant.cooldown_seconds, now_utc().isoformat())
        await pipe.execute()
