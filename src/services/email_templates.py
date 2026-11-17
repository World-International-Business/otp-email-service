"""Templates email transactionnels — layout HTML partagé, branding par tenant.

Design : compatible clients email majeurs (tables + styles inline,
pas de CSS externe). Palette indigo/violet alignée sur la charte MailHub.
"""

from html import escape


def _esc(value: object) -> str:
    return escape(str(value), quote=True)


def _sanitize_color(color: str) -> str:
    """Accepte uniquement #rgb/#rrggbb, sinon fallback indigo."""
    c = (color or "").strip()
    if len(c) in (4, 7) and c.startswith("#") and all(
        ch in "0123456789abcdefABCDEF" for ch in c[1:]
    ):
        return c
    return "#6366f1"


def email_layout(
    brand: str,
    content_html: str,
    preheader: str = "",
    color: str = "#6366f1",
    logo_url: str | None = None,
    footer: str | None = None,
) -> str:
    """Enveloppe commune : header marque + carte + footer (personnalisable)."""
    b = _esc(brand)
    accent = _sanitize_color(color)
    logo = (
        f'<img src="{_esc(logo_url)}" alt="{b}" '
        'style="max-height:36px; max-width:180px; display:block;">'
        if logo_url
        else (
            f'<span style="color:#fff; font-size:20px; font-weight:800;'
            f' letter-spacing:.3px;">{b}</span>'
        )
    )
    footer_text = _esc(footer) if footer else (
        f"Message automatique de {b} — merci de ne pas répondre."
    )
    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{b}</title>
</head>
<body style="margin:0; padding:0; background:#eef0f6;
     font-family:'Segoe UI', Arial, sans-serif; -webkit-font-smoothing:antialiased;">
  <div style="display:none; max-height:0; overflow:hidden;">{_esc(preheader)}</div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
         style="background:#eef0f6; padding:32px 16px;">
    <tr><td align="center">
      <table role="presentation" width="520" cellpadding="0" cellspacing="0"
             style="max-width:520px; width:100%;">

        <!-- Header marque -->
        <tr><td style="background:{accent};
              border-radius:16px 16px 0 0; padding:26px 32px;">
          {logo}
        </td></tr>

        <!-- Corps -->
        <tr><td style="background:#ffffff; padding:36px 32px; border-left:1px solid #e5e7f0;
              border-right:1px solid #e5e7f0;">
          {content_html}
        </td></tr>

        <!-- Footer -->
        <tr><td style="background:#f8f9fd; border:1px solid #e5e7f0; border-top:none;
              border-radius:0 0 16px 16px; padding:20px 32px; text-align:center;">
          <p style="margin:0; color:#9aa0b4; font-size:12px; line-height:1.6;">
            {footer_text}<br>
            Propulsé par MailHub
          </p>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""


def otp_html(
    code: str,
    brand: str,
    language: str = "fr",
    magic_url: str | None = None,
    ttl_minutes: int = 5,
    color: str = "#6366f1",
    logo_url: str | None = None,
    footer: str | None = None,
) -> str:
    """Email OTP : code en grand, avertissement anti-partage, bouton magique optionnel."""
    c = _esc(code)
    if language == "en":
        title = "Your verification code"
        intro = "Use this code to complete your sign-in:"
        validity = (
            f"This code expires in <strong>{ttl_minutes} "
            f"{'minute' if ttl_minutes == 1 else 'minutes'}</strong>."
        )
        warning = "Never share this code — even with our support team."
        preheader = f"Your {brand} verification code is {c}"
    else:
        title = "Votre code de vérification"
        intro = "Utilisez ce code pour finaliser votre connexion :"
        validity = (
            f"Ce code expire dans <strong>{ttl_minutes} "
            f"{'minute' if ttl_minutes == 1 else 'minutes'}</strong>."
        )
        warning = "Ne partagez jamais ce code, même avec notre support."
        preheader = f"Votre code {brand} est {c}"

    digits = "".join(
        f'<td align="center" style="width:44px; height:56px; background:#eef2ff;'
        f' border:1.5px solid #c7d2fe; border-radius:10px; font-size:30px;'
        f' font-weight:800; color:{_sanitize_color(color)};'
        f' font-family:Consolas,monospace;">{_esc(d)}</td>'
        f'<td style="width:8px;"></td>'
        for d in c
    )
    button = ""
    if magic_url:
        btn_label = "Verify with one click" if language == "en" else "Vérifier en un clic"
        button = f"""
      <div style="text-align:center; margin:0 0 24px;">
        <a href="{_esc(magic_url)}" style="display:inline-block;
           background:{_sanitize_color(color)};
           color:#fff; font-weight:700; font-size:15px; padding:13px 34px; border-radius:10px;
           text-decoration:none;">{btn_label}</a>
      </div>"""

    content = f"""
      <h1 style="margin:0 0 8px; font-size:22px; font-weight:800; color:#111827;">{title}</h1>
      <p style="margin:0 0 24px; color:#4b5563; font-size:15px; line-height:1.6;">{intro}</p>
      <table role="presentation" cellpadding="0" cellspacing="0"
             style="margin:0 auto 24px;"><tr>{digits}</tr></table>
      {button}
      <p style="margin:0 0 6px; color:#6b7280; font-size:13px;">{validity}</p>
      <p style="margin:0; color:#dc2626; font-size:13px; font-weight:700;">{warning}</p>
    """
    return email_layout(brand, content, preheader, color, logo_url, footer)


def generic_html(
    title: str,
    content_html: str,
    brand: str,
    color: str = "#6366f1",
    logo_url: str | None = None,
    footer: str | None = None,
) -> str:
    """Wrapper pour welcome / notification / transaction / custom."""
    t = _esc(title)
    content = f"""
      <h1 style="margin:0 0 16px; font-size:22px; font-weight:800; color:#111827;">{t}</h1>
      <div style="color:#4b5563; font-size:15px; line-height:1.7;">{content_html}</div>
    """
    return email_layout(brand, content, t, color, logo_url, footer)
