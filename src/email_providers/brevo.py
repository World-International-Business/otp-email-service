"""Provider Brevo (Sendinblue) — API REST v3."""

import logging

import httpx

from src.config import Settings
from src.email_providers.base import EmailProvider, EmailSendError, SenderIdentity

logger = logging.getLogger(__name__)

_API_URL = "https://api.brevo.com/v3/smtp/email"
_TIMEOUT = 10.0


class BrevoProvider(EmailProvider):
    """Envoi via https://api.brevo.com/v3/smtp/email (BREVO_API_KEY requis)."""

    def __init__(self, settings: Settings):
        if not settings.brevo_api_key:
            raise ValueError("BREVO_API_KEY requis avec EMAIL_PROVIDER=brevo")
        self.settings = settings

    async def send_email(
        self,
        to_email: str,
        subject: str,
        text_body: str,
        html_body: str,
        sender: SenderIdentity | None = None,
        *,
        attachments: list[dict] | None = None,
    ) -> bool:
        sender = sender or {}
        payload = {
            "sender": {
                "email": str(sender.get("from_email") or self.settings.from_email),
                "name": sender.get("from_name") or self.settings.from_name,
            },
            "to": [{"email": to_email}],
            "subject": subject,
            "textContent": text_body,
            "htmlContent": html_body,
        }
        if attachments:
            payload["attachment"] = [
                {"name": a["filename"], "content": a["content_base64"]}
                for a in attachments
            ]
        unsub = sender.get("unsubscribe_url") or self.settings.list_unsubscribe_url
        payload["headers"] = {"Auto-Submitted": "auto-generated"}
        if unsub:
            payload["headers"]["List-Unsubscribe"] = f"<{unsub}>"
            payload["headers"]["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(
                    _API_URL,
                    json=payload,
                    headers={
                        "api-key": self.settings.brevo_api_key,
                        "accept": "application/json",
                    },
                )
        except httpx.HTTPError as exc:
            logger.error("brevo_send_error", extra={"error": str(exc)})
            raise EmailSendError(str(exc)) from exc
        if not resp.is_success:
            detail = resp.text[:200]
            logger.error(
                "brevo_send_rejected",
                extra={"status": resp.status_code},
            )
            raise EmailSendError(f"Brevo {resp.status_code}: {detail}")
        return True
