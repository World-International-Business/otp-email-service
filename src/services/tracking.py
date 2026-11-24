"""Tracking des ouvertures et clics — pixel 1px + redirection de liens.

Injecté dans les emails transactionnels quand PUBLIC_BASE_URL est
configuré (opt-out par tenant via `track_emails`, switch global
EMAIL_TRACKING_ENABLED). Les OTP ne sont JAMAIS trackés (contenu
sécurité). Le message_id sert de token capacité — non devinable.

- ``/v1/track/o/{message_id}.gif`` → pixel transparent + stats opens
- ``/v1/track/c/{message_id}?u=...`` → 302 + stats clicks
"""

import re

PIXEL_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\x00\x00\x00"
    b"!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00"
    b"\x02\x02D\x01\x00;"
)

_HREF = re.compile(r'href="(https?://[^"]+)"', re.IGNORECASE)


def tracking_base_url(settings, tenant) -> str | None:
    """Base des liens trackés : domaine white-label du tenant s'il en a un
    (CNAME vers la plateforme — les routes /v1/track/* y répondent),
    sinon le domaine plateforme."""
    domain = getattr(tenant, "tracking_domain", None)
    if domain:
        return f"https://{domain}"
    return settings.public_base_url or None


def tracking_enabled(settings, tenant) -> bool:
    return bool(
        settings.email_tracking_enabled
        and tracking_base_url(settings, tenant)
        and getattr(tenant, "track_emails", True)
    )


def inject_tracking(html_body: str, message_id: str, base_url: str) -> str:
    """Pixel ouverture + réécriture des liens <a href> en redirections
    trackées. Ne touche ni mailto: ni l'URL List-Unsubscribe ni les
    liens déjà trackés."""
    base = base_url.rstrip("/")
    pixel = (
        f'<img src="{base}/v1/track/o/{message_id}.gif" width="1" '
        'height="1" alt="" style="display:none" border="0">'
    )

    def _wrap(m: re.Match) -> str:
        url = m.group(1)
        if "/v1/track/" in url:
            return m.group(0)
        from urllib.parse import quote
        return f'href="{base}/v1/track/c/{message_id}?u={quote(url, safe="")}"'

    html_body = _HREF.sub(_wrap, html_body)
    if "</body>" in html_body.lower():
        idx = html_body.lower().rindex("</body>")
        return html_body[:idx] + pixel + html_body[idx:]
    return html_body + pixel
