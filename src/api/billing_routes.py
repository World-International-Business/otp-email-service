"""Webhooks de paiement — endpoints publics (vérifiés par signature/lookup).

Ces routes n'utilisent volontairement PAS get_tenant : elles sont appelées
par Stripe, PayPal et CinetPay, jamais par un client API. L'authenticité est
garantie par la signature HMAC (Stripe), verify-webhook-signature (PayPal)
et la re-vérification serveur→serveur (CinetPay).
"""

from __future__ import annotations

import json
import logging

import httpx
from fastapi import APIRouter, HTTPException, Request

from src.config import get_settings
from src.services.payments import (
    apply_paid_plan,
    check_cinetpay_status,
    get_tx,
    set_tx_status,
    verify_paypal_webhook,
    verify_stripe_signature,
)

router = APIRouter(prefix="/v1/billing", tags=["billing"])
logger = logging.getLogger(__name__)


async def _apply_from_tx(redis, tx_id: str, provider: str) -> bool:
    tx = await get_tx(redis, tx_id)
    if not tx or tx.get("status") == "paid":
        return False
    return await apply_paid_plan(
        redis,
        tx_id=tx_id,
        tenant_id=tx["tenant_id"],
        plan=tx["plan"],
        provider=provider,
        amount=tx.get("amount"),
        currency=tx.get("currency", ""),
        period=tx.get("period", "monthly"),
    )


@router.post("/webhooks/stripe", include_in_schema=False)
async def stripe_webhook(request: Request) -> dict:
    settings = get_settings()
    if not settings.stripe_webhook_secret:
        raise HTTPException(status_code=404)
    body = await request.body()
    sig = request.headers.get("stripe-signature", "")
    if not verify_stripe_signature(body, sig, settings.stripe_webhook_secret):
        raise HTTPException(status_code=400, detail="invalid_signature")
    try:
        event = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="bad_json") from None

    if event.get("type") == "checkout.session.completed":
        obj = event.get("data", {}).get("object", {})
        tx_id = (obj.get("metadata") or {}).get("tx_id") or obj.get("client_reference_id")
        if tx_id and obj.get("payment_status") == "paid":
            await _apply_from_tx(request.app.state.redis, tx_id, "stripe")
    return {"received": True}


@router.post("/webhooks/paypal", include_in_schema=False)
async def paypal_webhook(request: Request) -> dict:
    settings = get_settings()
    try:
        event = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="bad_json") from None
    ok = await verify_paypal_webhook(dict(request.headers), event, settings)
    if not ok:
        raise HTTPException(status_code=400, detail="invalid_signature")

    if event.get("event_type") == "PAYMENT.CAPTURE.COMPLETED":
        resource = event.get("resource", {})
        tx_id = resource.get("custom_id") or resource.get("invoice_id")
        if tx_id:
            await _apply_from_tx(request.app.state.redis, tx_id, "paypal")
    return {"received": True}


@router.post("/webhooks/cinetpay", include_in_schema=False)
async def cinetpay_webhook(request: Request) -> dict:
    """Notification CinetPay — la confiance vient du double-check serveur."""
    settings = get_settings()
    if not (settings.cinetpay_api_key and settings.cinetpay_site_id):
        raise HTTPException(status_code=404)
    form = await request.form()
    tx_id = str(form.get("cpm_trans_id") or form.get("transaction_id") or "")
    if not tx_id:
        raise HTTPException(status_code=400, detail="missing_tx")
    # Jamais confiance au payload : on re-vérifie côté serveur CinetPay.
    try:
        status = await check_cinetpay_status(settings, tx_id)
    except httpx.HTTPError:
        raise HTTPException(status_code=503, detail="provider_unreachable") from None
    if status == "ACCEPTED":
        await _apply_from_tx(request.app.state.redis, tx_id, "mobile")
    elif status in ("REFUSED", "CANCELLED"):
        await set_tx_status(request.app.state.redis, tx_id, "failed")
    return {"received": True}
