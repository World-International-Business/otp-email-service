"""Orchestration métier : envoi et vérification d'OTP — multi-tenant."""

import logging

from fastapi import HTTPException
from redis.asyncio import Redis

from src.config import Settings
from src.core.models import Channel, Language, OTPSendResponse, OTPVerifyResponse
from src.core.otp import OTPStore, verify_otp
from src.core.security import hash_code, is_in_rollout
from src.core.tenants import Tenant
from src.email_providers.base import EmailProvider, EmailSendError, SenderIdentity
from src.services.email_queue import set_status
from src.services.email_templates import otp_html, resolve_logo_url
from src.services.events import incr_daily_stat, log_event, mask_email
from src.services.failover import send_with_failover
from src.services.rate_limiter import RateLimiter
from src.services.sandbox import capture_send
from src.services.suppression import suppression_reason
from src.services.warmup import seconds_until_midnight, warmup_state
from src.services.webhooks import enqueue_webhook

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
            smtp_use_ssl=tenant.smtp_use_ssl,
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
        magic_link: bool = False,
    ) -> OTPSendResponse:
        if purpose not in tenant.allowed_purposes:
            await log_event(
                self.redis, tenant, "otp_purpose_denied",
                email=mask_email(email), purpose=purpose,
            )
            raise HTTPException(
                status_code=403,
                detail=f"Purpose '{purpose}' is not allowed for this client",
            )

        channel = self._choose_channel(tenant, user_id, country_code)

        # Liste de suppression : adresse en bounce/plainte = refus net
        # (protège la réputation — l'admin peut la retirer manuellement).
        if channel == Channel.email:
            reason = await suppression_reason(
                self.redis, tenant.tenant_id, email)
            if reason:
                await log_event(
                    self.redis, tenant, "otp_suppressed",
                    email=mask_email(email), purpose=purpose, reason=reason,
                )
                raise HTTPException(
                    status_code=422,
                    detail="Recipient address is suppressed "
                           "(previous bounce or complaint)",
                )
            # Warm-up : SMTP neuf du tenant = cap journalier progressif.
            remaining, cap, _ = await warmup_state(
                self.redis, self.settings, tenant)
            if remaining == 0:
                retry = seconds_until_midnight()
                await log_event(
                    self.redis, tenant, "warmup_limited",
                    email=mask_email(email), purpose=purpose, daily_cap=cap,
                )
                raise HTTPException(
                    status_code=429,
                    detail=f"Sender warm-up in progress — daily cap "
                           f"{cap} reached, increases automatically",
                    headers={"Retry-After": str(retry)},
                )

        if channel == Channel.sms_fallback:
            logger.info(
                "sms_fallback_selected",
                extra={
                    "tenant_id": tenant.tenant_id,
                    "user_id_hash": hash_code(user_id),
                    "country_code": country_code,
                },
            )
            await log_event(
                self.redis, tenant, "otp_sms_fallback",
                email=mask_email(email), country_code=country_code,
                purpose=purpose,
            )
            return OTPSendResponse(
                channel=Channel.sms_fallback,
                request_id="",
                expires_in=0,
            )

        allowed, retry_after = await self._rate_limiter(tenant).try_send(user_id, client_ip)
        if not allowed:
            await log_event(
                self.redis, tenant, "otp_rate_limited",
                email=mask_email(email), purpose=purpose,
                retry_after=retry_after,
            )
            raise HTTPException(
                status_code=429,
                detail="Too many OTP requests",
                headers={"Retry-After": str(retry_after)},
            )

        store = self._store(tenant)
        code, request_id, _code_hash = await store.create(user_id)

        magic_url = None
        if magic_link and not self.settings.public_base_url:
            logger.warning(
                "magic_link_ignored",
                tenant_id=tenant.tenant_id,
                reason="PUBLIC_BASE_URL non configuré",
            )
        if magic_link and self.settings.public_base_url:
            base = self.settings.public_base_url.rstrip("/")
            from urllib.parse import urlencode
            qs = urlencode(
                {"request_id": request_id, "code": code, "t": tenant.tenant_id}
            )
            magic_url = f"{base}/v1/otp/confirm?{qs}"

        subject, text_body, html_body = self._render_email(code, language, tenant, magic_url)
        reason = ""
        if tenant.sandbox:
            # Sandbox : pipeline complet, capture au lieu de l'envoi SMTP.
            await capture_send(
                self.redis, tenant.tenant_id, to=email, subject=subject,
                text_body=text_body, html_body=html_body,
                kind="otp", ref=request_id,
            )
            success, via = True, "sandbox"
        else:
            try:
                success, via_failover = await send_with_failover(
                    self.email_provider, self.settings, tenant, self.redis,
                    to_email=email, subject=subject, text_body=text_body,
                    html_body=html_body, sender=self._sender(tenant),
                    context={"request_id": request_id, "purpose": purpose},
                )
            except EmailSendError as exc:
                success, via_failover, reason = False, False, str(exc)[:150]
            via = "failover" if via_failover else "tenant"

        if not success:
            await store.delete(request_id)
            await self._rate_limiter(tenant).release_send(user_id, client_ip)
            await log_event(
                self.redis, tenant, "otp_send_failed",
                email=mask_email(email), purpose=purpose,
            )
            await set_status(
                self.redis, request_id, tenant.tenant_id, "failed",
                to=email, type_="otp", error=reason, attempts=1,
            )
            await enqueue_webhook(
                self.redis, tenant, "otp.failed",
                {"request_id": request_id, "to": email,
                 "purpose": purpose, "error": reason},
            )
            detail = f"Failed to send email: {reason}" if reason else "Failed to send email"
            raise HTTPException(status_code=502, detail=detail)

        await set_status(
            self.redis, request_id, tenant.tenant_id, "sent",
            to=email, type_="otp", attempts=1,
        )
        await enqueue_webhook(
            self.redis, tenant, "otp.sent",
            {"request_id": request_id, "to": email,
             "purpose": purpose, "expires_in": tenant.otp_ttl_seconds},
        )
        await log_event(
            self.redis, tenant, "otp_sent",
            email=mask_email(email), purpose=purpose,
            country_code=country_code, request_id=request_id,
            via=via,
        )
        await self.redis.incr(f"{tenant.key_prefix()}stats:otps_sent")
        await incr_daily_stat(self.redis, tenant, "otps_sent")

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
            await incr_daily_stat(self.redis, tenant, "otps_verified")
            await set_status(self.redis, request_id, tenant.tenant_id, "verified")
            await enqueue_webhook(
                self.redis, tenant, "otp.verified",
                {"request_id": request_id},
            )

        await log_event(
            self.redis, tenant,
            "otp_verified" if verified else "otp_verify_failed",
            request_id=request_id, attempts_remaining=remaining,
        )

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
        self, code: str, language: Language, tenant: Tenant, magic_url: str | None = None
    ) -> tuple[str, str, str]:
        brand = tenant.brand_name
        lang = "fr" if language == Language.fr else "en"
        mins = max(1, round(tenant.otp_ttl_seconds / 60))
        if language == Language.fr:
            subject = f"Votre code de vérification {brand}"
            text_body = self._text_template_fr(code, brand, mins)
        else:
            subject = f"Your {brand} verification code"
            text_body = self._text_template_en(code, brand, mins)
        return subject, text_body, otp_html(
            code, brand, lang, magic_url, ttl_minutes=mins,
            color=tenant.brand_color,
            logo_url=resolve_logo_url(tenant.email_logo_url, self.settings.public_base_url),
            footer=tenant.email_footer,
        )

    def _text_template_fr(self, code: str, brand: str, mins: int) -> str:
        return (
            "Bonjour,\n\n"
            f"Voici votre code de vérification {brand} :\n\n"
            f"{code}\n\n"
            f"Ce code est valable {mins} {'minute' if mins == 1 else 'minutes'}.\n"
            f"Ne partagez jamais ce code, même avec le support {brand}.\n\n"
            f"L'équipe {brand}"
        )

    def _text_template_en(self, code: str, brand: str, mins: int) -> str:
        return (
            "Hello,\n\n"
            f"Here is your {brand} verification code:\n\n"
            f"{code}\n\n"
            f"This code is valid for {mins} {'minute' if mins == 1 else 'minutes'}.\n"
            f"Never share this code, even with {brand} support.\n\n"
            f"The {brand} Team"
        )

    # Les templates HTML partagés vivent dans src/services/email_templates.py
