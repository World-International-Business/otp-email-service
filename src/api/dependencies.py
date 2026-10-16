"""Dépendances FastAPI injectables."""

import redis.asyncio as redis_lib
from fastapi import Request

from src.config import Settings, get_settings
from src.email_providers.base import EmailProvider
from src.email_providers.gmail import GmailSMTPProvider
from src.services.email_service import EmailService
from src.services.notification_service import NotificationService
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


def get_email_provider(settings: Settings = get_settings()) -> EmailProvider:
    provider = settings.email_provider.lower()
    if provider == "gmail":
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


def get_notification_service(request: Request) -> NotificationService:
    return request.app.state.notification_service


def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
