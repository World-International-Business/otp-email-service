"""Routes FastAPI du microservice OTP + emails transactionnels (multi-tenant)."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from src.api.dependencies import get_client_ip, get_email_service, get_otp_service
from src.core.auth import get_tenant
from src.core.models import (
    EmailSendRequest,
    EmailSendResponse,
    EmailStatusResponse,
    OTPSendRequest,
    OTPSendResponse,
    OTPVerifyRequest,
    OTPVerifyResponse,
)
from src.core.tenants import Tenant
from src.services.email_queue import enqueue, get_status
from src.services.email_service import EmailService
from src.services.idempotency import idem_get, idem_store
from src.services.otp_service import OTPService

router = APIRouter(prefix="/v1/otp")
email_router = APIRouter(prefix="/v1/email")
logger = logging.getLogger(__name__)


@router.post(
    "/send",
    response_model=OTPSendResponse,
    summary="Envoyer un OTP par email ou demander un fallback SMS",
    response_description="Canal choisi et request_id associé",
)
async def send_otp(
    payload: OTPSendRequest,
    request: Request,
    tenant: Tenant = Depends(get_tenant),
    service: OTPService = Depends(get_otp_service),
    client_ip: str = Depends(get_client_ip),
) -> OTPSendResponse:
    redis = request.app.state.redis
    idem_key = request.headers.get("idempotency-key")
    if idem_key:
        # Un retry rejoue la MÊME réponse — sinon un nouveau code serait
        # généré à chaque retry et invaliderait le précédent.
        cached = await idem_get(redis, tenant.tenant_id, idem_key)
        if cached:
            return OTPSendResponse(**cached)
    resp = await service.send_otp(
        tenant=tenant,
        user_id=str(payload.user_id),
        email=str(payload.email),
        country_code=payload.country_code,
        purpose=payload.purpose,
        language=payload.language,
        client_ip=client_ip,
        magic_link=payload.magic_link,
    )
    if idem_key:
        await idem_store(redis, tenant.tenant_id, idem_key, resp.model_dump(mode="json"))
    return resp


@router.post(
    "/verify",
    response_model=OTPVerifyResponse,
    summary="Vérifier un OTP",
    response_description="Résultat de la vérification et tentatives restantes",
)
async def verify_otp(
    payload: OTPVerifyRequest,
    request: Request,
    tenant: Tenant = Depends(get_tenant),
    service: OTPService = Depends(get_otp_service),
) -> OTPVerifyResponse:
    return await service.verify(tenant, payload.request_id, payload.code)


@router.get("/confirm", include_in_schema=False)
async def confirm_otp(
    request: Request,
    request_id: str,
    code: str,
    service: OTPService = Depends(get_otp_service),
):
    """Lien magique — l'URL contient request_id + code (le secret de l'OTP).

    Sans signature HMAC : la connaissance du lien équivaut à connaître le code.
    """
    from fastapi.responses import HTMLResponse

    # Le tenant est encodé dans le lien ("t") — fallback "default" pour
    # les anciens liens. Le lien n'est utilisable que via un email déjà envoyé.
    from src.core.tenants import TenantStore

    tenant_id = request.query_params.get("t", "default")
    tenant = await TenantStore(request.app.state.redis).get(tenant_id)
    if not tenant or not tenant.is_active:
        raise HTTPException(status_code=404)

    result = await service.verify(tenant, request_id, code)

    if result.verified:
        body = """<h1 style="color:#10b981; font-size:26px;">✓ Code vérifié</h1>
<p style="color:#4b5563;">Votre identité est confirmée. Vous pouvez fermer cette page.</p>"""
    else:
        body = """<h1 style="color:#dc2626; font-size:26px;">Lien invalide ou expiré</h1>
<p style="color:#4b5563;">Le code a déjà été utilisé, est erroné ou a expiré.
Demandez un nouveau code.</p>"""

    return HTMLResponse(f"""<!DOCTYPE html><html lang="fr"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vérification</title></head>
<body style="margin:0; font-family:'Segoe UI',Arial,sans-serif; background:#eef0f6;
      display:flex; align-items:center; justify-content:center; min-height:100vh;">
<div style="background:#fff; border-radius:16px; padding:48px; text-align:center;
     max-width:420px; box-shadow:0 8px 30px rgba(0,0,0,.08);">{body}</div></body></html>""")


@email_router.post(
    "/send",
    response_model=EmailSendResponse,
    summary="Envoyer un email transactionnel",
    response_description="message_id de l'envoi (status=queued : livraison async + retries)",
)
async def send_email(
    payload: EmailSendRequest,
    request: Request,
    tenant: Tenant = Depends(get_tenant),
    service: EmailService = Depends(get_email_service),
    client_ip: str = Depends(get_client_ip),
) -> EmailSendResponse:
    redis = request.app.state.redis
    idem_key = request.headers.get("idempotency-key")
    if idem_key:
        cached = await idem_get(redis, tenant.tenant_id, idem_key)
        if cached:
            return EmailSendResponse(**cached)

    if service.settings.email_queue_enabled:
        # Validation + quota à la réception (422/429 immédiat), livraison
        # ensuite par le worker avec retries — l'email n'est jamais perdu.
        subject, text_body, html_body, _limiter = await service.prepare_send(
            tenant, payload, client_ip
        )
        message_id = await enqueue(
            redis, tenant.tenant_id, str(payload.to_email), payload.type.value,
            subject, text_body, html_body, client_ip=client_ip,
        )
        resp = EmailSendResponse(
            sent=True, status="queued",
            message_id=message_id, to_email=payload.to_email,
        )
    else:
        resp = await service.send(tenant, payload, client_ip=client_ip)

    if idem_key:
        await idem_store(redis, tenant.tenant_id, idem_key, resp.model_dump(mode="json"))
    return resp


@email_router.get(
    "/status/{message_id}",
    response_model=EmailStatusResponse,
    summary="Statut d'un envoi (queued/retry/sent/dead)",
    response_description="État courant du message, visible au tenant émetteur uniquement",
)
async def email_status(
    message_id: str,
    request: Request,
    tenant: Tenant = Depends(get_tenant),
) -> EmailStatusResponse:
    st = await get_status(request.app.state.redis, message_id)
    if not st or st.get("tenant_id") != tenant.tenant_id:
        raise HTTPException(status_code=404, detail="message_id introuvable")
    return EmailStatusResponse(
        message_id=message_id,
        status=st.get("status", "queued"),
        to=st.get("to"),
        type=st.get("type"),
        attempts=st.get("attempts", 0),
        error=st.get("error"),
        updated=st.get("updated", 0),
    )
