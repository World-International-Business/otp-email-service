"""API de notifications agnostique — dispatch unifié + templates tenant.

POST /v1/notifications        -> 202 Accepted, envoi délégué au worker
GET  /v1/notifications/{id}   -> statut du message (cycle de vie complet)
PUT  /v1/templates/{id}       -> créer/mettre à jour un template tenant
GET  /v1/templates            -> lister les templates du tenant
GET  /v1/templates/{id}       -> détail d'un template
DELETE /v1/templates/{id}     -> supprimer un template
"""

import re

from fastapi import APIRouter, Depends, HTTPException, Request, status

from src.api.dependencies import get_notification_service
from src.core.auth import get_tenant
from src.core.models import (
    MessageStatus,
    MessageStatusEvent,
    MessageStatusResponse,
    NotificationAcceptedResponse,
    NotificationChannel,
    NotificationRequest,
    TemplateResponse,
    TemplateUpsertRequest,
    TemplateVariant,
)
from src.core.templates import TemplateStore
from src.core.tenants import Tenant
from src.services.notification_service import NotificationService

notification_router = APIRouter(prefix="/v1", tags=["notifications"])

_TEMPLATE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


@notification_router.post(
    "/notifications",
    response_model=NotificationAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Dispatcher une notification (agnostique, asynchrone)",
    response_description="Message accepté et mis en file d'attente",
)
async def submit_notification(
    payload: NotificationRequest,
    tenant: Tenant = Depends(get_tenant),
    service: NotificationService = Depends(get_notification_service),
) -> NotificationAcceptedResponse:
    if payload.channel != NotificationChannel.email:
        raise HTTPException(
            status_code=422,
            detail=f"Channel '{payload.channel.value}' is not supported yet",
        )

    message_id = await service.submit(
        tenant,
        to=str(payload.to),
        channel=payload.channel,
        template_id=payload.template_id,
        subject=payload.subject,
        text=payload.text,
        html=payload.html,
        data=payload.data,
        language=payload.language.value,
        metadata=payload.metadata,
    )
    return NotificationAcceptedResponse(
        message_id=message_id,
        status=MessageStatus.queued,
        channel=payload.channel,
        to=str(payload.to),
    )


@notification_router.get(
    "/notifications/{message_id}",
    response_model=MessageStatusResponse,
    summary="Statut d'une notification",
    response_description="Cycle de vie : queued, processing, sent, failed…",
)
async def get_notification_status(
    message_id: str,
    tenant: Tenant = Depends(get_tenant),
    service: NotificationService = Depends(get_notification_service),
) -> MessageStatusResponse:
    record = await service.get_record(tenant.tenant_id, message_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Message not found")
    return MessageStatusResponse(
        message_id=record["message_id"],
        channel=NotificationChannel(record["channel"]),
        to=record["to"],
        subject=record.get("subject"),
        status=MessageStatus(record["status"]),
        attempts=record["attempts"],
        metadata=record.get("metadata") or {},
        error=record.get("error"),
        history=[MessageStatusEvent(**e) for e in record.get("history", [])],
        created_at=record["created_at"],
        updated_at=record["updated_at"],
    )


# ------------------------------------------------------------------
# Templates — gérés par le tenant lui-même
# ------------------------------------------------------------------


@notification_router.put(
    "/templates/{template_id}",
    response_model=TemplateResponse,
    summary="Créer ou mettre à jour un template",
)
async def upsert_template(
    template_id: str,
    payload: TemplateUpsertRequest,
    request: Request,
    tenant: Tenant = Depends(get_tenant),
) -> TemplateResponse:
    if not _TEMPLATE_ID_RE.match(template_id):
        raise HTTPException(
            status_code=422,
            detail="template_id must match ^[a-zA-Z0-9_-]{1,64}$",
        )
    store = TemplateStore(request.app.state.redis, tenant)
    record = await store.upsert(template_id, payload.variants, payload.description)
    return _template_response(record)


@notification_router.get(
    "/templates",
    response_model=list[TemplateResponse],
    summary="Lister les templates du tenant",
)
async def list_templates(
    request: Request, tenant: Tenant = Depends(get_tenant)
) -> list[TemplateResponse]:
    store = TemplateStore(request.app.state.redis, tenant)
    return [_template_response(r) for r in await store.list()]


@notification_router.get(
    "/templates/{template_id}",
    response_model=TemplateResponse,
    summary="Détail d'un template",
)
async def get_template(
    template_id: str,
    request: Request,
    tenant: Tenant = Depends(get_tenant),
) -> TemplateResponse:
    store = TemplateStore(request.app.state.redis, tenant)
    record = await store.get(template_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Template not found")
    return _template_response(record)


@notification_router.delete(
    "/templates/{template_id}",
    summary="Supprimer un template",
)
async def delete_template(
    template_id: str,
    request: Request,
    tenant: Tenant = Depends(get_tenant),
):
    store = TemplateStore(request.app.state.redis, tenant)
    if not await store.delete(template_id):
        raise HTTPException(status_code=404, detail="Template not found")
    return {"deleted": True, "template_id": template_id}


def _template_response(record: dict) -> TemplateResponse:
    return TemplateResponse(
        template_id=record["template_id"],
        variants={k: TemplateVariant(**v) for k, v in record["variants"].items()},
        description=record.get("description"),
        created_at=record["created_at"],
        updated_at=record["updated_at"],
    )
