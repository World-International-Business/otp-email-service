"""Fonctions utilitaires de sécurité."""

import hashlib
import hmac
import secrets
from datetime import datetime, timezone


def hash_user_id(user_id: str) -> int:
    """Hash déterministe d'un user_id pour le feature flag rollout."""
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def is_in_rollout(user_id: str, percentage: int) -> bool:
    """Détermine si un utilisateur est dans le bucket de rollout (déterministe)."""
    if percentage <= 0:
        return False
    if percentage >= 100:
        return True
    return (hash_user_id(user_id) % 100) < percentage


def hash_code(code: str) -> str:
    """Hash SHA-256 d'un code OTP."""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def compare_codes(provided_code: str, stored_hash: str) -> bool:
    """Comparaison en temps constant d'un code avec son hash stocké."""
    provided_hash = hash_code(provided_code)
    return hmac.compare_digest(provided_hash, stored_hash)


def generate_secure_code(length: int = 6) -> str:
    """Génère un code numérique aléatoire cryptographiquement sûr."""
    return str(secrets.randbelow(10**length)).zfill(length)


def generate_request_id() -> str:
    """Génère un request_id unique préfixé."""
    return f"req_{secrets.token_urlsafe(16)}"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
