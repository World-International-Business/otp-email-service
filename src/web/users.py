"""Utilisateurs web + sessions — stockage Redis.

Un utilisateur web = un compte de la plateforme qui possède un tenant.
Sessions : token stocké en Redis, cookie HttpOnly.
"""

import hashlib
import secrets
import time
from dataclasses import dataclass

from redis.asyncio import Redis

SESSION_TTL = 7 * 24 * 3600  # 7 jours


def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), 100_000
    ).hex()


@dataclass
class WebUser:
    user_id: str
    email: str
    password_hash: str
    password_salt: str
    tenant_id: str
    app_name: str
    created_at: float


class WebUserStore:
    def __init__(self, redis: Redis):
        self.redis = redis

    def _key(self, user_id: str) -> str:
        return f"webuser:{user_id}"

    def _email_key(self, email: str) -> str:
        return f"webuser:email:{email.lower()}"

    async def create(
        self, email: str, password: str, tenant_id: str, app_name: str
    ) -> WebUser:
        user_id = f"usr_{secrets.token_urlsafe(12)}"
        salt = secrets.token_hex(16)
        user = WebUser(
            user_id=user_id,
            email=email.lower(),
            password_hash=_hash_password(password, salt),
            password_salt=salt,
            tenant_id=tenant_id,
            app_name=app_name,
            created_at=time.time(),
        )
        pipe = self.redis.pipeline()
        pipe.set(self._key(user_id), repr(user.__dict__))
        pipe.set(self._email_key(email), user_id)
        await pipe.execute()
        return user

    async def get_by_email(self, email: str) -> WebUser | None:
        user_id = await self.redis.get(self._email_key(email))
        if not user_id:
            return None
        return await self.get(user_id)

    async def get(self, user_id: str) -> WebUser | None:
        raw = await self.redis.get(self._key(user_id))
        if not raw:
            return None
        data = eval(raw)  # stockage interne Redis — contrôlé par nous
        return WebUser(**data)

    async def authenticate(self, email: str, password: str) -> WebUser | None:
        user = await self.get_by_email(email)
        if not user:
            return None
        if _hash_password(password, user.password_salt) == user.password_hash:
            return user
        return None


class SessionStore:
    def __init__(self, redis: Redis):
        self.redis = redis

    def _key(self, token: str) -> str:
        return f"session:{token}"

    async def create(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        await self.redis.setex(self._key(token), SESSION_TTL, user_id)
        return token

    async def get_user_id(self, token: str) -> str | None:
        return await self.redis.get(self._key(token))

    async def destroy(self, token: str) -> None:
        await self.redis.delete(self._key(token))
