"""Routes publiques de tracking — pixel ouverture + redirection clics.

Le message_id sert de token capacité (~128 bits aléatoires, jamais
listable). Le statut mail donne le tenant pour l'attribution des stats.
"""

import logging

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from src.core.tenants import TenantStore
from src.services.email_queue import get_status
from src.services.events import incr_daily_stat, log_event, mask_email
from src.services.tracking import PIXEL_GIF
from src.services.webhooks import enqueue_webhook

logger = logging.getLogger(__name__)

track_router = APIRouter(prefix="/v1/track", include_in_schema=False)

_PIXEL_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Pragma": "no-cache",
}


async def _tenant_for(request: Request, message_id: str):
    st = await get_status(request.app.state.redis, message_id)
    if not st or not st.get("tenant_id"):
        return None, None
    tenant = await TenantStore(request.app.state.redis).get(st["tenant_id"])
    return st, tenant


@track_router.get("/o/{message_id}.gif")
async def track_open(message_id: str, request: Request) -> Response:
    st, tenant = await _tenant_for(request, message_id)
    if tenant:
        redis = request.app.state.redis
        await redis.incr(f"{tenant.key_prefix()}stats:emails_opened")
        await incr_daily_stat(redis, tenant, "emails_opened")
        await log_event(
            redis, tenant, "email_opened",
            email=mask_email(st.get("to") or ""),
            message_id=message_id,
        )
        await enqueue_webhook(
            redis, tenant, "email.opened",
            {"message_id": message_id, "to": st.get("to")},
        )
    return Response(
        content=PIXEL_GIF, media_type="image/gif", headers=_PIXEL_HEADERS)


@track_router.get("/c/{message_id}")
async def track_click(
    message_id: str, request: Request, u: str = ""
) -> RedirectResponse:
    if not u.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Invalid target URL")
    st, tenant = await _tenant_for(request, message_id)
    if tenant:
        redis = request.app.state.redis
        await redis.incr(f"{tenant.key_prefix()}stats:emails_clicked")
        await incr_daily_stat(redis, tenant, "emails_clicked")
        await log_event(
            redis, tenant, "email_clicked",
            email=mask_email(st.get("to") or ""),
            message_id=message_id, url=u[:200],
        )
        await enqueue_webhook(
            redis, tenant, "email.clicked",
            {"message_id": message_id, "to": st.get("to"), "url": u[:300]},
        )
    return RedirectResponse(u, status_code=302)
