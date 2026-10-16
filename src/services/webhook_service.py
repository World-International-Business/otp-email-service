"""Webhooks sortants — le provider notifie le client des changements de statut.

Si le tenant a configuré `webhook_url`, chaque transition de statut déclenche
un POST JSON vers le client, signé avec son `hmac_secret` :

    X-Webhook-Event:     notification.sent
    X-Webhook-Signature: sha256=<hmac_hex(body)>
    X-Webhook-Id:        evt_...

Le client vérifie la signature avec le secret qui lui a été communiqué.
Les échecs de livraison du webhook sont loggés, jamais bloquants.
"""

import hashlib
import hmac
import json
import logging
import secrets

import httpx

from src.config import Settings
from src.core.security import now_utc
from src.core.tenants import Tenant

logger = logging.getLogger(__name__)

EVENT_QUEUED = "notification.queued"
EVENT_SENT = "notification.sent"
EVENT_DELIVERED = "notification.delivered"
EVENT_BOUNCED = "notification.bounced"
EVENT_FAILED = "notification.failed"


class WebhookService:
    def __init__(self, settings: Settings):
        self.timeout = settings.webhook_timeout_seconds

    async def dispatch(self, tenant: Tenant, event: str, data: dict) -> bool:
        """Envoie l'événement au webhook du tenant. Retourne True si livré."""
        if not tenant.webhook_url:
            return False

        event_id = f"evt_{secrets.token_urlsafe(16)}"
        body = json.dumps(
            {
                "id": event_id,
                "type": event,
                "created_at": now_utc().isoformat(),
                "tenant_id": tenant.tenant_id,
                "data": data,
            }
        )
        signature = hmac.new(
            tenant.hmac_secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    tenant.webhook_url,
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Webhook-Id": event_id,
                        "X-Webhook-Event": event,
                        "X-Webhook-Signature": f"sha256={signature}",
                    },
                )
            if response.is_success:
                logger.info(
                    "webhook_delivered",
                    extra={
                        "tenant_id": tenant.tenant_id,
                        "event": event,
                        "event_id": event_id,
                        "status_code": response.status_code,
                    },
                )
                return True
            logger.warning(
                "webhook_rejected",
                extra={
                    "tenant_id": tenant.tenant_id,
                    "event": event,
                    "event_id": event_id,
                    "status_code": response.status_code,
                },
            )
        except Exception as exc:
            logger.warning(
                "webhook_delivery_failed",
                extra={
                    "tenant_id": tenant.tenant_id,
                    "event": event,
                    "event_id": event_id,
                    "error": str(exc),
                },
            )
        return False
