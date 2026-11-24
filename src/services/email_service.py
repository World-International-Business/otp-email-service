"""Service d'envoi d'emails transactionnels génériques — multi-tenant."""

import logging
import re
import secrets
from html import escape

from fastapi import HTTPException
from redis.asyncio import Redis

from src.config import Settings
from src.core.models import EmailSendRequest, EmailSendResponse, EmailType, Language
from src.core.security import hash_code
from src.core.tenants import Tenant
from src.email_providers.base import EmailProvider, EmailSendError, SenderIdentity
from src.services.custom_templates import get_template, render_vars
from src.services.email_queue import set_status
from src.services.email_templates import generic_html, resolve_logo_url
from src.services.events import incr_daily_stat, log_event, mask_email
from src.services.failover import send_with_failover
from src.services.quotas import check_quota, seconds_until_next_month
from src.services.rate_limiter import RateLimiter
from src.services.sandbox import capture_send
from src.services.suppression import suppression_reason
from src.services.tracking import (
    inject_tracking,
    tracking_base_url,
    tracking_enabled,
)
from src.services.unsubscribe import unsubscribe_url
from src.services.warmup import seconds_until_midnight, warmup_state
from src.services.webhooks import enqueue_webhook

logger = logging.getLogger(__name__)

# Documents HTML complets fournis en type=custom : extraction du corps
# (et des feuilles de style) pour les insérer dans l'enveloppe de marque.
_DOC_BODY_RE = re.compile(r"<body[^>]*>(.*)</body\s*>", re.IGNORECASE | re.DOTALL)
_DOC_STYLE_RE = re.compile(r"<style[^>]*>.*?</style\s*>", re.IGNORECASE | re.DOTALL)


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

    async def prepare_send(
        self, tenant: Tenant, request: EmailSendRequest, client_ip: str = "unknown"
    ) -> tuple[str, str, str, RateLimiter]:
        """Rend le contenu + réserve le quota destinataire (422/429 sinon)."""
        to_email = str(request.to_email)
        # Rendre AVANT de réserver un slot quota : un type=custom incomplet
        # lève 422 ici — il ne doit pas consommer le quota du destinataire.
        override = await get_template(
            self.redis, tenant.tenant_id, request.type.value)
        subject, text_body, html_body = self._render(
            request, tenant, override,
            unsubscribe_url=self._unsubscribe_url(tenant, to_email))

        # Liste de suppression : une adresse en bounce/plainte est refusée
        # avant toute consommation de quota ou appel SMTP.
        reason = await suppression_reason(
            self.redis, tenant.tenant_id, to_email)
        if reason:
            await log_event(
                self.redis, tenant, "email_suppressed",
                email=mask_email(to_email), reason=reason,
            )
            raise HTTPException(
                status_code=422,
                detail="Recipient address is suppressed "
                       "(previous bounce or complaint)",
            )

        # Warm-up expéditeur : le SMTP neuf du tenant est plafonné par jour.
        remaining, cap, _ = await warmup_state(self.redis, self.settings, tenant)
        if remaining == 0:
            retry = seconds_until_midnight()
            await log_event(
                self.redis, tenant, "warmup_limited",
                email=mask_email(to_email), daily_cap=cap,
            )
            raise HTTPException(
                status_code=429,
                detail=f"Sender warm-up in progress — daily cap "
                       f"{cap} reached, increases automatically",
                headers={"Retry-After": str(retry)},
            )

        # Quota mensuel du plan (0/illimité = pas de contrôle).
        allowed_q, used_q, quota_q = await check_quota(
            self.redis, tenant, "emails_sent")
        if not allowed_q:
            retry = seconds_until_next_month()
            await log_event(
                self.redis, tenant, "email_quota_exceeded",
                email=mask_email(to_email), used=used_q, quota=quota_q,
            )
            raise HTTPException(
                status_code=429,
                detail=f"Monthly plan quota reached ({used_q}/{quota_q} emails) "
                       "— upgrade your plan",
                headers={"Retry-After": str(retry)},
            )

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
        return subject, text_body, html_body, limiter

    def _unsubscribe_url(self, tenant: Tenant, to_email: str) -> str:
        """Lien opt-out : URL statique configurée sinon endpoint signé auto."""
        if self.settings.list_unsubscribe_url:
            return self.settings.list_unsubscribe_url
        return unsubscribe_url(self.settings, tenant.tenant_id, to_email)

    async def deliver(
        self,
        tenant: Tenant,
        to_email: str,
        subject: str,
        text_body: str,
        html_body: str,
        message_id: str,
        email_type: str,
        client_ip: str = "unknown",
        attachments: list[dict] | None = None,
    ) -> None:
        """Livraison SMTP + stats + événements + webhook `email.sent`.

        Ne touche pas au rate limiting (réservé à l'amont, rendu au dead).
        Lève EmailSendError — l'appelant décide : 502 (synchrone) ou retry
        (file d'attente).
        """
        # Tracking ouvertures/clics (transactionnel — jamais les OTP).
        if tracking_enabled(self.settings, tenant):
            html_body = inject_tracking(
                html_body, message_id,
                tracking_base_url(self.settings, tenant))

        if tenant.sandbox:
            # Sandbox : pipeline complet mais capture au lieu de l'envoi SMTP.
            await capture_send(
                self.redis, tenant.tenant_id, to=to_email, subject=subject,
                text_body=text_body, html_body=html_body,
                kind=email_type, ref=message_id,
            )
            via = "sandbox"
        else:
            try:
                sender = self._sender(tenant)
                if unsub := self._unsubscribe_url(tenant, to_email):
                    sender["unsubscribe_url"] = unsub
                _success, via_failover = await send_with_failover(
                    self.email_provider, self.settings, tenant, self.redis,
                    to_email=to_email, subject=subject, text_body=text_body,
                    html_body=html_body, sender=sender,
                    context={"message_id": message_id, "type": email_type},
                    attachments=attachments,
                )
            except EmailSendError as exc:
                raise EmailSendError(str(exc)[:150]) from exc
            via = "failover" if via_failover else "tenant"

        await self.redis.incr(f"{tenant.key_prefix()}stats:emails_sent")
        await incr_daily_stat(self.redis, tenant, "emails_sent")
        await log_event(
            self.redis, tenant, "email_sent",
            email=mask_email(to_email),
            email_type=email_type, message_id=message_id,
            via=via,
            client_ip=client_ip,
        )
        logger.info(
            "email_sent",
            extra={
                "tenant_id": tenant.tenant_id,
                "message_id": message_id,
                "type": email_type,
                "to_email_hash": hash_code(to_email),
            },
        )
        await enqueue_webhook(
            self.redis, tenant, "email.sent",
            {"message_id": message_id, "to": to_email,
             "type": email_type, "subject": subject,
             "via": via},
        )

    async def send(
        self, tenant: Tenant, request: EmailSendRequest, client_ip: str = "unknown"
    ) -> EmailSendResponse:
        """Envoie synchrone (test dashboard, appels internes). Lève 429/502."""
        to_email = str(request.to_email)
        subject, text_body, html_body, limiter = await self.prepare_send(
            tenant, request, client_ip
        )
        message_id = f"msg_{secrets.token_urlsafe(16)}"

        try:
            await self.deliver(
                tenant, to_email, subject, text_body, html_body,
                message_id, request.type.value, client_ip,
                attachments=[a.model_dump() for a in request.attachments] or None,
            )
        except EmailSendError as exc:
            reason = str(exc)[:150]
            await limiter.release_email(to_email)
            logger.error(
                "email_send_failed",
                extra={
                    "tenant_id": tenant.tenant_id,
                    "message_id": message_id,
                    "type": request.type.value,
                    "to_email_hash": hash_code(to_email),
                },
            )
            await log_event(
                self.redis, tenant, "email_failed",
                email=mask_email(to_email),
                email_type=request.type.value,
            )
            await enqueue_webhook(
                self.redis, tenant, "email.failed",
                {"message_id": message_id, "to": to_email,
                 "type": request.type.value, "error": reason,
                 "will_retry": False},
            )
            raise HTTPException(
                status_code=502, detail=f"Failed to send email: {reason}"
            ) from exc

        await set_status(
            self.redis, message_id, tenant.tenant_id, "sent",
            to=to_email, type_=request.type.value, attempts=1,
        )
        return EmailSendResponse(
            sent=True, status="sent",
            message_id=message_id,
            to_email=request.to_email,
        )

    # ------------------------------------------------------------------
    # Templates — brand_name injecté par tenant
    # ------------------------------------------------------------------

    def _render(
        self,
        request: EmailSendRequest,
        tenant: Tenant,
        override: dict[str, str] | None = None,
        unsubscribe_url: str = "",
    ) -> tuple[str, str, str]:
        if request.type == EmailType.custom:
            return self._render_custom(request, tenant, unsubscribe_url)

        builders = {
            EmailType.welcome: self._welcome,
            EmailType.notification: self._notification,
            EmailType.transaction: self._transaction,
        }
        return builders[request.type](request, tenant, override, unsubscribe_url)

    def _apply_override(
        self,
        override: dict[str, str],
        variables: dict,
        default_subject: str,
        default_text: str,
        tenant: Tenant,
        unsubscribe_url: str = "",
    ) -> tuple[str, str, str]:
        """Rend le template personnalisé du tenant ({vars} substituées).

        Le fragment HTML personnalisé est inséré brut — il appartient au
        tenant qui maîtrise ses propres emails (même confiance que
        type=custom). Le texte est échappé."""
        subject = (
            render_vars(override["subject"], variables)
            or default_subject
        )
        text = (
            render_vars(override["text"], variables)
            if override["text"] else default_text
        )
        if override["html"]:
            html = self._wrap_html(
                None, render_vars(override["html"], variables), tenant,
                unsubscribe_url=unsubscribe_url)
        else:
            html = self._wrap_html(
                subject, escape(text).replace("\n", "<br>"), tenant,
                unsubscribe_url=unsubscribe_url)
        return subject, text, html

    def _render_custom(
        self, request: EmailSendRequest, tenant: Tenant,
        unsubscribe_url: str = "",
    ) -> tuple[str, str, str]:
        if not request.subject or not request.text_body:
            raise HTTPException(
                status_code=422,
                detail="subject and text_body are required when type=custom",
            )
        html = request.html_body
        if html and "<html" in html.lower():
            # Document HTML complet : le contenu du <body> est extrait et
            # inséré dans l'enveloppe de marque — le branding (couleur,
            # logo, pied de page) s'applique à TOUS les emails du tenant.
            # Les <style> du document remontent dans le <head> de
            # l'enveloppe : dans le corps ils seraient ignorés par Gmail.
            body = _DOC_BODY_RE.search(html)
            if body:
                styles = "".join(_DOC_STYLE_RE.findall(html))
                html = self._wrap_html(
                    None, body.group(1), tenant, head_extra=styles,
                    unsubscribe_url=unsubscribe_url)
            # <html> sans <body> (document mal formé) : envoyé tel quel.
        else:
            # Fragment HTML ou texte : enrobé dans le template de marque
            # (couleur, logo, pied de page du tenant). Un fragment fourni par
            # le client porte déjà son propre titre : on n'injecte pas le
            # subject en <h1> — sinon deux titres s'affichent.
            html = self._wrap_html(
                None if html else request.subject,
                html if html else escape(request.text_body).replace("\n", "<br>"),
                tenant, unsubscribe_url=unsubscribe_url,
            )
        return request.subject, request.text_body, html

    def _welcome(
        self,
        request: EmailSendRequest,
        tenant: Tenant,
        override: dict[str, str] | None = None,
        unsubscribe_url: str = "",
    ) -> tuple[str, str, str]:
        brand = tenant.brand_name
        name = request.data.get("name", "")
        if override:
            return self._apply_override(
                override, {"name": name, "brand": brand},
                f"Bienvenue sur {brand} !" if request.language == Language.fr
                else f"Welcome to {brand}!",
                f"Bonjour {name},\n\nBienvenue sur {brand}.",
                tenant, unsubscribe_url,
            )
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
        html = self._wrap_html(
            subject, escape(text).replace("\n", "<br>"), tenant,
            unsubscribe_url=unsubscribe_url)
        return subject, text, html

    def _notification(
        self,
        request: EmailSendRequest,
        tenant: Tenant,
        override: dict[str, str] | None = None,
        unsubscribe_url: str = "",
    ) -> tuple[str, str, str]:
        brand = tenant.brand_name
        title = request.data.get("title", "Notification")
        message = request.data.get("message", "")
        subject = f"{brand} — {title}"
        if override:
            return self._apply_override(
                override,
                {"title": title, "message": message, "brand": brand},
                subject, f"{title}\n\n{message}", tenant, unsubscribe_url,
            )
        if request.language == Language.fr:
            text = f"{title}\n\n{message}\n\nL'équipe {brand}"
        else:
            text = f"{title}\n\n{message}\n\nThe {brand} Team"
        html = self._wrap_html(
            title, escape(message).replace("\n", "<br>"), tenant,
            unsubscribe_url=unsubscribe_url)
        return subject, text, html

    def _transaction(
        self,
        request: EmailSendRequest,
        tenant: Tenant,
        override: dict[str, str] | None = None,
        unsubscribe_url: str = "",
    ) -> tuple[str, str, str]:
        brand = tenant.brand_name
        amount = request.data.get("amount", "")
        currency = request.data.get("currency", "")
        status = request.data.get("status", "confirmed")
        if override:
            return self._apply_override(
                override,
                {"amount": amount, "currency": currency,
                 "status": status, "brand": brand},
                f"Votre transaction {brand}"
                if request.language == Language.fr
                else f"Your {brand} transaction",
                f"Transaction : {amount} {currency}\nStatut : {status}",
                tenant, unsubscribe_url,
            )
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
        html = self._wrap_html(
            subject, escape(text).replace("\n", "<br>"), tenant,
            unsubscribe_url=unsubscribe_url)
        return subject, text, html

    def _wrap_html(
        self, title: str | None, content_html: str, tenant_or_brand,
        head_extra: str = "",
        unsubscribe_url: str = "",
    ) -> str:
        if isinstance(tenant_or_brand, Tenant):
            t = tenant_or_brand
            return generic_html(
                title, content_html, t.brand_name,
                color=t.brand_color,
                logo_url=resolve_logo_url(t.email_logo_url, self.settings.public_base_url),
                footer=t.email_footer,
                head_extra=head_extra,
                unsubscribe_url=unsubscribe_url,
            )
        return generic_html(title, content_html, tenant_or_brand,
                            head_extra=head_extra)
