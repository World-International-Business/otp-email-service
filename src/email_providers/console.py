"""Provider console — affiche l'email dans les logs pour les tests/démos."""

import logging

from src.config import Settings
from src.email_providers.base import EmailProvider, SenderIdentity

logger = logging.getLogger(__name__)


class ConsoleEmailProvider(EmailProvider):
    def __init__(self, settings: Settings):
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
        from_email = sender.get("from_email") or self.settings.from_email
        from_name = sender.get("from_name") or self.settings.from_name

        logger.info(
            "email_sent_console",
            extra={
                "to": to_email,
                "subject": subject,
                "from": f"{from_name} <{from_email}>",
                "attachments": [a["filename"] for a in attachments or []],
            },
        )
        # INFO et non DEBUG : le provider console sert justement à lire
        # l'email (code OTP) dans les logs en mode local/démo.
        logger.info("email_text_body", extra={"body": text_body})
        return True
