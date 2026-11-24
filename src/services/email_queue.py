"""File d'attente d'emails + statuts + retries — Redis ZSET.

L'envoi SMTP est découplé de la requête HTTP : l'API rend 200 immédiatement
(status "queued"), un worker livre en arrière-plan avec retries exponentiels.

Clés :
  mail:queue                  ZSET job_id -> timestamp de prochaine tentative
  mail:job:{job_id}           JSON {tenant_id, message_id, to, type, subject,
                              text_body, html_body, attempts, client_ip}
  mail:status:{message_id}    JSON {tenant_id, status, to, type, attempts,
                              error, updated} — TTL 30 j

Cycle de vie : queued -> retry -> sent | dead (après email_max_attempts).
Les OTP restent synchrones (latence critique) mais enregistrent aussi leur
statut sous le même keyspace — consultable via GET /v1/emails/status/{id}.
"""

from __future__ import annotations

import json
import logging
import secrets
import time

from src.core.tenants import TenantStore
from src.email_providers.base import EmailSendError
from src.services.events import log_event
from src.services.webhooks import enqueue_webhook

logger = logging.getLogger(__name__)

QUEUE_KEY = "mail:queue"
STATUS_TTL = 30 * 24 * 3600
JOB_TTL = 7 * 24 * 3600
RETRY_DELAYS = (60, 300, 900)  # backoff après les essais 1, 2, 3


def _job_key(job_id: str) -> str:
    return f"mail:job:{job_id}"


def _status_key(message_id: str) -> str:
    return f"mail:status:{message_id}"


async def set_status(
    redis,
    message_id: str,
    tenant_id: str,
    status: str,
    *,
    to: str = "",
    type_: str = "",
    error: str = "",
    attempts: int | None = None,
) -> None:
    """Met à jour le statut d'un envoi (merge avec l'existant)."""
    data = await get_status(redis, message_id) or {}
    data.update({"tenant_id": tenant_id, "status": status, "updated": int(time.time())})
    if to:
        data["to"] = to
    if type_:
        data["type"] = type_
    if error:
        data["error"] = error[:200]
    elif status != "dead":
        data.pop("error", None)
    if attempts is not None:
        data["attempts"] = attempts
    await redis.set(_status_key(message_id), json.dumps(data), ex=STATUS_TTL)


async def get_status(redis, message_id: str) -> dict | None:
    raw = await redis.get(_status_key(message_id))
    return json.loads(raw) if raw else None


async def enqueue(
    redis,
    tenant_id: str,
    to_email: str,
    email_type: str,
    subject: str,
    text_body: str,
    html_body: str,
    client_ip: str = "unknown",
) -> str:
    """Ajoute un email à la file. Retourne le message_id consultable."""
    message_id = f"msg_{secrets.token_urlsafe(16)}"
    job_id = f"job_{secrets.token_urlsafe(16)}"
    job = {
        "job_id": job_id,
        "tenant_id": tenant_id,
        "message_id": message_id,
        "to": to_email,
        "type": email_type,
        "subject": subject,
        "text_body": text_body,
        "html_body": html_body,
        "attempts": 0,
        "client_ip": client_ip,
        "enqueued": int(time.time()),
    }
    await redis.set(_job_key(job_id), json.dumps(job), ex=JOB_TTL)
    await set_status(
        redis, message_id, tenant_id, "queued", to=to_email, type_=email_type, attempts=0
    )
    await redis.zadd(QUEUE_KEY, {job_id: time.time()})
    return message_id


async def process_email_queue_once(redis, email_service, max_attempts: int = 4) -> int:
    """Traite les jobs arrivés à échéance. Retourne le nombre traité.

    Découpé en « once » pour la testabilité — la boucle vit dans main.py.
    """
    now = time.time()
    due = await redis.zrangebyscore(QUEUE_KEY, "-inf", now, start=0, num=10)
    processed = 0
    for job_id in due:
        # Claim atomique : un seul worker traite un job donné, même si deux
        # répliques polluent la file en même temps (déploiement multi-instance).
        # Le verrou est TOUJOURS relâché en fin d'itération : sur le chemin
        # retry le job est reprogrammé à plus tard, sinon il resterait
        # bloqué jusqu'à l'expiration du verrou.
        if not await redis.set(f"mail:lock:{job_id}", "1", nx=True, ex=120):
            continue
        try:
            processed += await _process_one(redis, email_service, job_id,
                                          now, max_attempts)
        finally:
            await redis.delete(f"mail:lock:{job_id}")
    return processed


async def _process_one(redis, email_service, job_id: str, now: float,
                       max_attempts: int) -> int:
    raw = await redis.get(_job_key(job_id))
    if not raw:
        await redis.zrem(QUEUE_KEY, job_id)
        return 0
    job = json.loads(raw)
    job["attempts"] += 1
    tenant = await TenantStore(redis).get(job["tenant_id"])

    async def _finish(status: str, error: str = "") -> None:
        await set_status(
            redis, job["message_id"], job["tenant_id"], status,
            error=error, attempts=job["attempts"],
        )
        await redis.delete(_job_key(job_id))
        await redis.zrem(QUEUE_KEY, job_id)

    if not tenant or not tenant.is_active:
        await _finish("dead", "tenant_missing_or_inactive")
        return 1
    try:
        await email_service.deliver(
            tenant,
            job["to"],
            job["subject"],
            job["text_body"],
            job["html_body"],
            job["message_id"],
            job["type"],
            client_ip=job.get("client_ip", "queue"),
        )
    except EmailSendError as exc:
        reason = str(exc)[:200]
        if job["attempts"] >= max_attempts:
            await _finish("dead", reason)
            # Rendre le quota réservé au POST : l'email n'est jamais parti.
            from src.services.rate_limiter import RateLimiter

            await RateLimiter(redis, tenant).release_email(job["to"])
            await log_event(
                redis, tenant, "email_dead",
                message_id=job["message_id"], error=reason[:100],
            )
            await enqueue_webhook(
                redis, tenant, "email.dead",
                {"message_id": job["message_id"], "to": job["to"],
                 "type": job["type"], "error": reason,
                 "attempts": job["attempts"]},
            )
        else:
            delay = RETRY_DELAYS[min(job["attempts"] - 1, len(RETRY_DELAYS) - 1)]
            await redis.set(_job_key(job_id), json.dumps(job), ex=JOB_TTL)
            await redis.zadd(QUEUE_KEY, {job_id: now + delay})
            await set_status(
                redis, job["message_id"], job["tenant_id"], "retry",
                error=reason, attempts=job["attempts"],
            )
            await enqueue_webhook(
                redis, tenant, "email.failed",
                {"message_id": job["message_id"], "to": job["to"],
                 "type": job["type"], "error": reason,
                 "attempts": job["attempts"], "will_retry": True},
            )
    else:
        await _finish("sent")
    return 1
