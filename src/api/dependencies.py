"""Dépendances FastAPI injectables."""

import ipaddress
from urllib.parse import urlparse

import redis.asyncio as redis_lib
from fastapi import HTTPException, Request

from src.config import Settings, get_settings
from src.email_providers.base import EmailProvider
from src.email_providers.gmail import GmailSMTPProvider
from src.services.email_service import EmailService
from src.services.otp_service import OTPService

_redis_client = None


def get_redis_client(settings: Settings):
    global _redis_client
    if _redis_client is not None:
        return _redis_client

    if settings.use_fakeredis or settings.redis_url.lower().startswith("fakeredis"):
        import fakeredis

        _redis_client = fakeredis.FakeAsyncRedis(decode_responses=True)
    else:
        _redis_client = redis_lib.from_url(settings.redis_url, decode_responses=True)
    return _redis_client


def get_email_provider(settings: Settings | None = None) -> EmailProvider:
    settings = settings or get_settings()
    provider = settings.email_provider.lower()
    if provider in ("gmail", "smtp"):
        # "smtp" = alias explicite du provider SMTP générique (adresses pro)
        return GmailSMTPProvider(settings)
    if provider == "brevo":
        from src.email_providers.brevo import BrevoProvider

        return BrevoProvider(settings)
    if provider == "resend":
        from src.email_providers.resend import ResendProvider

        return ResendProvider(settings)
    if provider == "console":
        from src.email_providers.console import ConsoleEmailProvider

        return ConsoleEmailProvider(settings)
    raise ValueError(f"Unknown email provider: {settings.email_provider}")


def get_otp_service(request: Request) -> OTPService:
    return request.app.state.otp_service


def get_email_service(request: Request) -> EmailService:
    return request.app.state.email_service


def _is_trusted_proxy(ip_str: str) -> bool:
    """Le proxy immédiat est-il de confiance ? (loopback, privé, link-local)"""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def get_client_ip(request: Request) -> str:
    """IP réelle du client.

    X-Forwarded-For n'est honoré que si le pair direct est un proxy de
    confiance (loopback/privé) — sinon un client public pourrait forger
    le header et contourner le rate limiting par IP.
    """
    peer = request.client.host if request.client else ""
    if peer and _is_trusted_proxy(peer):
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return peer or "unknown"


async def require_same_origin(request: Request) -> None:
    """Anti-CSRF : rejette les POST navigateur émis depuis un autre site.

    Complète SameSite=Lax — couvre les navigateurs anciens ou non conformes.
    Les clients non-navigateur (curl, backends) n'envoient pas Origin :
    le contrôle ne s'applique qu'en présence du header.
    """
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return
    valid = {
        request.url.netloc,
        request.headers.get("x-forwarded-host", ""),
    }
    base = getattr(request.app.state, "settings", None)
    if base is not None and base.public_base_url:
        valid.add(urlparse(base.public_base_url).netloc)
    if urlparse(source).netloc not in valid:
        raise HTTPException(status_code=403, detail="Cross-site request rejected")
