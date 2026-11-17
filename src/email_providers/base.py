"""Interface abstraite pour les fournisseurs d'email."""

from abc import ABC, abstractmethod


class SenderIdentity(dict):
    """Identité expéditeur par tenant.

    Clés optionnelles : from_email, from_name, smtp_host, smtp_port,
    smtp_user, smtp_password, smtp_use_ssl (None = auto : SSL si port 465).
    Valeurs manquantes → fallback sur la config globale du service.
    """


class EmailSendError(Exception):
    """Échec d'envoi — `str(exc)` contient la raison (jamais de secrets)."""


class EmailProvider(ABC):
    @abstractmethod
    async def send_email(
        self,
        to_email: str,
        subject: str,
        text_body: str,
        html_body: str,
        sender: SenderIdentity | None = None,
    ) -> bool:
        """Envoie un email. Retourne True si envoyé avec succès."""
        ...
