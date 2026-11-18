"""Utilisateurs web + sessions — stockage Redis.

Un utilisateur web = un compte de la plateforme qui possède un tenant.
Sessions : token stocké en Redis, cookie HttpOnly.
"""

import ast
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import asdict, dataclass

from redis.asyncio import Redis

SESSION_TTL = 7 * 24 * 3600  # 7 jours
_PBKDF2_ITERATIONS = 600_000  # recommandation OWASP
_LEGACY_ITERATIONS = 100_000  # format initial : hex nu, sans préfixe


def _hash_password(password: str, salt: str, iterations: int = _PBKDF2_ITERATIONS) -> str:
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), iterations
    ).hex()
    return f"pbkdf2${iterations}${digest}"


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
        pipe.set(self._key(user_id), json.dumps(asdict(user)))
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
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # Compatibilité avec l'ancien format repr() — migration douce
            data = ast.literal_eval(raw)
        return WebUser(**data)

    async def list_by_tenant(self, tenant_id: str) -> list[WebUser]:
        """Tous les comptes web rattachés à un tenant (usage admin)."""
        users: list[WebUser] = []
        async for key in self.redis.scan_iter("webuser:usr_*"):
            raw = await self.redis.get(key)
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if data.get("tenant_id") == tenant_id:
                users.append(WebUser(**data))
        return sorted(users, key=lambda u: u.email)

    async def delete(self, user_id: str) -> bool:
        """Supprime un compte web + son index email."""
        user = await self.get(user_id)
        if not user:
            return False
        pipe = self.redis.pipeline()
        pipe.delete(self._key(user_id), self._email_key(user.email))
        await pipe.execute()
        return True

    async def set_password(self, user_id: str, new_password: str) -> bool:
        """Réinitialise le mot de passe (usage admin)."""
        user = await self.get(user_id)
        if not user:
            return False
        user.password_salt = secrets.token_hex(16)
        user.password_hash = _hash_password(new_password, user.password_salt)
        await self.redis.set(self._key(user_id), json.dumps(asdict(user)))
        return True

    async def authenticate(self, email: str, password: str) -> WebUser | None:
        user = await self.get_by_email(email)
        if not user:
            return None
        stored = user.password_hash
        if stored.startswith("pbkdf2$"):
            try:
                iterations = int(stored.split("$", 2)[1])
            except (IndexError, ValueError):
                return None
        else:
            # Legacy : hash hex nu = 100k itérations (format initial)
            iterations = _LEGACY_ITERATIONS
        candidate = _hash_password(password, user.password_salt, iterations)
        # Comparaison des digests : le format stocké peut être préfixé ou non
        if not hmac.compare_digest(
            candidate.rsplit("$", 1)[-1], stored.rsplit("$", 1)[-1]
        ):
            return None
        if iterations < _PBKDF2_ITERATIONS:
            # Migration douce : le hash est réécrit au nouveau facteur de
            # travail lors de la prochaine connexion réussie.
            user.password_hash = _hash_password(password, user.password_salt)
            await self.redis.set(self._key(user.user_id), json.dumps(asdict(user)))
        return user


class SessionStore:
    def __init__(self, redis: Redis):
        self.redis = redis

    def _key(self, token: str) -> str:
        return f"session:{token}"

    async def create(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        await self.redis.set(self._key(token), user_id, ex=SESSION_TTL)
        return token

    async def get_user_id(self, token: str) -> str | None:
        return await self.redis.get(self._key(token))

    async def destroy(self, token: str) -> None:
        await self.redis.delete(self._key(token))
