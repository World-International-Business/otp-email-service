"""Quotas mensuels de plan — compteur, enforcement 429 et alerte à 80 %.

Chaque plan définit ``quota_otp_month`` / ``quota_email_month`` (appliqués
au tenant lors du changement de plan, ajustables par l'admin). 0 = illimité,
``tenant.unlimited`` court-circuite tout (comptes partenaires).

À 80 % du quota, un événement ``quota_warning`` + webhook ``quota.warning``
sont émis UNE fois par mois et par métrique — le tenant voit l'alerte dans
son dashboard et peut upgrader avant la coupure (429 « plan quota »).
"""

import time
from datetime import datetime

from src.core.tenants import Tenant
from src.services.events import log_event, monthly_stat
from src.services.webhooks import enqueue_webhook

# métrique -> champ du tenant portant le quota mensuel
QUOTA_FIELDS = {
    "otps_sent": "quota_otp_month",
    "emails_sent": "quota_email_month",
}

WARN_THRESHOLD = 0.8


def seconds_until_next_month() -> int:
    """Secondes jusqu'au 1er du mois prochain (reset du compteur).

    Heure locale comme monthly_stat/incr_daily_stat (date.today()) —
    le Retry-After doit coïncider avec le rollover du compteur."""
    now = datetime.now()
    nxt = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    nxt = nxt.replace(year=nxt.year + 1, month=1) if nxt.month == 12 \
        else nxt.replace(month=nxt.month + 1)
    return max(60, int((nxt - now).total_seconds()))


async def monthly_usage(redis, tenant: Tenant) -> dict[str, dict]:
    """Usage du mois courant par métrique : {metric: {used, quota}}."""
    out = {}
    for metric, field in QUOTA_FIELDS.items():
        out[metric] = {
            "used": await monthly_stat(redis, tenant, metric),
            "quota": getattr(tenant, field),
        }
    return out


async def check_quota(
    redis, tenant: Tenant, metric: str
) -> tuple[bool, int, int]:
    """(autorisé, utilisé, quota). quota=0 ou tenant.unlimited -> toujours True.

    À l'approche du plafond, émet l'alerte 80 % (une fois par mois) — le
    coût est un GET redis, mutualisé avec le compteur mensuel déjà lu."""
    if tenant.unlimited:
        return True, 0, 0
    quota = getattr(tenant, QUOTA_FIELDS[metric])
    if not quota:
        return True, 0, 0
    used = await monthly_stat(redis, tenant, metric)
    if used >= quota:
        return False, used, quota
    await _warn_if_close(redis, tenant, metric, used, quota)
    return True, used, quota


async def _warn_if_close(
    redis, tenant: Tenant, metric: str, used: int, quota: int
) -> None:
    """Alerte unique par mois+métrique quand used >= 80 % du quota."""
    if used < quota * WARN_THRESHOLD:
        return
    month = time.strftime("%Y-%m", time.gmtime())
    marker = f"{tenant.key_prefix()}quota_warn:{metric}:{month}"
    if not await redis.set(marker, "1", nx=True, ex=62 * 86400):
        return
    await log_event(
        redis, tenant, "quota_warning",
        metric=metric, used=used, quota=quota,
    )
    await enqueue_webhook(
        redis, tenant, "quota.warning",
        {"metric": metric, "used": used, "quota": quota,
         "percent": round(100 * used / quota)},
    )
