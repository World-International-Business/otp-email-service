# OTP Email Service

Microservice OTP par email pour **SendBID / PayBID** (WIB Company). Réduit les coûts SMS Twilio : les codes 2FA de login partent par email, avec fallback SMS par pays.

## Lancer en 30 secondes (Windows)

Double-clique sur **`scripts\run-local.bat`** — c'est tout.

Ou en ligne de commande :

```bat
scripts\run-local.bat
```

Puis ouvre dans le navigateur :

| URL | Description |
|-----|-------------|
| `http://localhost:8001/` | Page d'accueil |
| `http://localhost:8001/demo` | **Démo interactive** — teste envoi + vérification sans écrire de code |
| `http://localhost:8001/admin` | **Espace admin** — gestion des tenants (login = `ADMIN_API_KEY`) |
| `http://localhost:8001/docs` | Swagger UI |
| `http://localhost:8001/redoc` | ReDoc |
| `http://localhost:8001/health` | Healthcheck |

Linux/macOS : `bash scripts/run-local.sh`

## Tester sans navigateur

```bash
# Envoyer un OTP (l'email s'affiche dans les logs du serveur)
python examples/send_otp.py test@example.com FR fr

# Pays à risque → sms_fallback
python examples/send_otp.py test@example.com CN fr

# Vérifier le code (visible dans les logs)
python examples/verify_otp.py req_xxxxxxxx 123456
```

## Multi-tenant

L'API est **multi-tenant** : chaque application cliente a sa propre clé API, son secret HMAC, son branding et ses règles. Données isolées par tenant dans Redis.

**Espace admin web** : `/admin` — connexion avec la valeur de `ADMIN_API_KEY`. Permet de créer des tenants (clés affichées une fois, compte dashboard client optionnel), modifier quotas/SMTp/purposes, activer/désactiver, et consulter le journal des interactions par tenant.

**Créer un tenant** (clé admin `X-Admin-Key`) :

```bash
curl -X POST http://localhost:8001/v1/admin/tenants \
  -H "Content-Type: application/json" \
  -H "X-Admin-Key: $ADMIN_API_KEY" \
  -d '{
    "tenant_id": "boutique-awa",
    "name": "Boutique Awa",
    "brand_name": "BoutiqueAwa",
    "allowed_purposes": ["login_2fa", "order_confirmation", "password_reset"],
    "risky_countries": ["CN"]
  }'
```

Réponse : `api_key` + `hmac_secret` à donner au client **une seule fois**.

**Config par tenant** : `brand_name`, `from_email`, `from_name`, `smtp_user`, `smtp_password`, `risky_countries`, `rollout_percentage`, `allowed_purposes`, quotas.

## Endpoints API

| Méthode | Endpoint | Description |
|---------|----------|-------------|
| `POST` | `/v1/otp/send` | Envoie un OTP — `{user_id, email, country_code, purpose, language}` |
| `POST` | `/v1/otp/verify` | Vérifie un code — `{request_id, code}` |
| `POST` | `/v1/email/send` | Email transactionnel — `{to_email, type, language, data}` |
| `POST` | `/v1/admin/tenants` | **Admin** : créer un tenant (`X-Admin-Key`) |
| `GET` | `/v1/admin/tenants` | **Admin** : lister les tenants |
| `GET/PATCH/DELETE` | `/v1/admin/tenants/{id}` | **Admin** : gérer un tenant |
| `GET` | `/health` | Healthcheck |

### Types d'emails supportés (`type` dans `/v1/email/send`)

| Type | Contenu | Variables `data` |
|------|---------|------------------|
| `welcome` | Email de bienvenue | `{name}` |
| `notification` | Notification générique | `{title, message}` |
| `transaction` | Confirmation de transaction | `{amount, currency, status}` |
| `custom` | Email 100 % libre | + `subject` et `text_body` obligatoires |

**Authentification** (endpoints `/v1/otp/*`) : `X-API-Key` + `X-Timestamp` + `X-HMAC-Signature` (= `HMAC-SHA256(body + timestamp, secret)`). La collection Postman `examples/otp-email-service.postman_collection.json` calcule la signature automatiquement.

## Règles métier

- Seul `purpose: "login_2fa"` est accepté — `phone_verification` et `transaction_confirmation` sont refusés (restent sur Twilio)
- `RISKY_COUNTRIES` → `channel: "sms_fallback"` (le backend déclenche Twilio)
- `ROLLOUT_PERCENTAGE` → rollout déterministe par `user_id` (0→5→100 %)
- Max 3 envois / user + IP / 10 min, cooldown 60 s, max 5 tentatives de vérification

## Mode de test vs production

| | Test local | Production |
|---|---|---|
| Redis | `USE_FAKEREDIS=true` (en mémoire) | `redis://...` réel |
| Email | `EMAIL_PROVIDER=console` (logs) | `EMAIL_PROVIDER=gmail` |
| `.env` | Valeurs dans `run-local.bat` | Fichier `.env` (copier `.env.example`) |

## Déploiement

### Docker Compose (local / VM)

```bash
cp .env.example .env   # remplir les vraies valeurs (API_KEY, HMAC_SECRET, SMTP_*…)
docker network create dokploy-network 2>/dev/null || true
docker compose up -d --build
```

Redis n'est jamais exposé sur l'hôte — il n'est joignable que via le réseau interne `otp-network`.

### Dokploy

1. **Repository** : connecter le repo GitHub dans Dokploy → *Compose*.
2. **Compose file** : `docker-compose.yml` (détecté automatiquement).
3. **Variables d'environnement** : coller le contenu de `.env` dans l'onglet *Environment* de Dokploy (ne jamais commiter `.env`).
4. **Domaine** : Dokploy expose le service `app` via Traefik sur le réseau `dokploy-network` — configurer le domaine dans l'onglet *Domains*.
5. **Deploy** : Dokploy build l'image, démarre Redis + l'app, et le healthcheck `/health` garantit le rollout.

Secrets à générer avant déploiement :

```bash
python -c "import secrets; print(secrets.token_hex(32))"  # API_KEY / ADMIN_API_KEY
python -c "import secrets; print(secrets.token_hex(32))"  # HMAC_SECRET
```

## Intégration Django

Voir `docs/06-django-integration.md` — code client `OTPClient` à copier-coller + logique de fallback Twilio.

## Structure

```
src/
├── main.py                  # FastAPI + lifespan
├── config.py                # Config (variables d'env)
├── api/routes.py            # /v1/otp/send, /verify
├── core/                    # auth HMAC, modèles, crypto OTP
├── services/                # logique métier + rate limiter
├── email_providers/         # gmail (implémenté), brevo/resend (stubs), console
└── web/routes.py            # pages /, /demo (DEBUG uniquement)
```

Docs complètes dans `docs/` : architecture, code, déploiement, intégration Django.
