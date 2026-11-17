"""Diagnostic SMTP autonome — teste l'envoi HORS de l'application.

Lit les credentials depuis .env (ou les variables d'environnement), se
connecte au serveur SMTP, affiche le dialogue de debug et envoie un email
de test au compte expéditeur lui-même.

Usage : venv\\Scripts\\python scripts\\test-smtp.py [destinataire]
"""

import os
import smtplib
import ssl
import sys
from email.mime.text import MIMEText


def load_env(path=".env"):
    env = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


def main():
    env = load_env()
    def get(k):
        return os.environ.get(k) or env.get(k, "")

    host = get("SMTP_HOST") or "smtp.gmail.com"
    port = int(get("SMTP_PORT") or 587)
    user = get("SMTP_USER")
    password = get("SMTP_PASSWORD")
    from_email = get("FROM_EMAIL") or user
    to = sys.argv[1] if len(sys.argv) > 1 else user  # auto-envoi par défaut
    use_ssl_env = get("SMTP_USE_SSL").lower()
    use_ssl = (
        use_ssl_env in ("1", "true", "yes")
        if use_ssl_env
        else port == 465  # convention : 465 = SSL implicite
    )

    print(f"Serveur   : {host}:{port} ({'SSL' if use_ssl else 'STARTTLS'})")
    print(f"Compte    : {user}")
    print(f"De        : {from_email}")
    print(f"Vers      : {to}")
    if not password or password.startswith("MOT_DE_PASSE"):
        print("\nERREUR : SMTP_PASSWORD n'est pas renseigné dans .env")
        print("(mot de passe d'application Google, pas le mot de passe normal)")
        sys.exit(2)

    msg = MIMEText("Test SMTP — si tu lis ceci, l'envoi fonctionne.", "plain", "utf-8")
    msg["Subject"] = "Test SMTP otp-email-service"
    msg["From"] = from_email
    msg["To"] = to

    print("\n--- Dialogue SMTP ---")
    try:
        if use_ssl:
            server = smtplib.SMTP_SSL(
                host, port, timeout=15, context=ssl.create_default_context()
            )
        else:
            server = smtplib.SMTP(host, port, timeout=15)
        with server:
            server.set_debuglevel(1)
            if not use_ssl:
                server.starttls()
            server.login(user, password)
            refused = server.sendmail(from_email, [to], msg.as_string())
            if refused:
                print(f"\nDESTINATAIRE REFUSÉ : {refused}")
                sys.exit(1)
    except smtplib.SMTPAuthenticationError as e:
        print(f"\nAUTH REFUSÉE : {e}")
        print("-> Le mot de passe d'application est faux ou la 2FA n'est pas active.")
        sys.exit(1)
    except Exception as e:
        print(f"\nÉCHEC ENVOI : {type(e).__name__}: {e}")
        sys.exit(1)

    print(f"\n>>> Le serveur SMTP a ACCEPTÉ le message pour {to}")
    print(">>> Vérifie 'Messages envoyés' du compte expéditeur :")
    print("    - présent  => envoyé par le serveur, le problème est côté destinataire")
    print("    - absent   => le serveur n'a pas réellement transmis le message")


if __name__ == "__main__":
    main()
