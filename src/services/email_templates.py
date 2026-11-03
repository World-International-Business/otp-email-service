"""Templates email transactionnels — layout HTML partagé, branding par tenant.

Design : compatible clients email majeurs (tables + styles inline,
pas de CSS externe). Palette indigo/violet alignée sur la charte MailHub.
"""

from html import escape


def _esc(value: object) -> str:
    return escape(str(value), quote=True)


def email_layout(brand: str, content_html: str, preheader: str = "") -> str:
    """Enveloppe commune : header dégradé + carte + footer."""
    b = _esc(brand)
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
        <tr><td style="background:linear-gradient(135deg,#6366f1,#8b5cf6);
              border-radius:16px 16px 0 0; padding:26px 32px;">
          <span style="color:#fff; font-size:20px; font-weight:800; letter-spacing:.3px;">{b}</span>
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
            Message automatique de {b} — merci de ne pas répondre.<br>
            Propulsé par MailHub
          </p>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""


def otp_html(code: str, brand: str, language: str = "fr") -> str:
    """Email OTP : code en grand, avertissement anti-partage."""
    c = _esc(code)
    if language == "en":
        title = "Your verification code"
        intro = "Use this code to complete your sign-in:"
        validity = "This code expires in <strong>5 minutes</strong>."
        warning = "Never share this code — even with our support team."
        preheader = f"Your {brand} verification code is {c}"
    else:
        title = "Votre code de vérification"
        intro = "Utilisez ce code pour finaliser votre connexion :"
        validity = "Ce code expire dans <strong>5 minutes</strong>."
        warning = "Ne partagez jamais ce code, même avec notre support."
        preheader = f"Votre code {brand} est {c}"

    digits = "".join(
        f'<td align="center" style="width:44px; height:56px; background:#eef2ff;'
        f' border:1.5px solid #c7d2fe; border-radius:10px; font-size:30px;'
        f' font-weight:800; color:#4338ca; font-family:Consolas,monospace;">{_esc(d)}</td>'
        f'<td style="width:8px;"></td>'
        for d in c
    )
    content = f"""
      <h1 style="margin:0 0 8px; font-size:22px; font-weight:800; color:#111827;">{title}</h1>
      <p style="margin:0 0 24px; color:#4b5563; font-size:15px; line-height:1.6;">{intro}</p>
      <table role="presentation" cellpadding="0" cellspacing="0"
             style="margin:0 auto 24px;"><tr>{digits}</tr></table>
      <p style="margin:0 0 6px; color:#6b7280; font-size:13px;">{validity}</p>
      <p style="margin:0; color:#dc2626; font-size:13px; font-weight:700;">{warning}</p>
    """
    return email_layout(brand, content, preheader)


def generic_html(title: str, content_html: str, brand: str) -> str:
    """Wrapper pour welcome / notification / transaction / custom."""
    t = _esc(title)
    content = f"""
      <h1 style="margin:0 0 16px; font-size:22px; font-weight:800; color:#111827;">{t}</h1>
      <div style="color:#4b5563; font-size:15px; line-height:1.7;">{content_html}</div>
    """
    return email_layout(brand, content, t)
