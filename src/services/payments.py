"""Paiements en ligne — Stripe, PayPal, CinetPay (agrégateur mobile money).

Principes :
- Aucun SDK externe : appels REST via httpx, signatures vérifiées à la main.
- Un provider non configuré (clés manquantes) est masqué de l'UI ; la
  validation manuelle par l'admin reste toujours disponible en parallèle.
- Chaque paiement est idempotent : le même tx_id n'applique le plan qu'une
  fois, même si le webhook est rejoué.

Redis :
  pay:tx:{tx_id}              -> JSON {tenant_id, plan, provider, amount, currency, status, ts}
  pay:done:{tx_id}            -> garde d'idempotence (SET NX)
  tenant:{tid}:payments       -> liste LPUSH des tx_id (25 max)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
from datetime import date, timedelta

import httpx

from src.core.tenants import TenantStore, plan_quota_updates

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(20.0)
CINETPAY_API = "https://api-checkout.cinetpay.com/v2"


# ---------------------------------------------------------------------
# Enregistrements
# ---------------------------------------------------------------------


def new_tx_id(prefix: str = "mh") -> str:
    """Référence de transaction courte (≤ 25 chars pour CinetPay)."""
    return f"{prefix}_{secrets.token_hex(8)}"


async def record_tx(redis, tx_id: str, data: dict) -> None:
    await redis.set(f"pay:tx:{tx_id}", json.dumps(data), ex=30 * 24 * 3600)
    tenant_id = data.get("tenant_id")
    if tenant_id:
        key = f"tenant:{tenant_id}:payments"
        await redis.lrem(key, 0, tx_id)
        await redis.lpush(key, tx_id)
        await redis.ltrim(key, 0, 24)


async def get_tx(redis, tx_id: str) -> dict | None:
    raw = await redis.get(f"pay:tx:{tx_id}")
    return json.loads(raw) if raw else None


async def list_payments(redis, tenant_id: str, limit: int = 10) -> list[dict]:
    ids = await redis.lrange(f"tenant:{tenant_id}:payments", 0, limit - 1)
    out = []
    for tx in ids or []:
        t = tx if isinstance(tx, str) else tx.decode()
        data = await get_tx(redis, t)
        if data:
            data["tx_id"] = t
            out.append(data)
    return out


async def set_tx_status(redis, tx_id: str, status: str) -> None:
    data = await get_tx(redis, tx_id)
    if data:
        data["status"] = status
        await redis.set(f"pay:tx:{tx_id}", json.dumps(data), ex=30 * 24 * 3600)


# ---------------------------------------------------------------------
# Application du plan (idempotent) — partagé par tous les providers
# ---------------------------------------------------------------------


async def apply_paid_plan(
    redis,
    *,
    tx_id: str,
    tenant_id: str,
    plan: str,
    provider: str,
    amount: float | int | None = None,
    currency: str = "",
    period: str = "monthly",
) -> bool:
    """Applique un plan après paiement confirmé. Retourne False si déjà fait."""
    marker = await redis.set(f"pay:done:{tx_id}", "1", nx=True, ex=90 * 24 * 3600)
    if not marker:
        logger.info("payment_already_applied", extra={"tx_id": tx_id})
        return False
    store = TenantStore(redis)
    tenant = await store.get(tenant_id)
    if not tenant:
        logger.error("payment_tenant_missing", extra={"tx_id": tx_id, "tenant_id": tenant_id})
        return False
    # Renouvellement : prolonge depuis la fin de période courante si future
    base = date.today()
    if tenant.paid_until and tenant.paid_until > base.isoformat():
        base = date.fromisoformat(tenant.paid_until)
    days = PERIODS.get(period, PERIODS["monthly"])["days"]
    updates = {
        "plan": plan,
        "plan_requested": None,
        "plan_scheduled": None,
        "billing_status": "active",
        "paid_until": (base + timedelta(days=days)).isoformat(),
        "payment_provider": provider,
        "payment_ref": tx_id,
        **plan_quota_updates(plan),
    }
    await store.update(tenant_id, updates)
    await record_tx(
        redis,
        tx_id,
        {
            "tenant_id": tenant_id,
            "plan": plan,
            "provider": provider,
            "amount": amount,
            "currency": currency,
            "status": "paid",
            "period": period,
            "ts": int(time.time()),
            "date": date.today().isoformat(),
        },
    )
    logger.info(
        "payment_applied",
        extra={"tx_id": tx_id, "tenant_id": tenant_id, "plan": plan, "provider": provider},
    )
    return True


# ---------------------------------------------------------------------
# Providers configurés (pour l'UI)
# ---------------------------------------------------------------------


def available_providers(settings) -> dict[str, dict]:
    """Providers actifs : clés présentes + tarif configuré."""
    out: dict[str, dict] = {}
    if settings.stripe_secret_key:
        out["stripe"] = {"label": "Carte bancaire", "via": "Stripe"}
    if settings.paypal_client_id and settings.paypal_client_secret:
        out["paypal"] = {"label": "PayPal", "via": "PayPal"}
    if settings.cinetpay_api_key and settings.cinetpay_site_id:
        out["mobile"] = {"label": "Mobile Money", "via": "Orange / MTN / Wave"}
    return out


# Moyens de paiement hors ligne — toujours proposés, validés manuellement
# par l'administrateur après réception effective du règlement.
MANUAL_METHODS: dict[str, dict] = {
    "virement": {"label": "Virement bancaire", "via": "IBAN / transfert"},
    "momo": {"label": "Mobile Money", "via": "Orange / MTN / Wave / Moov — transfert direct"},
    "cash": {"label": "Espèces / autre moyen", "via": "remise en main propre, Western Union…"},
}


def manual_method_details(settings, method: str) -> str:
    """Coordonnées de réception affichées au client pour un moyen manuel."""
    if method == "virement":
        return settings.billing_bank_details or ""
    if method == "momo":
        parts = [x for x in (settings.billing_momo_number, settings.billing_momo_name) if x]
        return " · ".join(parts)
    if method == "cash":
        return settings.billing_manual_note or ""
    return ""


# Périodes de facturation — comportement SaaS standard
PERIODS: dict[str, dict] = {
    "monthly": {"label": "Mensuel", "days": 30},
    "yearly": {"label": "Annuel", "days": 365},
}


def plan_price(
    settings, plan: str, provider: str, period: str = "monthly"
) -> tuple[float | int, str]:
    """(montant, devise) selon le provider et la période ; (0, '') si non tarifé."""
    mobile = provider == "mobile"
    suffix = "_mobile" if mobile else ""
    if period == "yearly":
        suffix += "_yearly"
    amount = getattr(settings, f"plan_price_{plan}{suffix}", 0) or 0
    currency = settings.billing_currency_mobile if mobile else settings.billing_currency
    return amount, currency


# ---------------------------------------------------------------------
# Création des sessions de paiement
# ---------------------------------------------------------------------


async def create_checkout(
    provider: str,
    plan: str,
    tenant,
    settings,
    base_url: str,
    redis,
    period: str = "monthly",
) -> dict:
    """Retourne {"url": ...} ou {"error": ...}. Enregistre le tx en 'created'."""
    amount, currency = plan_price(settings, plan, provider, period)
    if not amount:
        return {"error": "no_price"}
    tx_id = new_tx_id()
    days = PERIODS.get(period, PERIODS["monthly"])["days"]
    ret = f"{base_url}/dashboard/billing/return"
    try:
        if provider == "stripe":
            url = await _stripe_checkout(settings, tx_id, tenant, plan, amount, currency, ret, days)
        elif provider == "paypal":
            url = await _paypal_checkout(settings, tx_id, tenant, plan, amount, currency, ret, days)
        elif provider == "mobile":
            url = await _cinetpay_checkout(
                settings, tx_id, tenant, plan, amount, currency, ret, days
            )
        else:
            return {"error": "unknown_provider"}
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        logger.warning("checkout_failed", extra={"provider": provider, "err": str(exc)[:200]})
        return {"error": "provider_error"}
    if not url:
        return {"error": "provider_error"}
    await record_tx(
        redis,
        tx_id,
        {
            "tenant_id": tenant.tenant_id,
            "plan": plan,
            "provider": provider,
            "amount": amount,
            "currency": currency,
            "status": "created",
            "period": period,
            "ts": int(time.time()),
            "date": date.today().isoformat(),
        },
    )
    return {"url": url, "tx_id": tx_id, "amount": amount, "currency": currency}


async def _stripe_checkout(
    settings, tx_id, tenant, plan, amount, currency, ret, days: int = 30
) -> str | None:
    params = {
        "mode": "payment",
        "success_url": f"{ret}?p=ok&provider=stripe",
        "cancel_url": f"{ret}?p=cancel&provider=stripe",
        "client_reference_id": tx_id,
        "customer_email": tenant.billing_email or tenant.from_email or "",
        "line_items[0][price_data][currency]": currency.lower(),
        "line_items[0][price_data][unit_amount]": int(round(float(amount) * 100)),
        "line_items[0][price_data][product_data][name]": f"MailHub {plan.upper()} — {days} jours",
        "line_items[0][quantity]": "1",
        "metadata[tx_id]": tx_id,
        "metadata[plan]": plan,
        "metadata[tenant_id]": tenant.tenant_id,
        "payment_intent_data[metadata][tx_id]": tx_id,
        "payment_intent_data[metadata][plan]": plan,
        "payment_intent_data[metadata][tenant_id]": tenant.tenant_id,
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(
            "https://api.stripe.com/v1/checkout/sessions",
            data=params,
            auth=(settings.stripe_secret_key, ""),
        )
    if r.status_code not in (200, 201):
        logger.warning(
            "stripe_checkout_http", extra={"status": r.status_code, "body": r.text[:300]}
        )
        return None
    return r.json().get("url")


async def _paypal_token(settings) -> str | None:
    base = "https://api-m.paypal.com" if settings.paypal_env == "live" else "https://api-m.sandbox.paypal.com"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(
            f"{base}/v1/oauth2/token",
            data={"grant_type": "client_credentials"},
            auth=(settings.paypal_client_id, settings.paypal_client_secret),
        )
    return r.json().get("access_token") if r.status_code == 200 else None


async def _paypal_checkout(
    settings, tx_id, tenant, plan, amount, currency, ret, days: int = 30
) -> str | None:
    token = await _paypal_token(settings)
    if not token:
        return None
    base = "https://api-m.paypal.com" if settings.paypal_env == "live" else "https://api-m.sandbox.paypal.com"
    body = {
        "intent": "CAPTURE",
        "purchase_units": [
            {
                "reference_id": tx_id,
                "custom_id": tx_id,
                "description": f"MailHub {plan.upper()} — {days} jours",
                "amount": {"currency_code": currency, "value": f"{float(amount):.2f}"},
            }
        ],
        "application_context": {
            "return_url": f"{ret}?provider=paypal",
            "cancel_url": f"{ret}?p=cancel&provider=paypal",
        },
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(
            f"{base}/v2/checkout/orders",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )
    if r.status_code not in (200, 201):
        logger.warning("paypal_order_http", extra={"status": r.status_code, "body": r.text[:300]})
        return None
    for link in r.json().get("links", []):
        if link.get("rel") == "approve":
            return link["href"]
    return None


async def _cinetpay_checkout(
    settings, tx_id, tenant, plan, amount, currency, ret, days: int = 30
) -> str | None:
    base = (settings.public_base_url or "").rstrip("/") or ret.rsplit("/dashboard", 1)[0]
    body = {
        "apikey": settings.cinetpay_api_key,
        "site_id": settings.cinetpay_site_id,
        "transaction_id": tx_id,
        "amount": int(amount),
        "currency": currency,
        "description": f"MailHub {plan.upper()} - {days} jours",
        "customer_email": tenant.billing_email or tenant.from_email or "",
        "notify_url": f"{base}/v1/billing/webhooks/cinetpay",
        "return_url": f"{ret}?provider=mobile",
        "channels": "ALL",
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(f"{CINETPAY_API}/payment", json=body)
    if r.status_code not in (200, 201):
        logger.warning(
            "cinetpay_checkout_http", extra={"status": r.status_code, "body": r.text[:300]}
        )
        return None
    data = r.json()
    return (data.get("data") or {}).get("payment_url")


# ---------------------------------------------------------------------
# Vérifications webhook
# ---------------------------------------------------------------------


def verify_stripe_signature(
    body: bytes, sig_header: str, secret: str, tolerance: int = 300
) -> bool:
    """Vérifie l'en-tête Stripe-Signature : t=timestamp,v1=HMAC-SHA256."""
    try:
        parts = dict(p.split("=", 1) for p in sig_header.split(","))
        ts, v1 = parts["t"], parts["v1"]
    except (AttributeError, KeyError, ValueError):
        return False
    if abs(time.time() - int(ts)) > tolerance:
        return False
    expected = hmac.new(
        secret.encode(), f"{ts}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, v1)


async def verify_paypal_webhook(headers: dict, event: dict, settings) -> bool:
    """Vérifie un webhook PayPal via verify-webhook-signature."""
    if not settings.paypal_webhook_id:
        return False
    token = await _paypal_token(settings)
    if not token:
        return False
    base = "https://api-m.paypal.com" if settings.paypal_env == "live" else "https://api-m.sandbox.paypal.com"
    body = {
        "auth_algo": headers.get("paypal-auth-algo", ""),
        "cert_url": headers.get("paypal-cert-url", ""),
        "transmission_id": headers.get("paypal-transmission-id", ""),
        "transmission_sig": headers.get("paypal-transmission-sig", ""),
        "transmission_time": headers.get("paypal-transmission-time", ""),
        "webhook_id": settings.paypal_webhook_id,
        "webhook_event": event,
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(
            f"{base}/v1/notifications/verify-webhook-signature",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )
    return r.status_code == 200 and r.json().get("verification_status") == "SUCCESS"


async def check_cinetpay_status(settings, tx_id: str) -> str | None:
    """État d'une transaction CinetPay : ACCEPTED / REFUSED / PENDING…"""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(
            f"{CINETPAY_API}/payment/check",
            json={
                "apikey": settings.cinetpay_api_key,
                "site_id": settings.cinetpay_site_id,
                "transaction_id": tx_id,
            },
        )
    if r.status_code != 200:
        return None
    return ((r.json().get("data") or {}).get("status"))


async def capture_paypal_order(settings, order_id: str) -> dict | None:
    """Capture un ordre PayPal au retour client. Retourne le JSON ou None."""
    token = await _paypal_token(settings)
    if not token:
        return None
    base = "https://api-m.paypal.com" if settings.paypal_env == "live" else "https://api-m.sandbox.paypal.com"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(
            f"{base}/v2/checkout/orders/{order_id}/capture",
            headers={"Authorization": f"Bearer {token}"},
        )
    return r.json() if r.status_code in (200, 201) else None
