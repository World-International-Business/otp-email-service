"""Implémentation SMTP Gmail du provider d'email — supporte le sender par tenant."""

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from src.config import Settings
from src.email_providers.base import EmailProvider, SenderIdentity


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
            with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
                server.starttls()
                server.login(smtp_user, smtp_password)
                server.sendmail(from_email, [to_email], msg.as_string())
            return True
        except Exception:
            return False
