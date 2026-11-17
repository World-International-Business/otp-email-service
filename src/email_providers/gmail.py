"""Implémentation SMTP Gmail du provider d'email — supporte le sender par tenant."""

import asyncio
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from src.config import Settings
from src.email_providers.base import EmailProvider, SenderIdentity

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
                smtp_host, smtp_port, smtp_user, smtp_password,
                from_email, to_email, msg.as_string(),
            )
            return True
        except Exception as exc:
            # Ne jamais logger smtp_password — le message d'erreur SMTP
            # (ex. "535 Username and Password not accepted") suffit au diag.
            logger.error(
                "smtp_send_error",
                extra={"smtp_host": smtp_host, "smtp_port": smtp_port,
                       "error": str(exc)},
            )
            return False

    @staticmethod
    def _send_smtp(
        smtp_host: str,
        smtp_port: int,
        smtp_user: str | None,
        smtp_password: str | None,
        from_email: str,
        to_email: str,
        raw_message: str,
    ) -> None:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            refused = server.sendmail(from_email, [to_email], raw_message)
            if refused:
                # sendmail() ne lève PAS d'exception sur RCPT refusé :
                # il retourne {destinataire: (code, msg)}. Le traiter en erreur.
                raise smtplib.SMTPRecipientsRefused(refused)
