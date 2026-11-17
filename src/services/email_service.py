"""Service d'envoi d'emails transactionnels génériques — multi-tenant."""

import logging
import secrets
from html import escape

from fastapi import HTTPException
from redis.asyncio import Redis

from src.config import Settings
from src.core.models import EmailSendRequest, EmailSendResponse, EmailType, Language
from src.core.security import hash_code
from src.core.tenants import Tenant
from src.email_providers.base import EmailProvider, EmailSendError, SenderIdentity
from src.services.email_templates import generic_html
from src.services.events import incr_daily_stat, log_event, mask_email
from src.services.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)


class EmailService:
    """Envoi d'emails transactionnels (bienvenue, notification, transaction, custom)."""

    def __init__(
        self,
        redis: Redis,
        settings: Settings,
        email_provider: EmailProvider,
    ):
        self.redis = redis
        self.settings = settings
        self.email_provider = email_provider

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

    async def send(
        self, tenant: Tenant, request: EmailSendRequest, client_ip: str = "unknown"
    ) -> EmailSendResponse:
        """Envoie un email selon son type. Lève HTTPException en cas d'échec."""
        to_email = str(request.to_email)
        # Rendre AVANT de réserver un slot quota : un type=custom incomplet
        # lève 422 ici — il ne doit pas consommer le quota du destinataire.
        subject, text_body, html_body = self._render(request, tenant)

        limiter = RateLimiter(self.redis, tenant)
        allowed, retry_after = await limiter.try_email(to_email)
        if not allowed:
            await log_event(
                self.redis, tenant, "email_rate_limited",
                email=mask_email(to_email), retry_after=retry_after,
                client_ip=client_ip,
            )
            raise HTTPException(
                status_code=429,
                detail="Too many emails to this recipient",
                headers={"Retry-After": str(retry_after)},
            )

        message_id = f"msg_{secrets.token_urlsafe(16)}"

        reason = ""
        try:
            success = await self.email_provider.send_email(
                to_email=request.to_email,
                subject=subject,
                text_body=text_body,
                html_body=html_body,
                sender=self._sender(tenant),
            )
        except EmailSendError as exc:
            success, reason = False, str(exc)[:150]

        if not success:
            await limiter.release_email(to_email)
            logger.error(
                "email_send_failed",
                extra={
                    "tenant_id": tenant.tenant_id,
                    "message_id": message_id,
                    "type": request.type.value,
                    "to_email_hash": hash_code(str(request.to_email)),
                },
            )
            await log_event(
                self.redis, tenant, "email_failed",
                email=mask_email(str(request.to_email)),
                email_type=request.type.value,
            )
            detail = f"Failed to send email: {reason}" if reason else "Failed to send email"
            raise HTTPException(status_code=502, detail=detail)

        await self.redis.incr(f"{tenant.key_prefix()}stats:emails_sent")
        await incr_daily_stat(self.redis, tenant, "emails_sent")
        await log_event(
            self.redis, tenant, "email_sent",
            email=mask_email(to_email),
            email_type=request.type.value, message_id=message_id,
            client_ip=client_ip,
        )

        logger.info(
            "email_sent",
            extra={
                "tenant_id": tenant.tenant_id,
                "message_id": message_id,
                "type": request.type.value,
                "to_email_hash": hash_code(str(request.to_email)),
            },
        )

        return EmailSendResponse(
            sent=True,
            message_id=message_id,
            to_email=request.to_email,
        )

    # ------------------------------------------------------------------
    # Templates — brand_name injecté par tenant
    # ------------------------------------------------------------------

    def _render(
        self, request: EmailSendRequest, tenant: Tenant
    ) -> tuple[str, str, str]:
        if request.type == EmailType.custom:
            return self._render_custom(request, tenant)

        builders = {
            EmailType.welcome: self._welcome,
            EmailType.notification: self._notification,
            EmailType.transaction: self._transaction,
        }
        return builders[request.type](request, tenant)

    def _render_custom(
        self, request: EmailSendRequest, tenant: Tenant
    ) -> tuple[str, str, str]:
        if not request.subject or not request.text_body:
            raise HTTPException(
                status_code=422,
                detail="subject and text_body are required when type=custom",
            )
        html = request.html_body or self._wrap_html(
            request.subject,
            escape(request.text_body).replace("\n", "<br>"),
            tenant,
        )
        return request.subject, request.text_body, html

    def _welcome(
        self, request: EmailSendRequest, tenant: Tenant
    ) -> tuple[str, str, str]:
        brand = tenant.brand_name
        name = request.data.get("name", "")
        if request.language == Language.fr:
            subject = f"Bienvenue sur {brand} !"
            text = (
                f"Bonjour {name},\n\n"
                f"Bienvenue sur {brand}. Votre compte est prêt à l'emploi.\n\n"
                f"L'équipe {brand}"
            )
        else:
            subject = f"Welcome to {brand}!"
            text = (
                f"Hello {name},\n\n"
                f"Welcome to {brand}. Your account is ready to use.\n\n"
                f"The {brand} Team"
            )
        html = self._wrap_html(subject, escape(text).replace("\n", "<br>"), tenant)
        return subject, text, html

    def _notification(
        self, request: EmailSendRequest, tenant: Tenant
    ) -> tuple[str, str, str]:
        brand = tenant.brand_name
        title = request.data.get("title", "Notification")
        message = request.data.get("message", "")
        subject = f"{brand} — {title}"
        if request.language == Language.fr:
            text = f"{title}\n\n{message}\n\nL'équipe {brand}"
        else:
            text = f"{title}\n\n{message}\n\nThe {brand} Team"
        html = self._wrap_html(title, escape(message).replace("\n", "<br>"), tenant)
        return subject, text, html

    def _transaction(
        self, request: EmailSendRequest, tenant: Tenant
    ) -> tuple[str, str, str]:
        brand = tenant.brand_name
        amount = request.data.get("amount", "")
        currency = request.data.get("currency", "")
        status = request.data.get("status", "confirmed")
        if request.language == Language.fr:
            subject = f"Votre transaction {brand}"
            text = (
                f"Transaction : {amount} {currency}\n"
                f"Statut : {status}\n\n"
                "Ne partagez jamais vos codes de sécurité.\n"
                f"L'équipe {brand}"
            )
        else:
            subject = f"Your {brand} transaction"
            text = (
                f"Transaction: {amount} {currency}\n"
                f"Status: {status}\n\n"
                "Never share your security codes.\n"
                f"The {brand} Team"
            )
        html = self._wrap_html(subject, escape(text).replace("\n", "<br>"), tenant)
        return subject, text, html

    def _wrap_html(
        self, title: str, content_html: str, tenant_or_brand
    ) -> str:
        if isinstance(tenant_or_brand, Tenant):
            t = tenant_or_brand
            return generic_html(
                title, content_html, t.brand_name,
                color=t.brand_color, logo_url=t.email_logo_url,
                footer=t.email_footer,
            )
        return generic_html(title, content_html, tenant_or_brand)
