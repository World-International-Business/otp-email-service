"""Service d'envoi d'emails transactionnels génériques — multi-tenant."""

import logging
import secrets

from fastapi import HTTPException
from redis.asyncio import Redis

from src.config import Settings
from src.core.models import EmailSendRequest, EmailSendResponse, EmailType, Language
from src.core.security import hash_code
from src.core.tenants import Tenant
from src.email_providers.base import EmailProvider, SenderIdentity
from src.services.events import log_event, mask_email

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
        )

    async def send(
        self, tenant: Tenant, request: EmailSendRequest
    ) -> EmailSendResponse:
        """Envoie un email selon son type. Lève HTTPException en cas d'échec."""
        subject, text_body, html_body = self._render(request, tenant)

        message_id = f"msg_{secrets.token_urlsafe(16)}"

        success = await self.email_provider.send_email(
            to_email=request.to_email,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
            sender=self._sender(tenant),
        )

        if not success:
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
            raise HTTPException(status_code=502, detail="Failed to send email")

        await self.redis.incr(f"{tenant.key_prefix()}stats:emails_sent")
        await log_event(
            self.redis, tenant, "email_sent",
            email=mask_email(str(request.to_email)),
            email_type=request.type.value, message_id=message_id,
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
            request.text_body.replace("\n", "<br>"),
            tenant.brand_name,
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
        html = self._wrap_html(subject, text.replace("\n", "<br>"), brand)
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
        html = self._wrap_html(title, message.replace("\n", "<br>"), brand)
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
        html = self._wrap_html(subject, text.replace("\n", "<br>"), brand)
        return subject, text, html

    def _wrap_html(self, title: str, content_html: str, brand: str) -> str:
        return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><title>{title}</title></head>
<body style="font-family: Arial, sans-serif; background:#f4f4f4; padding:20px;">
  <div style="max-width:480px; margin:auto; background:#fff; border-radius:8px; padding:32px;">
    <h2 style="color:#111; margin-top:0;">{title}</h2>
    <p style="color:#333; line-height:1.6;">{content_html}</p>
    <hr style="border:none; border-top:1px solid #eee; margin:24px 0;">
    <p style="color:#999; font-size:12px;">{brand}</p>
  </div>
</body>
</html>"""
