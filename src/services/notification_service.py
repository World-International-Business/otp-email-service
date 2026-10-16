"""Service de notifications agnostique — admission + suivi du cycle de vie.

Façon provider : la requête HTTP ne fait que valider, persister l'état
`queued` et pousser le job dans la file Redis. L'envoi réel est délégué
au NotificationWorker (asynchrone), ce qui garantit une réponse < 50 ms
et isole le client des lenteurs/erreurs du provider SMTP.

Clés Redis :
  t:{tenant_id}:msg:{message_id}   -> JSON du message (statut, historique)
  {queue_key}                      -> liste FIFO des jobs à traiter
"""

import json
import logging
import secrets

from redis.asyncio import Redis

from src.config import Settings
from src.core.models import MessageStatus, NotificationChannel
from src.core.security import now_utc
from src.core.tenants import Tenant

logger = logging.getLogger(__name__)


def generate_message_id() -> str:
    return f"msg_{secrets.token_urlsafe(16)}"


def message_key(tenant_id: str, message_id: str) -> str:
    return f"t:{tenant_id}:msg:{message_id}"


class NotificationService:
    """Admission des notifications et persistance de leur cycle de vie."""

    def __init__(self, redis: Redis, settings: Settings):
        self.redis = redis
        self.settings = settings
        self.queue_key = settings.notification_queue

    async def submit(
        self,
        tenant: Tenant,
        *,
        to: str,
        channel: NotificationChannel = NotificationChannel.email,
        template_id: str | None = None,
        subject: str | None = None,
        text: str | None = None,
        html: str | None = None,
        data: dict | None = None,
        language: str = "en",
        metadata: dict | None = None,
    ) -> str:
        """Persiste le message en `queued` et l'enfile. Retourne message_id."""
        message_id = generate_message_id()
        now = now_utc().isoformat()

        record = {
            "message_id": message_id,
            "tenant_id": tenant.tenant_id,
            "channel": channel.value,
            "to": to,
            "subject": subject,
            "template_id": template_id,
            "language": language,
            "metadata": metadata or {},
            "status": MessageStatus.queued.value,
            "attempts": 0,
            "error": None,
            "history": [{"status": MessageStatus.queued.value, "at": now}],
            "created_at": now,
            "updated_at": now,
        }

        # Le corps (potentiellement sensible, ex. code OTP) ne transite que par
        # la file — jamais persisté dans l'enregistrement de statut.
        job = {
            "message_id": message_id,
            "tenant_id": tenant.tenant_id,
            "channel": channel.value,
            "to": to,
            "subject": subject,
            "text": text,
            "html": html,
            "template_id": template_id,
            "data": data or {},
            "language": language,
            "metadata": metadata or {},
        }

        pipe = self.redis.pipeline()
        pipe.setex(
            message_key(tenant.tenant_id, message_id),
            self.settings.message_ttl_seconds,
            json.dumps(record),
        )
        pipe.lpush(self.queue_key, json.dumps(job))
        await pipe.execute()

        logger.info(
            "notification_queued",
            extra={
                "tenant_id": tenant.tenant_id,
                "message_id": message_id,
                "channel": channel.value,
            },
        )
        return message_id

    async def get_record(self, tenant_id: str, message_id: str) -> dict | None:
        raw = await self.redis.get(message_key(tenant_id, message_id))
        return json.loads(raw) if raw else None

    async def update_status(
        self,
        tenant_id: str,
        message_id: str,
        status: MessageStatus,
        *,
        error: str | None = None,
        detail: str | None = None,
    ) -> dict | None:
        """Met à jour le statut d'un message et ajoute une entrée d'historique."""
        record = await self.get_record(tenant_id, message_id)
        if record is None:
            return None

        now = now_utc().isoformat()
        record["status"] = status.value
        record["updated_at"] = now
        if error is not None:
            record["error"] = error
        event = {"status": status.value, "at": now}
        if detail:
            event["detail"] = detail
        record["history"].append(event)

        await self.redis.setex(
            message_key(tenant_id, message_id),
            self.settings.message_ttl_seconds,
            json.dumps(record),
        )
        return record

    async def increment_attempts(self, tenant_id: str, message_id: str) -> dict | None:
        record = await self.get_record(tenant_id, message_id)
        if record is None:
            return None
        record["attempts"] += 1
        record["updated_at"] = now_utc().isoformat()
        await self.redis.setex(
            message_key(tenant_id, message_id),
            self.settings.message_ttl_seconds,
            json.dumps(record),
        )
        return record

    async def requeue(self, job: dict) -> None:
        """Remet un job en file pour un nouvel essai."""
        await self.redis.rpush(self.queue_key, json.dumps(job))
