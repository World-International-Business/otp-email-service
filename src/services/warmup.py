"""Montée en charge (warm-up) des expéditeurs SMTP neufs.

Un domaine/IP qui envoie un gros volume dès le premier jour est traité
comme du spam. Quand un tenant configure un SMTP personnalisé, son volume
journalier est plafonné puis croît exponentiellement :

    cap(jour n) = WARMUP_START_VOLUME * WARMUP_GROWTH ** n

jusqu'à WARMUP_DAYS, puis plus aucune limite (le quota du plan reprend).
Le compteur jour = OTP + emails transactionnels déjà envoyés (clés
stats_daily existantes). Un envoi bloqué retourne 429 + Retry-After
jusqu'à minuit UTC. Les tenants déjà en production sans
``smtp_configured_at`` ne sont pas affectés (pas de rétro-warm-up).
"""

import time
from datetime import date, datetime, timedelta, timezone

from src.config import Settings
from src.core.tenants import Tenant


def warmup_cap(settings: Settings, tenant: Tenant) -> int | None:
    """Cap journalier actuel — None hors période de warm-up."""
    if not (
        settings.warmup_enabled
        and tenant.smtp_host
        and tenant.smtp_configured_at
    ):
        return None
    days = int((time.time() - tenant.smtp_configured_at) / 86400)
    if days >= settings.warmup_days:
        return None
    return max(
        1,
        int(settings.warmup_start_volume * settings.warmup_growth ** days),
    )


async def warmup_state(
    redis, settings: Settings, tenant: Tenant
) -> tuple[int | None, int | None, int]:
    """(restant, cap, envoyés_aujourd'hui). restant/cap = None hors warm-up."""
    cap = warmup_cap(settings, tenant)
    if cap is None:
        return None, None, 0
    today = date.today().isoformat()
    pipe = redis.pipeline()
    pipe.get(f"{tenant.key_prefix()}stats_daily:emails_sent:{today}")
    pipe.get(f"{tenant.key_prefix()}stats_daily:otps_sent:{today}")
    n_emails, n_otps = await pipe.execute()
    sent = int(n_emails or 0) + int(n_otps or 0)
    return max(0, cap - sent), cap, sent


def seconds_until_midnight() -> int:
    now = datetime.now(timezone.utc)
    nxt = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return max(60, int((nxt - now).total_seconds()))
