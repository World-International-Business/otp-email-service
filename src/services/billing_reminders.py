"""Rappels d'expiration d'abonnement — emails automatiques J-7 / J-3 / J-1.

Envoyés par la boucle billing de main.py aux comptes dashboard du tenant,
via l'expéditeur GLOBAL de la plateforme (pas le SMTP du client — c'est un
email de MailHub). Chaque rappel est dédupliqué par une clé Redis, et un
email "expiré" part quand le plan est rétrogradé.
"""

from __future__ import annotations

import logging
from datetime import date
from html import escape as _e

from src.core.tenants import TenantStore
from src.services.email_templates import generic_html, resolve_logo_url

logger = logging.getLogger(__name__)

REMINDER_DAYS = (7, 3, 1)
_DEDUP_TTL = 72 * 3600


def _reminder_html(brand: str, color: str, logo: str | None, plan: str,
                   paid_until: str, days: int) -> str:
    jour = "jour" if days == 1 else "jours"
    body = f"""
<p style="margin:0 0 12px">Votre abonnement <b>{_e(plan.upper())}</b> arrive à
expiration dans <b>{days} {jour}</b> (le <b>{_e(paid_until)}</b>).</p>
<p style="margin:0 0 12px">Pour conserver vos quotas et votre branding,
renouvelez-le depuis votre dashboard — la période se prolongera de 30 jours
à partir de la date de fin actuelle.</p>
<p style="margin:20px 0 0"><a href="/dashboard" style="background:{_e(color)};
  color:#fff;text-decoration:none;padding:12px 28px;border-radius:10px;
  font-weight:600;display:inline-block">Gérer mon abonnement</a></p>"""
    return generic_html(
        f"Votre plan {plan.upper()} expire dans {days} {jour}",
        body, brand=brand, color=color, logo_url=logo,
        footer="Email automatique — vous avez un abonnement actif.",
    )


def _expired_html(brand: str, color: str, logo: str | None, plan: str) -> str:
    body = f"""
<p style="margin:0 0 12px">Votre abonnement <b>{_e(plan.upper())}</b> a expiré —
votre compte a été rétrogradé au plan gratuit.</p>
<p style="margin:0">Vos clés API restent actives. Pour retrouver vos quotas et
votre branding, renouvelez depuis votre dashboard.</p>"""
    return generic_html(
        "Votre abonnement a expiré",
        body, brand=brand, color=color, logo_url=logo,
        footer="Email automatique — votre abonnement vient d'expirer.",
    )


async def _send_to_tenant_users(redis, email_provider, tenant_id: str,
                                subject: str, text: str, html: str) -> int:
    """Envoie aux comptes dashboard du tenant (expéditeur plateforme)."""
    from src.web.users import WebUserStore

    sent = 0
    for u in await WebUserStore(redis).list_by_tenant(tenant_id):
        try:
            await email_provider.send_email(u.email, subject, text, html)
            sent += 1
        except Exception:
            logger.warning("billing_reminder_send_failed",
                           extra={"tenant_id": tenant_id, "email": u.email})
    return sent


async def run_billing_reminders(redis, email_provider, public_base_url: str = "") -> int:
    """Balayage des abonnements proches de l'expiration. Retourne le nb envoyé."""
    store = TenantStore(redis)
    brand = await store.get("default")
    brand_name = brand.brand_name if brand else "MailHub"
    brand_color = brand.brand_color if brand else "#5060a8"
    logo = resolve_logo_url(brand.email_logo_url if brand else None, public_base_url)
    today = date.today()
    sent = 0

    for t in await store.list():
        if t.unlimited or t.plan == "free" or not t.paid_until or not t.is_active:
            continue
        try:
            delta = (date.fromisoformat(t.paid_until) - today).days
        except ValueError:
            continue
        if t.billing_status != "active" or delta not in REMINDER_DAYS:
            continue
        dedup = f"billing:remind:{t.tenant_id}:{t.paid_until}:{delta}"
        if not await redis.set(dedup, "1", nx=True, ex=_DEDUP_TTL):
            continue
        html = _reminder_html(brand_name, brand_color, logo, t.plan, t.paid_until, delta)
        text = (
            f"Votre abonnement {t.plan.upper()} expire dans {delta} jour(s) "
            f"(le {t.paid_until}). Renouvelez-le depuis votre dashboard."
        )
        n = await _send_to_tenant_users(
            redis, email_provider, t.tenant_id,
            f"[{brand_name}] Votre plan {t.plan.upper()} expire dans {delta} jour(s)",
            text, html,
        )
        sent += n
        logger.info("billing_reminder_sent",
                    extra={"tenant_id": t.tenant_id, "days": delta, "to": n})
    return sent


async def notify_plan_expired(
    redis, email_provider, tenant_id: str, plan: str, public_base_url: str = ""
) -> None:
    """Email 'abonnement expiré' — appelé par la rétrogradation automatique."""
    store = TenantStore(redis)
    brand = await store.get("default")
    html = _expired_html(
        brand.brand_name if brand else "MailHub",
        brand.brand_color if brand else "#5060a8",
        resolve_logo_url(brand.email_logo_url if brand else None, public_base_url),
        plan,
    )
    await _send_to_tenant_users(
        redis, email_provider, tenant_id,
        f"[{brand.brand_name if brand else 'MailHub'}] Votre abonnement {plan.upper()} a expiré",
        f"Votre abonnement {plan.upper()} a expiré — compte rétrogradé au plan gratuit.",
        html,
    )
