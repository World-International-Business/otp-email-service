"""Vérification de santé SMTP — teste les credentials SANS envoyer de mail.

Étapes testées : connexion TCP -> EHLO -> STARTTLS (si annoncé) -> LOGIN.
Un tenant dont le SMTP est mort le découvre AVANT que ses mails échouent
(badge dashboard + événement tenant.smtp_failed).
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
import time

logger = logging.getLogger(__name__)


def _probe(host: str, port: int, use_ssl: bool,
           user: str | None, password: str | None,
           timeout: int = 10) -> tuple[str, str]:
    """Test synchrone (appelé via to_thread). Retourne (stage, erreur|'')."""
    try:
        server: smtplib.SMTP
        if use_ssl:
            server = smtplib.SMTP_SSL(
                host, port, timeout=timeout,
                context=ssl.create_default_context(),
            )
        else:
            server = smtplib.SMTP(host, port, timeout=timeout)
    except Exception as exc:
        return "connect", str(exc)[:150]
    try:
        server.ehlo_or_helo_if_needed()
        if not use_ssl and server.has_extn("starttls"):
            server.starttls()
        if user or password:
            try:
                server.login(user, password)
            except smtplib.SMTPAuthenticationError as exc:
                reason = exc.smtp_error.decode("utf-8", "replace") if isinstance(
                    exc.smtp_error, bytes) else str(exc.smtp_error)
                return "login", f"auth refusée ({exc.smtp_code}) {reason[:100]}"
            except Exception as exc:
                return "login", str(exc)[:150]
    except Exception as exc:
        return "ehlo_tls", str(exc)[:150]
    finally:
        try:
            server.quit()
        except Exception:
            try:
                server.close()
            except Exception:
                pass
    return "ok", ""


async def check_smtp_health(
    host: str,
    port: int,
    user: str | None,
    password: str | None,
    use_ssl: bool,
) -> dict:
    """Teste une config SMTP. Retourne {ok, stage, error, latency_ms}."""
    started = time.monotonic()
    stage, error = await asyncio.to_thread(
        _probe, host, int(port), bool(use_ssl), user, password
    )
    latency = int((time.monotonic() - started) * 1000)
    ok = stage == "ok"
    if not ok:
        logger.warning(
            "smtp_health_failed",
            extra={"smtp_host": host, "smtp_port": port,
                   "stage": stage, "error": error},
        )
    return {
        "ok": ok,
        "stage": stage,
        "error": error,
        "latency_ms": latency,
        "checked_at": int(time.time()),
    }
