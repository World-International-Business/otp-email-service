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
# Envoyer un OTP (l'email s'affiche dans les logs du serveur en mode console)
python examples/send_otp.py test@example.com FR fr

# Pays à risque → sms_fallback
python examples/send_otp.py test@example.com CN fr

# Vérifier le code (visible dans les logs)
python examples/verify_otp.py req_xxxxxxxx 123456
```

## Envoyer de vrais emails en local

Par défaut (`run-local` sans `.env`), les emails sont **simulés** : `sent: true`
mais ils partent uniquement dans les logs du serveur (provider `console`).
Pour recevoir de vrais emails dans une boîte mail :

### 1. Créer `.env`

```bash
cp .env.example .env   # Windows : copy .env.example .env
```

### 2. Configurer le compte expéditeur Gmail

`SMTP_USER`/`SMTP_PASSWORD` = le compte qui **envoie** ; les destinataires
peuvent être n'importe quelle adresse. `SMTP_PASSWORD` n'est **pas** le mot
de passe Google normal — il faut un **mot de passe d'application** :

1. Activer la **validation en 2 étapes** sur le compte Gmail (obligatoire)
2. `myaccount.google.com` → **Sécurité** → **Mots de passe des applications**
   (ou taper « mots de passe des applications » dans la recherche du compte)
3. Générer → copier les **16 caractères** (affiché une seule fois, avec ou
   sans espaces — les deux fonctionnent)
4. Coller dans `.env` :

```ini
EMAIL_PROVIDER=gmail
SMTP_USER=mon.adresse@gmail.com
SMTP_PASSWORD=abcdefghijklmnop
FROM_EMAIL=mon.adresse@gmail.com
FROM_NAME=MonApp
DEBUG=false            # le code OTP n'apparaît plus dans /demo — boîte mail uniquement
DEMO_ENABLED=true      # garde /demo accessible en local
USE_FAKEREDIS=true
```

### 3. Tester les credentials AVANT de lancer le serveur

```bash
venv\Scripts\python scripts\test-smtp.py            # auto-test (envoie à soi-même)
venv\Scripts\python scripts\test-smtp.py autre@x.com  # vers un autre destinataire
```

Affiche le dialogue SMTP complet. `Gmail a ACCEPTÉ le message` = credentials OK.
`AUTH REFUSÉE` = mauvais mot de passe d'application ou 2FA non activée.

### 4. Relancer

```bash
scripts\run-local.bat   # affiche "config chargée depuis .env"
```

Les envois prennent ~2 s (vrai SMTP, normal). Le code OTP arrive dans la
boîte du destinataire — **regarder aussi "Messages envoyés" du compte
expéditeur et le dossier Spam du destinataire**.

### Alternatives à Gmail

- **Resend** : `EMAIL_PROVIDER=resend` + `RESEND_API_KEY` — tier gratuit
  (envoi vers l'adresse du compte uniquement sans domaine vérifié)
- **Brevo** : `EMAIL_PROVIDER=brevo` + `BREVO_API_KEY` — 300 emails/jour,
  expéditeur à vérifier
- **Par tenant** : chaque client peut connecter son propre compte email
  (SMTP ou domaine) dans `/dashboard/config` — ses envois passent par
  son compte au lieu du global

### Dépannage envois

| Symptôme | Cause | Solution |
|----------|-------|----------|
| `sent:true` instantané (~5 ms), rien reçu | Provider `console` actif | `.env` absent ou `EMAIL_PROVIDER` hérité du shell — relancer via `run-local` (purge les variables héritées) |
| `502 Failed to send email` + `smtp_send_error ... Connection unexpectedly closed` | Mot de passe placeholder/invalide | Mot de passe d'application requis |
| `smtp_send_error ... 535 Username and Password not accepted` | Mot de passe normal au lieu d'app password, ou 2FA absente | Activer la 2FA puis régénérer un app password |
| `sent:true` ~2 s mais rien reçu | Gmail a accepté, livraison retardée/spam | Vérifier "Messages envoyés" de l'expéditeur + Spam du destinataire |

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
| Email | `EMAIL_PROVIDER=console` (logs) ou réel via `.env` | `EMAIL_PROVIDER=gmail` (ou brevo/resend) |
| Démo `/demo` | `DEMO_ENABLED=true` (endpoints non authentifiés) | **`DEMO_ENABLED=false` obligatoire** |
| `.env` | `run-local` utilise `.env` s'il existe, sinon mode démo rapide | Fichier `.env` (copier `.env.example`) |

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
├── email_providers/         # gmail (SMTP), brevo/resend (API REST), console (dev)
└── web/routes.py            # pages /, /demo (DEMO_ENABLED uniquement)
```

Docs complètes dans `docs/` : architecture, code, déploiement, intégration Django,
**`docs/08-emails-reels.md`** (guide complet envoi réel : Gmail app password,
Brevo/Resend, dépannage livraison).
