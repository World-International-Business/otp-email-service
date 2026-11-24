"""Bascule d'expéditeur : SMTP personnalisé du tenant en panne → plateforme.

Si un tenant a configuré son propre serveur SMTP (`tenant.smtp_host`) et que
l'envoi échoue, on retente UNE fois via l'expéditeur plateforme (config
globale). Le mail part alors de `settings.from_email` avec un nom affiché
« <Marque> via <Plateforme> » — moins idéal que le SMTP du tenant, mais bien
mieux qu'un OTP jamais livré. Un événement `smtp_failover` + webhook
`tenant.smtp_failover` signalent la bascule pour que le tenant répare.
"""

import logging
from typing import Any

from redis.asyncio import Redis

from src.config import Settings
from src.core.tenants import Tenant
from src.email_providers.base import (
    EmailProvider,
    EmailSendError,
    SenderIdentity,
)
from src.services.events import log_event, mask_email
from src.services.webhooks import enqueue_webhook

logger = logging.getLogger(__name__)


def failover_eligible(settings: Settings, tenant: Tenant) -> bool:
    """La bascule n'a de sens que si le tenant utilise SON serveur SMTP —
    sinon primary == plateforme, retenter serait un doublon."""
    return bool(settings.email_failover_enabled and tenant.smtp_host)


def platform_sender(settings: Settings, tenant: Tenant) -> SenderIdentity:
    """Expéditeur de secours : transport ET adresse From de la plateforme
    (obligatoire pour SPF/DMARC — on ne peut pas usurper le domaine du
    tenant depuis notre serveur). Le nom affiché garde la marque du tenant."""
    brand = tenant.from_name or tenant.brand_name or ""
    name = (
        f"{brand} via {settings.from_name}"
        if brand and brand != settings.from_name
        else settings.from_name
    )
    # Aucune clé smtp_* → le provider retombe sur les settings globaux.
    return SenderIdentity(from_email=settings.from_email, from_name=name)


async def send_with_failover(
    provider: EmailProvider,
    settings: Settings,
    tenant: Tenant,
    redis: Redis,
    *,
    to_email: str,
    subject: str,
    text_body: str,
    html_body: str,
    sender: SenderIdentity,
    context: dict[str, Any] | None = None,
    attachments: list[dict] | None = None,
) -> tuple[bool, bool]:
    """Envoie via `sender`; bascule plateforme si le SMTP tenant échoue.

    Retourne (succès, via_failover). Ne lève PAS EmailSendError : la raison
    finale est laissée à l'appelant via le premier élément du tuple — False
    et la dernière erreur est dans les logs + event `smtp_failover`.
    """
    primary_err = ""
    try:
        if await provider.send_email(
            to_email, subject, text_body, html_body, sender=sender,
            attachments=attachments,
        ):
            return True, False
        primary_err = "provider returned False"
    except EmailSendError as exc:
        primary_err = str(exc)[:120]

    if not failover_eligible(settings, tenant):
        raise EmailSendError(primary_err)

    fb_err = ""
    try:
        ok = await provider.send_email(
            to_email, subject, text_body, html_body,
            sender=platform_sender(settings, tenant),
            attachments=attachments,
        )
    except EmailSendError as exc:
        ok, fb_err = False, str(exc)[:80]

    if not ok:
        raise EmailSendError(
            f"{primary_err} — fallback: {fb_err or 'provider returned False'}"
        )

    logger.warning(
        "smtp_failover_used",
        extra={"tenant_id": tenant.tenant_id, "smtp_host": tenant.smtp_host},
    )
    await log_event(
        redis, tenant, "smtp_failover",
        email=mask_email(to_email), smtp_host=tenant.smtp_host,
        **(context or {}),
    )
    await enqueue_webhook(
        redis, tenant, "tenant.smtp_failover",
        {"to": to_email, "smtp_host": tenant.smtp_host,
         "primary_error": primary_err, **(context or {})},
    )
    return True, True
