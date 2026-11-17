"""Rate limiting Redis pour les envois et vérifications d'OTP — par tenant."""

from src.core.security import hash_code, now_utc
from src.core.tenants import Tenant

_WINDOW_SECONDS = 600


class RateLimiter:
    def __init__(self, redis, tenant: Tenant):
        self.redis = redis
        self.tenant = tenant

    def _key(self, *parts: str) -> str:
        return self.tenant.key_prefix() + ":".join(parts)

    async def _bump(self, *keys: str) -> list[int]:
        """INCR + EXPIRE des compteurs ; retourne les valeurs après incrément."""
        pipe = self.redis.pipeline()
        for key in keys:
            pipe.incr(key)
            pipe.expire(key, _WINDOW_SECONDS)
        results = await pipe.execute()
        return results[::2]

    async def try_send(self, user_id: str, ip: str) -> tuple[bool, int | None]:
        """Réserve atomiquement un envoi OTP.

        SET NX et INCR sont atomiques : des requêtes simultanées ne peuvent
        pas dépasser les quotas (pas de race check-then-act).
        Retourne (autorisé, retry_after_en_secondes).
        En cas d'échec d'envoi, appeler release_send() pour rendre le slot.
        """
        cooldown_key = self._key("send", "cooldown", hash_code(user_id))
        acquired = await self.redis.set(
            cooldown_key, now_utc().isoformat(),
            nx=True, ex=self.tenant.cooldown_seconds,
        )
        if acquired is None:
            ttl = await self.redis.ttl(cooldown_key)
            return False, max(ttl, 1)

        user_key = self._key("send", "count", "user", hash_code(user_id))
        ip_key = self._key("send", "count", "ip", hash_code(ip))
        user_count, ip_count = await self._bump(user_key, ip_key)

        if user_count > self.tenant.max_sends_per_user_per_10min:
            ttl = await self.redis.ttl(user_key)
            return False, max(ttl, 1)
        if ip_count > self.tenant.max_sends_per_ip_per_10min:
            ttl = await self.redis.ttl(ip_key)
            return False, max(ttl, 1)
        return True, None

    async def release_send(self, user_id: str, ip: str) -> None:
        """Annule la réservation try_send() quand l'envoi a échoué."""
        pipe = self.redis.pipeline()
        pipe.delete(self._key("send", "cooldown", hash_code(user_id)))
        pipe.decr(self._key("send", "count", "user", hash_code(user_id)))
        pipe.decr(self._key("send", "count", "ip", hash_code(ip)))
        await pipe.execute()

    # ------------------------------------------------------------------
    # Emails transactionnels — quota par destinataire (anti-bombardement).
    # L'IP appelante est le backend du tenant : la limiter casserait le
    # trafic légitime ; on borne donc la fréquence par adresse cible.
    # ------------------------------------------------------------------

    def _email_key(self, to_email: str) -> str:
        return self._key("email", "count", "to", hash_code(to_email.lower()))

    async def try_email(self, to_email: str) -> tuple[bool, int | None]:
        """Réserve atomiquement un slot email vers ce destinataire.

        Retourne (autorisé, retry_after_en_secondes).
        En cas d'échec d'envoi, appeler release_email() pour rendre le slot.
        """
        key = self._email_key(to_email)
        (count,) = await self._bump(key)
        if count > self.tenant.max_sends_per_user_per_10min:
            ttl = await self.redis.ttl(key)
            return False, max(ttl, 1)
        return True, None

    async def release_email(self, to_email: str) -> None:
        """Annule la réservation try_email() quand l'envoi a échoué."""
        await self.redis.decr(self._email_key(to_email))
