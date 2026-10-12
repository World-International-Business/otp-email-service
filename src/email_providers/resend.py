"""Provider Resend — prêt à être implémenté."""

from src.config import Settings
from src.email_providers.base import EmailProvider, SenderIdentity


class ResendProvider(EmailProvider):
    """Stub pour futur envoi via l'API Resend.

    Pour l'activer : implémenter l'appel POST https://api.resend.com/emails
    avec httpx et la clé RESEND_API_KEY.
    """

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
        raise NotImplementedError("Resend provider is not implemented yet")
