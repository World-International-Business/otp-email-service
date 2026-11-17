"""Implémentation SMTP du provider d'email — supporte le sender par tenant.

Fonctionne avec tout serveur SMTP (Gmail, Google Workspace, OVH, Zoho,
IONOS, cPanel, serveur auto-hébergé…) : STARTTLS par défaut, SSL implicite
sur port 465 ou via smtp_use_ssl.
"""

import asyncio
import logging
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from src.config import Settings
from src.email_providers.base import EmailProvider, EmailSendError, SenderIdentity

logger = logging.getLogger(__name__)


class GmailSMTPProvider(EmailProvider):
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
        smtp_host = sender.get("smtp_host") or self.settings.smtp_host
        smtp_port = int(sender.get("smtp_port") or self.settings.smtp_port)
        smtp_user = sender.get("smtp_user") or self.settings.smtp_user
        smtp_password = sender.get("smtp_password") or self.settings.smtp_password
        use_ssl = sender.get("smtp_use_ssl")
        if use_ssl is None:
            use_ssl = self.settings.smtp_use_ssl
        if use_ssl is None:
            use_ssl = smtp_port == 465  # convention : 465 = SSL implicite

        if not smtp_user or not smtp_password:
            raise EmailSendError(
                "Identifiants SMTP manquants (SMTP_USER / SMTP_PASSWORD) — "
                "configurer le tenant ou les variables globales"
            )

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{from_name} <{from_email}>"
        msg["To"] = to_email
        msg.attach(MIMEText(text_body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        try:
            # smtplib est synchrone : on l'exécute hors de l'event loop
            await asyncio.to_thread(
                self._send_smtp,
                smtp_host, smtp_port, use_ssl, smtp_user, smtp_password,
                from_email, to_email, msg.as_string(),
            )
            return True
        except Exception as exc:
            # Ne jamais exposer smtp_password — on le masque si l'erreur le contient.
            reason = str(exc)
            if smtp_password:
                reason = reason.replace(smtp_password, "***")
            logger.error(
                "smtp_send_error",
                extra={"smtp_host": smtp_host, "smtp_port": smtp_port,
                       "error": reason},
            )
            raise EmailSendError(reason) from exc

    @staticmethod
    def _send_smtp(
        smtp_host: str,
        smtp_port: int,
        use_ssl: bool,
        smtp_user: str | None,
        smtp_password: str | None,
        from_email: str,
        to_email: str,
        raw_message: str,
    ) -> None:
        if use_ssl:
            # SSL implicite (port 465) — OVH, Zoho, cPanel, serveurs pro
            server: smtplib.SMTP = smtplib.SMTP_SSL(
                smtp_host, smtp_port, timeout=10,
                context=ssl.create_default_context(),
            )
        else:
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=10)
        with server:
            if not use_ssl:
                server.ehlo_or_helo_if_needed()
                # STARTTLS seulement si le serveur l'annonce — un serveur
                # plain (ex. mailserver local sur :25) n'a pas d'extension TLS.
                if server.has_extn("starttls"):
                    server.starttls()
            server.login(smtp_user, smtp_password)
            refused = server.sendmail(from_email, [to_email], raw_message)
            if refused:
                # sendmail() ne lève PAS d'exception sur RCPT refusé :
                # il retourne {destinataire: (code, msg)}. Le traiter en erreur.
                raise smtplib.SMTPRecipientsRefused(refused)
