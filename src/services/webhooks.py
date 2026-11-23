"""Webhooks sortants — notification des tenants (email.*, otp.*).

Quand un tenant configure `webhook_url`, MailHub lui POST un événement JSON
signé par son `hmac_secret` — le même secret que le client utilise déjà pour
signer ses appels entrants (vérifiable côté client sans config en plus).

Headers envoyés :
  X-MailHub-Event      nom de l'événement (email.sent, email.failed…)
  X-MailHub-Delivery   id de livraison (idempotent côté client)
  X-MailHub-Signature  sha256=<HMAC-SHA256 du body brut, clé = hmac_secret>

Clés :
  wh:queue              ZSET delivery_id -> timestamp prochaine tentative
  wh:job:{delivery_id}  JSON {tenant_id, url, secret, event, body, attempts}

3 tentatives (immédiat, +60 s, +5 min) puis abandon — loggué en event tenant.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time

import httpx

logger = logging.getLogger(__name__)

QUEUE_KEY = "wh:queue"
JOB_TTL = 24 * 3600
MAX_ATTEMPTS = 3
RETRY_DELAYS = (60, 300)
_TIMEOUT = httpx.Timeout(10.0)


def _job_key(delivery_id: str) -> str:
    return f"wh:job:{delivery_id}"


async def enqueue_webhook(redis, tenant, event: str, data: dict) -> str | None:
    """Programme une livraison webhook. None si le tenant n'en a pas configuré."""
    url = getattr(tenant, "webhook_url", None)
    if not url:
        return None
    delivery_id = f"wh_{secrets.token_urlsafe(12)}"
    job = {
        "delivery_id": delivery_id,
        "tenant_id": tenant.tenant_id,
        "url": url,
        "secret": tenant.hmac_secret,
        "event": event,
        "body": {"id": delivery_id, "event": event, "created": int(time.time()), "data": data},
        "attempts": 0,
    }
    await redis.set(_job_key(delivery_id), json.dumps(job), ex=JOB_TTL)
    await redis.zadd(QUEUE_KEY, {delivery_id: time.time()})
    return delivery_id


async def process_webhook_queue_once(redis) -> int:
    """Livre les webhooks dus. Retourne le nombre traité."""
    now = time.time()
    due = await redis.zrangebyscore(QUEUE_KEY, "-inf", now, start=0, num=10)
    processed = 0
    for delivery_id in due:
        raw = await redis.get(_job_key(delivery_id))
        if not raw:
            await redis.zrem(QUEUE_KEY, delivery_id)
            continue
        job = json.loads(raw)
        job["attempts"] += 1

        payload = json.dumps(job["body"], ensure_ascii=False).encode()
        sig = hmac.new(job["secret"].encode(), payload, hashlib.sha256).hexdigest()
        ok = False
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                r = await c.post(
                    job["url"],
                    content=payload,
                    headers={
                        "Content-Type": "application/json",
                        "X-MailHub-Event": job["event"],
                        "X-MailHub-Delivery": delivery_id,
                        "X-MailHub-Signature": f"sha256={sig}",
                    },
                )
            ok = 200 <= r.status_code < 300
        except httpx.HTTPError as exc:
            logger.warning(
                "webhook_delivery_error",
                extra={"delivery_id": delivery_id, "event": job["event"],
                       "err": str(exc)[:150]},
            )

        if ok or job["attempts"] >= MAX_ATTEMPTS:
            if not ok:
                logger.error(
                    "webhook_dead",
                    extra={"delivery_id": delivery_id, "tenant_id": job["tenant_id"],
                           "event": job["event"], "url": job["url"]},
                )
            await redis.delete(_job_key(delivery_id))
            await redis.zrem(QUEUE_KEY, delivery_id)
        else:
            delay = RETRY_DELAYS[min(job["attempts"] - 1, len(RETRY_DELAYS) - 1)]
            await redis.set(_job_key(delivery_id), json.dumps(job), ex=JOB_TTL)
            await redis.zadd(QUEUE_KEY, {delivery_id: now + delay})
        processed += 1
    return processed
