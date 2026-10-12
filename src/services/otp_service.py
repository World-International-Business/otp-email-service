"""Orchestration métier : envoi et vérification d'OTP — multi-tenant."""

import logging

from fastapi import HTTPException
from redis.asyncio import Redis

from src.config import Settings
from src.core.models import Channel, Language, OTPSendResponse, OTPVerifyResponse
from src.core.otp import OTPStore, verify_otp
from src.core.security import hash_code, is_in_rollout
from src.core.tenants import Tenant
from src.email_providers.base import EmailProvider, SenderIdentity
from src.services.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)


class OTPService:
    def __init__(
        self,
        redis: Redis,
        settings: Settings,
        email_provider: EmailProvider,
    ):
        self.redis = redis
        self.settings = settings
        self.email_provider = email_provider

    def _store(self, tenant: Tenant) -> OTPStore:
        return OTPStore(self.redis, tenant)

    def _rate_limiter(self, tenant: Tenant) -> RateLimiter:
        return RateLimiter(self.redis, tenant)

    def _sender(self, tenant: Tenant) -> SenderIdentity:
        return SenderIdentity(
            from_email=tenant.from_email or self.settings.from_email,
            from_name=tenant.from_name or self.settings.from_name,
            smtp_host=tenant.smtp_host or self.settings.smtp_host,
            smtp_port=tenant.smtp_port or self.settings.smtp_port,
            smtp_user=tenant.smtp_user or self.settings.smtp_user,
            smtp_password=tenant.smtp_password or self.settings.smtp_password,
        )

    def _choose_channel(self, tenant: Tenant, user_id: str, country_code: str) -> Channel:
        if country_code.upper() in {c.upper() for c in tenant.risky_countries}:
            return Channel.sms_fallback
        if not is_in_rollout(str(user_id), tenant.rollout_percentage):
            return Channel.sms_fallback
        return Channel.email

    async def send_otp(
        self,
        tenant: Tenant,
        user_id: str,
        email: str,
        country_code: str,
        purpose: str,
        language: Language,
        client_ip: str,
        include_debug_code: bool = False,
    ) -> OTPSendResponse:
        if purpose not in tenant.allowed_purposes:
            raise HTTPException(
                status_code=403,
                detail=f"Purpose '{purpose}' is not allowed for this client",
            )

        channel = self._choose_channel(tenant, user_id, country_code)

        if channel == Channel.sms_fallback:
            logger.info(
                "sms_fallback_selected",
                extra={
                    "tenant_id": tenant.tenant_id,
                    "user_id_hash": hash_code(user_id),
                    "country_code": country_code,
                },
            )
            return OTPSendResponse(
                channel=Channel.sms_fallback,
                request_id="",
                expires_in=0,
            )

        allowed, retry_after = await self._rate_limiter(tenant).can_send(user_id, client_ip)
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="Too many OTP requests",
                headers={"Retry-After": str(retry_after)},
            )

        store = self._store(tenant)
        code, request_id, _code_hash = await store.create(user_id)

        subject, text_body, html_body = self._render_email(code, language, tenant)
        success = await self.email_provider.send_email(
            email, subject, text_body, html_body, sender=self._sender(tenant)
        )

        if not success:
            await store.delete(request_id)
            raise HTTPException(status_code=502, detail="Failed to send email")

        await self._rate_limiter(tenant).record_send(user_id, client_ip)
        await self.redis.incr(f"{tenant.key_prefix()}stats:otps_sent")

        logger.info(
            "otp_email_sent",
            extra={
                "tenant_id": tenant.tenant_id,
                "user_id_hash": hash_code(user_id),
                "request_id": request_id,
                "country_code": country_code,
                "purpose": purpose,
            },
        )

        return OTPSendResponse(
            channel=Channel.email,
            request_id=request_id,
            expires_in=tenant.otp_ttl_seconds,
            debug_code=code if (include_debug_code and self.settings.debug) else None,
        )

    async def verify(
        self, tenant: Tenant, request_id: str, code: str
    ) -> OTPVerifyResponse:
        verified, remaining = await verify_otp(
            self.redis,
            tenant,
            request_id,
            code,
        )
        if verified:
            await self.redis.incr(f"{tenant.key_prefix()}stats:otps_verified")

        logger.info(
            "otp_verification_attempt",
            extra={
                "tenant_id": tenant.tenant_id,
                "request_id": request_id,
                "verified": verified,
                "attempts_remaining": remaining,
            },
        )

        return OTPVerifyResponse(verified=verified, attempts_remaining=remaining)

    # ------------------------------------------------------------------
    # Templates — brand_name injecté par tenant
    # ------------------------------------------------------------------

    def _render_email(
        self, code: str, language: Language, tenant: Tenant
    ) -> tuple[str, str, str]:
        brand = tenant.brand_name
        if language == Language.fr:
            subject = f"Votre code de vérification {brand}"
            text_body = self._text_template_fr(code, brand)
            html_body = self._html_template_fr(code, brand)
        else:
            subject = f"Your {brand} verification code"
            text_body = self._text_template_en(code, brand)
            html_body = self._html_template_en(code, brand)
        return subject, text_body, html_body

    def _text_template_fr(self, code: str, brand: str) -> str:
        return (
            "Bonjour,\n\n"
            f"Voici votre code de vérification {brand} :\n\n"
            f"{code}\n\n"
            "Ce code est valable 5 minutes.\n"
            f"Ne partagez jamais ce code, même avec le support {brand}.\n\n"
            f"L'équipe {brand}"
        )

    def _text_template_en(self, code: str, brand: str) -> str:
        return (
            "Hello,\n\n"
            f"Here is your {brand} verification code:\n\n"
            f"{code}\n\n"
            "This code is valid for 5 minutes.\n"
            f"Never share this code, even with {brand} support.\n\n"
            f"The {brand} Team"
        )

    def _html_template_fr(self, code: str, brand: str) -> str:
        return f"""<!DOCTYPE html>
<html lang="fr">
<head><meta charset="UTF-8"><title>Code {brand}</title></head>
<body style="font-family: Arial, sans-serif; background:#f4f4f4; padding:20px;">
  <div style="max-width:480px; margin:auto; background:#fff; border-radius:8px; padding:32px;">
    <h2 style="color:#111;">Votre code de vérification</h2>
    <p style="color:#333;">Utilisez ce code pour finaliser votre connexion :</p>
    <div style="font-size:42px; font-weight:bold; letter-spacing:8px; color:#2d6cdf; margin:24px 0;">{code}</div>
    <p style="color:#666; font-size:14px;">Ce code est valable 5 minutes.</p>
    <p style="color:#c00; font-size:14px; font-weight:bold;">Ne partagez jamais ce code, même avec le support {brand}.</p>
    <hr style="border:none; border-top:1px solid #eee; margin:24px 0;">
    <p style="color:#999; font-size:12px;">{brand}</p>
  </div>
</body>
</html>"""

    def _html_template_en(self, code: str, brand: str) -> str:
        return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>{brand} Code</title></head>
<body style="font-family: Arial, sans-serif; background:#f4f4f4; padding:20px;">
  <div style="max-width:480px; margin:auto; background:#fff; border-radius:8px; padding:32px;">
    <h2 style="color:#111;">Your verification code</h2>
    <p style="color:#333;">Use this code to complete your login:</p>
    <div style="font-size:42px; font-weight:bold; letter-spacing:8px; color:#2d6cdf; margin:24px 0;">{code}</div>
    <p style="color:#666; font-size:14px;">This code is valid for 5 minutes.</p>
    <p style="color:#c00; font-size:14px; font-weight:bold;">Never share this code, even with {brand} support.</p>
    <hr style="border:none; border-top:1px solid #eee; margin:24px 0;">
    <p style="color:#999; font-size:12px;">{brand}</p>
  </div>
</body>
</html>"""
