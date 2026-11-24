# MailHub — OTP Email Service

Microservice **multi-tenant** d'envoi de codes OTP et d'emails transactionnels
(FastAPI + Redis). Chaque client (tenant) a sa clé API, son secret HMAC, son
expéditeur SMTP et ses quotas. Réduit les coûts SMS : le 2FA part par email,
avec routage `sms_fallback` configurable par pays.

## Lancer en local (Windows)

Double-cliquer **`scripts\run-local.bat`** — c'est tout. Le script :

1. Purge les variables d'environnement héritées du shell (qui écraseraient `.env`)
2. Démarre le conteneur Redis persistant `mailhub-redis` si présent (Docker requis pour la persistance)
3. Charge `.env` s'il existe — sinon mode démo rapide (fakeredis + provider console)

Linux/macOS : `bash scripts/run-local.sh`

| URL | Description |
|-----|-------------|
| `http://localhost:8001/` | Page d'accueil (redirige vers `/dashboard` si connecté) |
| `http://localhost:8001/demo` | Démo interactive — envoi + vérification OTP, email transactionnel |
| `http://localhost:8001/dashboard` | Espace client — config SMTP, branding, test d'envoi |
| `http://localhost:8001/admin` | Espace admin — gestion des tenants (login = `ADMIN_API_KEY` ou `ADMIN_WEB_PASSWORD`) |
| `http://localhost:8001/documentation` | **Guide d'intégration complet** (HMAC, endpoints, exemples par langage) |
| `http://localhost:8001/docs` / `/redoc` | Swagger UI / ReDoc |
| `http://localhost:8001/health` | Healthcheck `{status, redis, version}` |

## Configuration — `.env`

```bash
cp .env.example .env   # Windows : copy .env.example .env
```

| Variable | Rôle | Défaut |
|----------|------|--------|
| `PORT` / `APP_PORT` | Port HTTP | 8001 local / 8000 docker |
| `DEBUG` | `true` → `debug_code` renvoie le code OTP dans la réponse (jamais en prod) | `false` |
| `DEMO_ENABLED` | Active `/demo` (endpoints non authentifiés) — **`false` en prod** | `false` |
| `API_KEY`, `HMAC_SECRET`, `ADMIN_API_KEY` | Secrets service — générer avec `secrets.token_hex(32)` | — |
| `ADMIN_WEB_PASSWORD` | Mot de passe mémorisable pour `/admin/login` (en plus de `ADMIN_API_KEY`) | vide |
| `USE_FAKEREDIS` | `true` = Redis en mémoire (**tout perdu au restart**) · `false` = vrai Redis | `false` |
| `REDIS_URL` | Connexion Redis | `redis://localhost:6379/0` |
| `EMAIL_PROVIDER` | `gmail` / `smtp` (alias générique) / `brevo` / `resend` / `console` | `console` |
| `SMTP_HOST` `SMTP_PORT` | Serveur SMTP — `587` STARTTLS · `465` SSL auto | `smtp.gmail.com` `587` |
| `SMTP_USER` `SMTP_PASSWORD` | Compte qui **envoie** réellement | — |
| `SMTP_USE_SSL` | Forcer le chiffrement — vide = auto par port | auto |
| `FROM_EMAIL` `FROM_NAME` | Expéditeur affiché | — |
| `RISKY_COUNTRIES` | Pays → `sms_fallback` | `CN,RU,BY,IR,KP` |
| `ROLLOUT_PERCENTAGE` | % d'utilisateurs routés email (déterministe par `user_id`) | `100` |
| `MAX_SENDS_PER_USER_PER_10MIN` / `_IP_` / `COOLDOWN_SECONDS` / `MAX_VERIFY_ATTEMPTS` / `OTP_TTL_SECONDS` | Quotas globaux (surchargeables par tenant) | `3 / 3 / 60 / 5 / 300` |
| `PUBLIC_BASE_URL` | URL publique (`https://…`) — magic links OTP + logo emails | vide |
| `EMAIL_QUEUE_ENABLED` / `EMAIL_QUEUE_POLL_SECONDS` / `EMAIL_MAX_ATTEMPTS` | File d'envoi async + retries (60s/5min/15min) | `true / 1 / 4` |
| `LIST_UNSUBSCRIBE_URL` | En-tête `List-Unsubscribe` (délivrabilité Gmail/Yahoo) | vide |
| `STRIPE_SECRET_KEY` / `STRIPE_WEBHOOK_SECRET` / `STRIPE_PRICE_*` | Paiement carte — activation plan immédiate au webhook | vide |
| `PAYPAL_CLIENT_ID` / `PAYPAL_CLIENT_SECRET` / `PAYPAL_WEBHOOK_ID` / `PAYPAL_ENV` | PayPal (`live`/`sandbox`) | vide |
| `CINETPAY_API_KEY` / `CINETPAY_SITE_ID` | Agrégateur mobile money (Orange/MTN/Wave…) | vide |
| `PLAN_PRICE_{PRO,ENTERPRISE}[_YEARLY]` | Tarifs carte en `BILLING_CURRENCY` | `0` / `EUR` |
| `PLAN_PRICE_{PRO,ENTERPRISE}_MOBILE[_YEARLY]` | Tarifs mobile en `BILLING_CURRENCY_MOBILE` | `0` / `XOF` |
| `BILLING_BANK_DETAILS` / `BILLING_MOMO_NUMBER` / `BILLING_MOMO_NAME` / `BILLING_MANUAL_NOTE` | Coordonnées affichées pour les moyens manuels (virement, momo direct, autre) — validation admin | vide |
| `BILLING_CHECK_INTERVAL_SECONDS` | Cadence expiration plans + rappels J-7/J-3/J-1 | `3600` |

### Persistance des données

`USE_FAKEREDIS=false` + `REDIS_URL` → comptes, tenants, config SMTP et sessions
**survivent aux redémarrages**. En local, le conteneur Docker `mailhub-redis`
(AOF + volume `mailhub-redis-data`) est créé via :

```bash
docker run -d --name mailhub-redis --restart unless-stopped \
  -p 6379:6379 -v mailhub-redis-data:/data redis:7-alpine \
  redis-server --appendonly yes
```

`run-local` le redémarre automatiquement. Sans Docker, `USE_FAKEREDIS=true`
reste possible mais **rien ne persiste** (comptes à recréer à chaque run).

## Envoyer de vrais emails

### Option A — compte Gmail

`SMTP_PASSWORD` n'est **pas** le mot de passe Google — il faut un
**mot de passe d'application** (2FA requise) :

1. `myaccount.google.com` → Sécurité → **Mots de passe des applications**
2. Générer → copier les 16 caractères
3. `.env` :

```ini
EMAIL_PROVIDER=gmail
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=mon.adresse@gmail.com
SMTP_PASSWORD=abcdefghijklmnop
FROM_EMAIL=mon.adresse@gmail.com
FROM_NAME=MonApp
```

### Option B — adresse professionnelle (n'importe quel hébergeur)

Même mécanisme — le provider `gmail`/`smtp` est un client SMTP générique.
**Le host est celui de l'hébergeur de la boîte**, le user = l'adresse complète :

| Hébergeur | `SMTP_HOST` | `SMTP_PORT` | Password |
|-----------|-------------|-------------|----------|
| Google Workspace | `smtp.gmail.com` | `587` | App password du compte |
| IONOS | `smtp.ionos.fr` (FR) / `.com` | `587` ou `465` | Mot de passe de la boîte |
| OVH | `ssl0.ovh.net` | `465` (SSL auto) | Mot de passe de la boîte |
| Zoho | `smtp.zoho.com` | `465` | Mot de passe / app password |
| cPanel / mutualisé | `mail.mondomaine.com` | `465` | Mot de passe de la boîte |
| Serveur dédié | votre `mailserver/` | `587`/`465` | Credential de la boîte |

Règle d'or : **`SMTP_USER` décide qui envoie vraiment** — `FROM_EMAIL` doit être
une adresse autorisée par ce compte (Gmail réécrit un `From` étranger ; IONOS
le rejette `550`). Pour la délivrabilité : enregistrements **SPF + DKIM**
dans le DNS du domaine. Détails : `docs/08-emails-reels.md`.

### Option C — Brevo / Resend (API REST)

`EMAIL_PROVIDER=brevo` + `BREVO_API_KEY` (300/j gratuits, domaine vérifiable)
ou `EMAIL_PROVIDER=resend` + `RESEND_API_KEY`.

### Tester les credentials avant de lancer

```bash
venv\Scripts\python scripts\test-smtp.py                # auto-test (envoie à soi-même)
venv\Scripts\python scripts\test-smtp.py autre@x.com    # autre destinataire
```

## Multi-tenant

Chaque application cliente = un **tenant** : clé API, secret HMAC, expéditeur,
branding, quotas — isolés dans Redis et relus à chaque requête (modif en
dashboard = effet immédiat, sans restart).

**Créer un tenant** (`X-Admin-Key` ou panneau `/admin` ou compte `/register`) :

```bash
curl -X POST http://localhost:8001/v1/admin/tenants \
  -H "Content-Type: application/json" -H "X-Admin-Key: $ADMIN_API_KEY" \
  -d '{"tenant_id": "boutique-awa", "name": "Boutique Awa",
       "allowed_purposes": ["login_2fa", "password_reset"]}'
```

Réponse : `api_key` + `hmac_secret` affichés **une seule fois**.

**Champs tenant** : `name`, `brand_name`, `from_email`, `from_name`,
`smtp_host`, `smtp_port`, `smtp_user`, `smtp_password`, `smtp_use_ssl`,
`risky_countries`, `rollout_percentage`, `allowed_purposes`,
`max_sends_per_user_per_10min`, `max_sends_per_ip_per_10min`,
`cooldown_seconds`, `max_verify_attempts`, `otp_ttl_seconds`, `is_active`.

Le tenant `default` (auto-créé) utilise la config globale `.env` — pratique
pour `/demo`. Un envoi utilise d'abord `smtp_*`/`from_*` du tenant, sinon le
global.

## Endpoints API

| Méthode | Endpoint | Auth | Description |
|---------|----------|------|-------------|
| `POST` | `/v1/otp/send` | HMAC | OTP 6 chiffres — `{user_id (UUID), email, country_code, purpose, language?, magic_link?}` |
| `POST` | `/v1/otp/verify` | HMAC | `{request_id, code (6 chiffres)}` → `{verified, attempts_remaining}` |
| `GET` | `/v1/otp/confirm` | lien | Lien magique (`magic_link=true`) — consomme le code au clic |
| `POST` | `/v1/email/send` | HMAC | Transactionnel — `{to_email, type, language?, data?, subject?, text_body?, html_body?, send_at?}` → `{sent, status:"queued"\|"sent", message_id, scheduled_for?}`. `send_at` (ISO 8601, ≤30 j) = envoi programmé |
| `GET` | `/v1/email/status/{message_id}` | HMAC | Statut d'un envoi — `queued` / `retry` / `sent` / `dead` + `attempts`, `error` |
| `POST` | `/v1/billing/webhooks/{stripe,paypal,cinetpay}` | signature provider | Callbacks paiement — activation du plan dès confirmation |
| `POST` | `/v1/events/email` | `X-Admin-Key` | Bounces/plaintes entrants (Brevo, Resend ou générique) → liste de suppression + stats réputation |
| `POST` | `/v1/admin/tenants` | `X-Admin-Key` | Créer un tenant |
| `GET` | `/v1/admin/tenants` | `X-Admin-Key` | Lister les tenants |
| `GET/PATCH/DELETE` | `/v1/admin/tenants/{id}` | `X-Admin-Key` | Gérer un tenant |
| `GET` | `/health` | — | `{status, redis, version}` |

**Signature** : `X-API-Key` + `X-Timestamp` (secondes, ±5 min) +
`X-HMAC-Signature` = `hex(HMAC_SHA256(secret, body_brut + timestamp [+ "|" + nonce]))`.
`X-Nonce` (aléatoire par requête) est **recommandé** : sans lui, deux appels
identiques dans la même seconde partageraient la même signature → `403 Replay
detected`. Une signature n'est utilisable qu'une fois (nonce Redis, anti-rejeu).

Types d'emails : `welcome` `{name}` · `notification` `{title, message}` ·
`transaction` `{amount, currency, status}` · `custom` (`subject`+`text_body`
racine, `html_body` optionnel).

➡️ **Guide d'intégration complet** : `http://localhost:8001/documentation`
(en ligne) et `docs/07-integration-client.md` (référence exhaustive —
exemples Python/Node/PHP/cURL/Dart, gestion des erreurs, pièges de signature).

## Règles métier

- `purpose` doit figurer dans `allowed_purposes` du tenant — sinon `403`
- `risky_countries` → `channel: "sms_fallback"` (l'app cliente envoie le SMS)
- `rollout_percentage` → routage déterministe par `user_id`
- OTP : 6 chiffres, usage unique, TTL `otp_ttl_seconds`, max `max_verify_attempts` tentatives
- `429` → header `Retry-After` (secondes) · `502` → `Failed to send email: <raison SMTP>`
- Les codes OTP **n'apparaissent jamais dans l'UI** (sauf `debug_code` si `DEBUG=true`)

## Dépannage

| Symptôme | Cause | Solution |
|----------|-------|----------|
| `sent:true` ~5 ms, rien reçu | Provider `console` actif | `.env` absent ou var héritée du shell — relancer via `run-local` |
| `535 Authentication credentials invalid` | Mauvais password **ou mauvais host** (ex. boîte IONOS sur `smtp.gmail.com` ou `smtp.ionos.com` au lieu de `.fr`) | Host = hébergeur de la boîte + password de la boîte |
| `550 Sender address is not allowed` | `from_email` étranger au compte SMTP | `FROM_EMAIL` = adresse du `SMTP_USER` (ou alias vérifié) |
| `Connection unexpectedly closed` | Mauvais port/mode TLS ou host | `587`→STARTTLS, `465`→SSL ; vérifier host |
| Email part d'un autre expéditeur | Tenant `default` ou tenant sans `smtp_*` → config globale | Choisir le bon tenant / remplir sa config |
| « Email ou mot de passe incorrect » après restart | Compte créé pendant un run fakeredis | Recréer une fois — vérif `USE_FAKEREDIS=false` + `mailhub-redis` up |
| `429 Too many OTP requests` | Quota tenant | Attendre `Retry-After` ou ajuster les champs quota du tenant |
| Emails acceptés mais non reçus | SPF/DKIM absents, spam, quota Gmail | « Messages envoyés » + Spam + DNS du domaine |

## Déploiement

### Docker Compose

```bash
cp .env.example .env   # remplir les vraies valeurs
docker network create dokploy-network 2>/dev/null || true
docker compose up -d --build
```

Redis (`otp-redis`, AOF + volume `redis_data`) n'est jamais exposé sur l'hôte —
réseau interne uniquement.

### Dokploy

1. Connecter le repo → *Compose* (`docker-compose.yml` détecté)
2. Coller le contenu de `.env` dans *Environment* (jamais commité)
3. Configurer le domaine (*Domains* → Traefik, réseau `dokploy-network`)
4. Deploy — le healthcheck `/health` pilote le rollout

```bash
python -c "import secrets; print(secrets.token_hex(32))"  # API_KEY / ADMIN_API_KEY / HMAC_SECRET
```

## Structure

```
src/
├── main.py                  # FastAPI + lifespan
├── config.py                # Variables d'environnement
├── api/                     # routes /v1/*, admin, dépendances, middleware
├── core/                    # auth HMAC, modèles, OTP, tenants
├── services/                # otp_service, email_service, templates, rate_limiter
├── email_providers/         # gmail (SMTP générique + SSL), brevo, resend, console
└── web/                     # pages publiques, auth, dashboard, admin, /documentation, i18n
scripts/                     # run-local (win/unix), test-smtp
examples/                    # client Python, simulation, collection Postman
docs/                        # documentation complète (voir ci-dessous)
```

| Doc | Contenu |
|-----|---------|
| `docs/01-architecture.md` | Flux, schéma Redis, décisions, matrice sécurité |
| `docs/02-project-structure.md` | Arborescence réelle |
| `docs/03-code.md` | Visite guidée du code |
| `docs/04-tests.md` | Tests, lint, diagnostic SMTP, E2E |
| `docs/05-deployment.md` | Env vars, Docker/Dokploy/VPS, rotation secrets |
| `docs/06-django-integration.md` | Client Django + fallback Twilio |
| `docs/07-integration-client.md` | **Référence intégration exhaustive** — HMAC, endpoints, erreurs, langages |
| `docs/08-emails-reels.md` | Gmail app password, SMTP pro, délivrabilité, dépannage |
