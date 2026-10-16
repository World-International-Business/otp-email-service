"""Worker d'arrière-plan — consomme la file de notifications Redis.

Démarré dans le lifespan FastAPI, il dépile les jobs (BRPOP avec fallback
polling pour fakeredis), rend les templates, appelle le provider email,
met à jour le cycle de vie du message et déclenche les webhooks sortants.
"""

import asyncio
import json
import logging

from redis.asyncio import Redis

from src.config import Settings
from src.core.models import MessageStatus, NotificationChannel
from src.core.templates import TemplateStore
from src.core.tenants import TenantStore
from src.email_providers.base import EmailProvider, resolve_sender
from src.services.notification_service import NotificationService
from src.services.webhook_service import (
    EVENT_FAILED,
    EVENT_SENT,
    WebhookService,
)

logger = logging.getLogger(__name__)


class NotificationWorker:
    def __init__(
        self,
        redis: Redis,
        settings: Settings,
        email_provider: EmailProvider,
        notifications: NotificationService,
        webhooks: WebhookService,
    ):
        self.redis = redis
        self.settings = settings
        self.email_provider = email_provider
        self.notifications = notifications
        self.webhooks = webhooks
        self._stop = asyncio.Event()
        self._brpop_supported = True

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        logger.info("notification_worker_started")
        while not self._stop.is_set():
            try:
                job = await self._dequeue()
            except Exception:
                logger.exception("notification_dequeue_error")
                await asyncio.sleep(1)
                continue
            if job is None:
                continue
            try:
                await self._process(job)
            except Exception:
                logger.exception(
                    "notification_job_failed",
                    extra={"message_id": job.get("message_id")},
                )

    async def _dequeue(self) -> dict | None:
        if self._brpop_supported:
            try:
                item = await self.redis.brpop(
                    self.notifications.queue_key,
                    timeout=self.settings.worker_poll_timeout_seconds,
                )
                return json.loads(item[1]) if item else None
            except Exception:
                # ex. fakeredis sans support des commandes bloquantes
                self._brpop_supported = False

        raw = await self.redis.rpop(self.notifications.queue_key)
        if raw is None:
            await asyncio.sleep(self.settings.worker_idle_sleep_seconds)
            return None
        return json.loads(raw)

    async def _process(self, job: dict) -> None:
        tenant_id = job["tenant_id"]
        message_id = job["message_id"]

        record = await self.notifications.increment_attempts(tenant_id, message_id)
        if record is None:
            logger.warning("notification_record_missing", extra={"message_id": message_id})
            return

        tenant = await TenantStore(self.redis).get(tenant_id)
        if tenant is None or not tenant.is_active:
            await self.notifications.update_status(
                tenant_id, message_id, MessageStatus.failed, error="tenant_inactive"
            )
            return

        await self.notifications.update_status(tenant_id, message_id, MessageStatus.processing)

        try:
            subject, text, html = await self._resolve_content(job, tenant)
        except _TemplateNotFound as exc:
            await self._fail(tenant, message_id, str(exc))
            return

        if job.get("channel", NotificationChannel.email.value) != NotificationChannel.email.value:
            await self._fail(
                tenant, message_id, f"channel '{job.get('channel')}' not supported"
            )
            return

        try:
            success = await self.email_provider.send_email(
                to_email=job["to"],
                subject=subject,
                text_body=text or "",
                html_body=html or "",
                sender=resolve_sender(tenant, self.settings),
            )
        except Exception as exc:
            logger.exception(
                "provider_send_exception",
                extra={"tenant_id": tenant_id, "message_id": message_id},
            )
            success = False
            error_detail = str(exc)
        else:
            error_detail = "provider rejected the message" if not success else None

        if success:
            await self._succeed(tenant, message_id, record)
        else:
            await self._retry_or_fail(tenant, job, record, error_detail)

    async def _resolve_content(self, job: dict, tenant) -> tuple[str, str | None, str | None]:
        """Contenu final : template tenant, ou contenu inline rendu avec data."""
        template_id = job.get("template_id")
        data = {"brand_name": tenant.brand_name, **(job.get("data") or {})}

        if template_id:
            rendered = await TemplateStore(self.redis, tenant).render(
                template_id, job.get("language", "en"), data
            )
            if rendered is None:
                raise _TemplateNotFound(f"template '{template_id}' not found")
            return rendered

        from src.core.templates import render_string

        subject = render_string(job.get("subject"), data) or ""
        text = render_string(job.get("text"), data)
        html = render_string(job.get("html"), data)
        return subject, text, html

    async def _succeed(self, tenant, message_id: str, record: dict) -> None:
        updated = await self.notifications.update_status(
            tenant.tenant_id, message_id, MessageStatus.sent
        )
        await self.redis.incr(f"{tenant.key_prefix()}stats:emails_sent")
        logger.info(
            "notification_sent",
            extra={"tenant_id": tenant.tenant_id, "message_id": message_id},
        )
        await self.webhooks.dispatch(tenant, EVENT_SENT, self._event_payload(updated or record))

    async def _retry_or_fail(self, tenant, job: dict, record: dict, error: str | None) -> None:
        attempts = record["attempts"]
        if attempts < self.settings.worker_max_attempts:
            await self.notifications.update_status(
                tenant.tenant_id,
                job["message_id"],
                MessageStatus.queued,
                error=error,
                detail=f"retry {attempts}/{self.settings.worker_max_attempts}",
            )
            await self.notifications.requeue(job)
            return
        await self._fail(tenant, job["message_id"], error or "send failed")

    async def _fail(self, tenant, message_id: str, error: str) -> None:
        updated = await self.notifications.update_status(
            tenant.tenant_id, message_id, MessageStatus.failed, error=error
        )
        await self.redis.incr(f"{tenant.key_prefix()}stats:emails_failed")
        logger.error(
            "notification_failed",
            extra={
                "tenant_id": tenant.tenant_id,
                "message_id": message_id,
                "error": error,
            },
        )
        await self.webhooks.dispatch(
            tenant, EVENT_FAILED, self._event_payload(updated or {"message_id": message_id})
        )

    @staticmethod
    def _event_payload(record: dict) -> dict:
        """Charge utile du webhook : métadonnées du message (jamais le corps)."""
        return {
            "message_id": record.get("message_id"),
            "channel": record.get("channel"),
            "to": record.get("to"),
            "subject": record.get("subject"),
            "status": record.get("status"),
            "attempts": record.get("attempts"),
            "error": record.get("error"),
            "metadata": record.get("metadata") or {},
        }


class _TemplateNotFound(Exception):
    pass
