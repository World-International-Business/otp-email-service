"""Routes FastAPI du microservice OTP + emails transactionnels (multi-tenant)."""

import logging

from fastapi import APIRouter, Depends, Request

from src.api.dependencies import get_client_ip, get_email_service, get_otp_service
from src.core.auth import get_tenant
from src.core.models import (
    EmailSendRequest,
    EmailSendResponse,
    OTPSendRequest,
    OTPSendResponse,
    OTPVerifyRequest,
    OTPVerifyResponse,
)
from src.core.tenants import Tenant
from src.services.email_service import EmailService
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
    return await service.send_otp(
        tenant=tenant,
        user_id=str(payload.user_id),
        email=str(payload.email),
        country_code=payload.country_code,
        purpose=payload.purpose,
        language=payload.language,
        client_ip=client_ip,
    )


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


@email_router.post(
    "/send",
    response_model=EmailSendResponse,
    status_code=202,
    summary="Envoyer un email transactionnel (asynchrone)",
    response_description="message_id — suivi via GET /v1/notifications/{message_id}",
)
async def send_email(
    payload: EmailSendRequest,
    request: Request,
    tenant: Tenant = Depends(get_tenant),
    service: EmailService = Depends(get_email_service),
    client_ip: str = Depends(get_client_ip),
) -> EmailSendResponse:
    return await service.send(tenant, payload)
