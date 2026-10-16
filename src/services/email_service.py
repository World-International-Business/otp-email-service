"""Service d'emails transactionnels — file d'attente + templates tenant.

L'envoi est délégué au NotificationWorker : ce service ne fait que résoudre
le contenu (template tenant > built-in > inline) puis enfiler le message.
"""

import logging

from fastapi import HTTPException
from redis.asyncio import Redis

from src.config import Settings
from src.core.models import (
    EmailSendRequest,
    EmailSendResponse,
    EmailType,
    Language,
    MessageStatus,
    NotificationChannel,
)
from src.core.security import hash_code
from src.core.templates import TemplateStore, render_string
from src.core.tenants import Tenant
from src.services.notification_service import NotificationService

logger = logging.getLogger(__name__)


class EmailService:
    """Envoi d'emails transactionnels (bienvenue, notification, transaction, custom)."""

    def __init__(
        self,
        redis: Redis,
        settings: Settings,
        notifications: NotificationService,
    ):
        self.redis = redis
        self.settings = settings
        self.notifications = notifications

    async def send(
        self, tenant: Tenant, request: EmailSendRequest
    ) -> EmailSendResponse:
        """Accepte un email et le met en file d'attente (envoi asynchrone)."""
        subject, text_body, html_body = await self._render(request, tenant)

        message_id = await self.notifications.submit(
            tenant,
            to=str(request.to_email),
            channel=NotificationChannel.email,
            subject=subject,
            text=text_body,
            html=html_body,
            language=request.language.value,
            metadata={"kind": "transactional_email", "email_type": request.type.value},
        )

        await self.redis.incr(f"{tenant.key_prefix()}stats:emails_queued")

        logger.info(
            "email_queued",
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
            to_email=str(request.to_email),
            status=MessageStatus.queued,
        )

    # ------------------------------------------------------------------
    # Templates — surcharge tenant (id = type) sinon built-in
    # ------------------------------------------------------------------

    async def _render(
        self, request: EmailSendRequest, tenant: Tenant
    ) -> tuple[str, str, str]:
        if request.type == EmailType.custom:
            return self._render_custom(request, tenant)

        # Un template tenant portant le nom du type (welcome, notification…)
        # surcharge le built-in.
        rendered = await TemplateStore(self.redis, tenant).render(
            request.type.value, request.language.value, request.data
        )
        if rendered is not None:
            subject, text, html = rendered
            return subject, text or "", html or ""

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
        data = {"brand_name": tenant.brand_name, **request.data}
        subject = render_string(request.subject, data) or ""
        text_body = render_string(request.text_body, data) or ""
        html = render_string(request.html_body, data) if request.html_body else None
        if html is None:
            html = self._wrap_html(
                subject, text_body.replace("\n", "<br>"), tenant.brand_name
            )
        return subject, text_body, html

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
