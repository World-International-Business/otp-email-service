"""Provider Resend — API REST."""

import logging

import httpx

from src.config import Settings
from src.email_providers.base import EmailProvider, EmailSendError, SenderIdentity

logger = logging.getLogger(__name__)

_API_URL = "https://api.resend.com/emails"
_TIMEOUT = 10.0


class ResendProvider(EmailProvider):
    """Envoi via https://api.resend.com/emails (RESEND_API_KEY requis)."""

    def __init__(self, settings: Settings):
        if not settings.resend_api_key:
            raise ValueError("RESEND_API_KEY requis avec EMAIL_PROVIDER=resend")
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
        from_email = str(sender.get("from_email") or self.settings.from_email)
        from_name = sender.get("from_name") or self.settings.from_name
        payload = {
            "from": f"{from_name} <{from_email}>",
            "to": [to_email],
            "subject": subject,
            "text": text_body,
            "html": html_body,
        }
        if attachments:
            payload["attachments"] = [
                {"filename": a["filename"], "content": a["content_base64"]}
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
                        "Authorization": f"Bearer {self.settings.resend_api_key}",
                    },
                )
        except httpx.HTTPError as exc:
            logger.error("resend_send_error", extra={"error": str(exc)})
            raise EmailSendError(str(exc)) from exc
        if not resp.is_success:
            detail = resp.text[:200]
            logger.error(
                "resend_send_rejected",
                extra={"status": resp.status_code},
            )
            raise EmailSendError(f"Resend {resp.status_code}: {detail}")
        return True
