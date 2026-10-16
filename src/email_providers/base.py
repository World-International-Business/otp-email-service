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


def resolve_sender(tenant, settings) -> SenderIdentity:
    """Identité expéditeur : config du tenant, sinon fallback global."""
    return SenderIdentity(
        from_email=tenant.from_email or settings.from_email,
        from_name=tenant.from_name or settings.from_name,
        smtp_host=tenant.smtp_host or settings.smtp_host,
        smtp_port=tenant.smtp_port or settings.smtp_port,
        smtp_user=tenant.smtp_user or settings.smtp_user,
        smtp_password=tenant.smtp_password or settings.smtp_password,
    )
