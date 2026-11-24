"""Vérification DNS de délivrabilité pour un domaine expéditeur.

Contrôle MX, SPF (v=spf1), DMARC (_dmarc) et DKIM (sélecteurs courants) —
les 4 enregistrements qui décident spam vs boîte de réception. Affiché dans
le dashboard tenant sous forme de badges + instructions.
"""

from __future__ import annotations

import asyncio
import logging
import time

import dns.resolver

logger = logging.getLogger(__name__)

DKIM_SELECTORS = ("mail", "default", "google", "s1", "s2", "selector1",
                  "selector2", "dkim", "k1", "smtp", "mandrill", "sendgrid")


def _txt(domain: str) -> list[str]:
    try:
        return [
            b"".join(r.strings).decode("utf-8", "replace")
            for r in dns.resolver.resolve(domain, "TXT", lifetime=5)
        ]
    except Exception:
        return []


def _exists(domain: str, rtype: str) -> bool:
    try:
        return bool(dns.resolver.resolve(domain, rtype, lifetime=5))
    except Exception:
        return False


def _check(domain: str) -> dict:
    """Contrôle synchrone (to_thread côté appelant)."""
    mx = _exists(domain, "MX")
    txts = _txt(domain)
    spf = next((t for t in txts if t.startswith("v=spf1")), "")
    dmarc = next((t for t in _txt(f"_dmarc.{domain}") if "v=DMARC1" in t), "")
    dkim_selector = next(
        (s for s in DKIM_SELECTORS if _exists(f"{s}._domainkey.{domain}", "TXT")),
        "",
    )
    return {
        "domain": domain,
        "mx": mx,
        "spf": spf,
        "spf_ok": bool(spf),
        "dmarc": dmarc,
        "dmarc_ok": bool(dmarc),
        "dkim_selector": dkim_selector,
        "dkim_ok": bool(dkim_selector),
        "checked_at": int(time.time()),
    }


async def check_email_domain(domain: str) -> dict:
    """Version async du contrôle DNS. `domain` = domaine de l'expéditeur."""
    return await asyncio.to_thread(_check, domain)


def domain_verdict(result: dict) -> str:
    """Résumé lisible : 'ok' si tout est en place, sinon liste des manques."""
    missing = [
        name for name, key in (("MX", "mx"), ("SPF", "spf_ok"),
                               ("DKIM", "dkim_ok"), ("DMARC", "dmarc_ok"))
        if not result.get(key)
    ]
    return "ok" if not missing else "manquant: " + ", ".join(missing)
