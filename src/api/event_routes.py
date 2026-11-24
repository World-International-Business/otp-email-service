"""Réception des événements email entrants — bounces et plaintes spam.

Les fournisseurs (Brevo, Resend, relais SMTP perso) notifient les
bounces/plaintes par webhook. Ce endpoint les normalise et alimente la
liste de suppression + les stats de réputation.

POST /v1/events/email   — header X-Admin-Key (niveau plateforme)

Formats acceptés :
- générique : {"event": "hard_bounce", "email": "x@y", "tenant_id": "t1"?}
- Brevo     : {"event": "hard_bounce", "email": "x@y", "ts": ..., ...}
- Resend    : {"type": "email.bounced", "data": {"to": ["x@y"], ...}}

Sans ``tenant_id``, un hard bounce ou une plainte supprime l'adresse au
niveau PLATEFORME (une boîte morte l'est pour tous les tenants).
"""

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.core.auth import require_admin
from src.core.tenants import TenantStore
from src.services.events import incr_daily_stat, log_event, mask_email
from src.services.suppression import GLOBAL, add_suppression
from src.services.webhooks import enqueue_webhook

logger = logging.getLogger(__name__)

events_router = APIRouter(
    prefix="/v1/events", dependencies=[require_admin],
    include_in_schema=False,
)

_HARD = {
    "hard_bounce", "bounce", "bounced", "email.bounced",
    "invalid_email", "blocked", "dropped", "email.bounced_permanent",
}
_SOFT = {
    "soft_bounce", "deferred", "email.deferred", "email.delivery_delayed",
    "email.bounced_temporary",
}
_COMPLAINT = {
    "complaint", "spam", "spam_complaint", "email.complained", "abuse",
}


class EmailEventIn(BaseModel):
    event: str | None = None          # générique / Brevo
    type: str | None = None           # Resend
    email: str | list[str] | None = None
    emails: list[str] | None = None
    tenant_id: str | None = None
    reason: str | None = None
    data: dict | None = None          # Resend payload


def _normalize(payload: EmailEventIn) -> tuple[str, list[str]]:
    """Extrait (kind, emails) du payload multi-format.

    kind ∈ hard | soft | complaint — lève 422 si l'événement est inconnu.
    """
    name = (payload.event or payload.type or "").strip().lower()

    emails: list[str] = []
    if payload.data and isinstance(payload.data, dict):
        v = payload.data.get("to") or payload.data.get("email") or []
        emails = v if isinstance(v, list) else [v]
    if not emails:
        v = payload.email or payload.emails or []
        emails = v if isinstance(v, list) else [v]
    emails = [e.strip().lower() for e in emails if isinstance(e, str) and "@" in e]

    if name in _COMPLAINT:
        kind = "complaint"
    elif name in _HARD:
        kind = "hard"
    elif name in _SOFT:
        kind = "soft"
    else:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown email event '{name}' — expected one of "
                   f"{sorted(_HARD | _SOFT | _COMPLAINT)}",
        )
    if not emails:
        raise HTTPException(status_code=422, detail="No recipient email in payload")
    return kind, emails


@events_router.post("/email")
async def ingest_email_event(
    payload: EmailEventIn, request: Request
) -> dict:
    """Enregistre un bounce/plainte : suppression + stats + event tenant."""
    redis = request.app.state.redis
    kind, emails = _normalize(payload)

    tenant = None
    if payload.tenant_id:
        tenant = await TenantStore(redis).get(payload.tenant_id)

    reason = payload.reason or kind
    processed = []
    for email in emails:
        if kind in ("hard", "complaint"):
            # Hard bounce / plainte : boîte morte ou hostile — suppression
            # globale plateforme + entrée tenant si connu.
            await add_suppression(redis, GLOBAL, email, reason, source="provider")
            if tenant:
                await add_suppression(
                    redis, tenant.tenant_id, email, reason, source="provider")
        else:
            # Soft bounce : compté (réputation) mais pas supprimé —
            # la boîte peut être pleine ou le serveur momentanément HS.
            pass

        if tenant:
            metric = {"hard": "emails_bounced", "soft": "emails_softbounced",
                      "complaint": "emails_complained"}[kind]
            await incr_daily_stat(redis, tenant, metric)
            await redis.incr(f"{tenant.key_prefix()}stats:{metric}")
            ev_type = {"hard": "email_bounced", "soft": "email_softbounced",
                       "complaint": "email_complained"}[kind]
            await log_event(
                redis, tenant, ev_type,
                email=mask_email(email), reason=reason,
            )
            await enqueue_webhook(
                redis, tenant,
                {"hard": "recipient.bounced",
                 "soft": "recipient.deferred",
                 "complaint": "recipient.complained"}[kind],
                {"email": email, "kind": kind, "reason": reason},
            )
        processed.append(email)

    logger.info(
        "email_event_ingested",
        extra={"kind": kind, "count": len(processed),
               "tenant_id": payload.tenant_id},
    )
    return {"processed": len(processed), "kind": kind,
            "suppressed": kind in ("hard", "complaint")}
