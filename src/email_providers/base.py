"""Interface abstraite pour les fournisseurs d'email."""

from abc import ABC, abstractmethod


class SenderIdentity(dict):
    """Identité expéditeur par tenant.

    Clés optionnelles : from_email, from_name, smtp_user, smtp_password.
    Valeurs manquantes → fallback sur la config globale du service.
    """


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
