"""Désinscription destinataire — GET (lien footer) + POST (RFC 8058).

Le lien signé est l'authentification : sig = HMAC(tenant_id|email).
Aucun header requis — cliquable depuis n'importe quel client mail.
Le POST sans corps `List-Unsubscribe=One-Click` est le geste un clic
de Gmail/Yahoo (header `List-Unsubscribe-Post` émis par le provider SMTP).
"""

import html
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from src.core.tenants import TenantStore
from src.services.events import incr_daily_stat, log_event, mask_email
from src.services.suppression import add_suppression
from src.services.unsubscribe import verify_sig
from src.services.webhooks import enqueue_webhook

logger = logging.getLogger(__name__)

unsub_router = APIRouter(prefix="/v1", include_in_schema=False)


async def _do_unsubscribe(request: Request, t: str, e: str, sig: str) -> str:
    """Vérifie + applique l'opt-out. Retourne le nom de marque du tenant."""
    settings = request.app.state.settings
    if not t or not e or "@" not in e or not verify_sig(settings, t, e, sig):
        raise HTTPException(status_code=403, detail="Invalid unsubscribe link")

    redis = request.app.state.redis
    tenant = await TenantStore(redis).get(t)
    if not tenant:
        raise HTTPException(status_code=404, detail="Unknown tenant")

    await add_suppression(redis, t, e, "unsubscribe", "user_optout")
    await incr_daily_stat(redis, tenant, "emails_unsubscribed")
    await log_event(
        redis, tenant, "email_unsubscribed", email=mask_email(e),
    )
    await enqueue_webhook(
        redis, tenant, "email.unsubscribed", {"to": e},
    )
    return tenant.brand_name or "MailHub"


@unsub_router.get("/unsubscribe", response_class=HTMLResponse)
async def unsubscribe_get(request: Request, t: str, e: str, sig: str):
    brand = html.escape(await _do_unsubscribe(request, t, e, sig))
    return HTMLResponse(f"""<!DOCTYPE html><html lang="fr"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Désinscription</title></head>
<body style="margin:0; font-family:'Segoe UI',Arial,sans-serif; background:#eef0f6;
      display:flex; align-items:center; justify-content:center; min-height:100vh;">
<div style="background:#fff; border-radius:16px; padding:32px 24px; text-align:center;
     max-width:420px; width:100%; margin:0 16px; box-sizing:border-box;
     box-shadow:0 8px 30px rgba(0,0,0,.08);">
<h1 style="color:#10b981; font-size:24px;">Vous êtes désinscrit</h1>
<p style="color:#4b5563;">Cette adresse ne recevra plus d'emails de la part de
{brand} via MailHub.</p>
</div></body></html>""")


@unsub_router.post("/unsubscribe")
async def unsubscribe_post(request: Request, t: str, e: str, sig: str):
    """RFC 8058 one-click — Gmail/Yahoo postent `List-Unsubscribe=One-Click`
    sur l'URL du header List-Unsubscribe. Même sémantique que le GET."""
    await _do_unsubscribe(request, t, e, sig)
    return {"unsubscribed": True}
