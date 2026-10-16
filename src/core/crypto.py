"""Chiffrement des secrets au repos (Fernet — AES-128-CBC + HMAC-SHA256).

Les champs sensibles des tenants (smtp_password, hmac_secret) sont stockés
chiffrés dans Redis sous la forme `enc:v1:<token_fernet>`.

Clé de chiffrement, par ordre de priorité :
  1. `ENCRYPTION_KEY` (env) — clé Fernet base64, ou n'importe quel secret
     (il est alors dérivé par SHA-256).
  2. Dérivation déterministe de `HMAC_SECRET` — fonctionne sans config
     supplémentaire, mais une rotation de HMAC_SECRET rend les secrets
     chiffrés illisibles. Préférer ENCRYPTION_KEY dédiée en production.

Générer une clé dédiée :
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

import base64
import hashlib
import logging
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

ENCRYPTED_PREFIX = "enc:v1:"
_DERIVATION_CONTEXT = b"otp-email-service/tenant-secrets/v1"


class SecretCipher:
    def __init__(self, key: bytes):
        self._fernet = Fernet(key)

    def encrypt(self, value: str | None) -> str | None:
        """Chiffre une valeur ; les valeurs vides/None passent telles quelles."""
        if not value:
            return value
        token = self._fernet.encrypt(value.encode("utf-8")).decode("ascii")
        return ENCRYPTED_PREFIX + token

    def decrypt(self, value: str | None) -> str | None:
        """Déchiffre ; les valeurs sans préfixe `enc:v1:` sont supposées en clair
        (rétrocompatibilité avec les tenants créés avant le chiffrement)."""
        if not value or not value.startswith(ENCRYPTED_PREFIX):
            return value
        token = value[len(ENCRYPTED_PREFIX):].encode("ascii")
        try:
            return self._fernet.decrypt(token).decode("utf-8")
        except InvalidToken as exc:
            raise RuntimeError(
                "Unable to decrypt tenant secret — wrong ENCRYPTION_KEY "
                "or rotated HMAC_SECRET"
            ) from exc

    @staticmethod
    def is_encrypted(value: str | None) -> bool:
        return bool(value) and value.startswith(ENCRYPTED_PREFIX)


def _derive_key(material: str | bytes) -> bytes:
    """Dérive une clé Fernet valide depuis un secret arbitraire."""
    if isinstance(material, str):
        material = material.encode("utf-8")
    digest = hashlib.sha256(_DERIVATION_CONTEXT + material).digest()
    return base64.urlsafe_b64encode(digest)


def _key_from_settings(settings) -> bytes:
    explicit = getattr(settings, "encryption_key", None)
    if explicit:
        raw = explicit.strip()
        # Clé Fernet native (44 chars base64 urlsafe) ou secret arbitraire ?
        try:
            decoded = base64.urlsafe_b64decode(raw)
            if len(decoded) == 32:
                return raw.encode("ascii")
        except Exception:
            pass
        return _derive_key(raw)
    # Fallback : dérivation du secret HMAC maître (déterministe entre restarts)
    secret = settings.hmac_secret
    if isinstance(secret, bytes):
        secret = secret.decode("utf-8")
    logger.warning(
        "encryption_key_not_configured — deriving cipher key from HMAC_SECRET. "
        "Set ENCRYPTION_KEY in production."
    )
    return _derive_key(secret)


@lru_cache
def get_cipher() -> SecretCipher:
    """Singleton — construit depuis get_settings() au premier appel."""
    from src.config import get_settings

    return SecretCipher(_key_from_settings(get_settings()))
