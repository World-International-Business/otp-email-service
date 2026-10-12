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
            },
        )
        logger.debug("email_text_body", extra={"body": text_body})
        return True
