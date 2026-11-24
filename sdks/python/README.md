# MailHub SDK (Python)

Client officiel — OTP par email, emails transactionnels, statuts d'envoi.
**Stdlib pure** : aucune dépendance, Python ≥ 3.10.

```bash
pip install ./sdks/python          # depuis le repo
# ou copiez mailhub/ dans votre projet
```

## Usage

```python
from mailhub import MailHubClient, MailHubError

client = MailHubClient(
    base_url="https://mail.maboite.com",
    api_key="mk_...",          # X-API-Key de votre tenant
    hmac_secret="whsec_...",   # secret HMAC du tenant
)

# OTP
res = client.send_otp(
    user_id="usr_123", email="user@ex.com",
    country_code="FR", purpose="login_2fa",
    language="fr", magic_link=True,
)
ok = client.verify_otp(res["request_id"], "123456")
assert ok["verified"]

# Email transactionnel
msg = client.send_email(
    to_email="client@ex.com", type="notification", language="fr",
    data={"title": "Commande", "message": "Expédiée"},
    idempotency_key="cmd-8842",
)
# Programmé : send_at="2026-12-01T09:00:00Z"
# Custom    : type="custom", subject=..., text_body=..., html_body=...

# Statut d'un envoi
print(client.email_status(msg["message_id"]))
# → {'status': 'sent', 'attempts': 1, 'error': None, ...}
```

## Erreurs

`MailHubError` : `.status` (HTTP, 0 = réseau) + `.detail` (message serveur).

| Code | Cause |
|---|---|
| 401 | X-API-Key absent/invalide |
| 403 | Signature HMAC ou horodatage invalide (±5 min) |
| 422 | Payload invalide ou destinataire supprimé |
| 429 | Rate limit / warm-up — voir header `Retry-After` |
| 502 | Échec SMTP (voir `detail`) |
