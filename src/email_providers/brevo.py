"""Provider Brevo (Sendinblue) — prêt à être implémenté."""

from src.config import Settings
from src.email_providers.base import EmailProvider, SenderIdentity


class BrevoProvider(EmailProvider):
    """Stub pour futur envoi via l'API Brevo (REST).

    Pour l'activer : implémenter l'appel à https://api.brevo.com/v3/smtp/email
    avec httpx et la clé BREVO_API_KEY.
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
        raise NotImplementedError("Brevo provider is not implemented yet")
