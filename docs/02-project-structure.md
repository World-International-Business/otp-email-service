# 2. Arborescence du projet — otp-email-service

Arborescence **réelle** du dépôt (à jour).

```
otp-email-service/
├── .env.example                    # Template de variables (committé) — .env reste HORS git
├── .gitignore                      # Exclut .env, venv/, __pycache__, *.egg-info…
├── Dockerfile                      # Image de l'app (healthcheck intégré)
├── docker-compose.yml              # app + Redis (+ réseau externe dokploy optionnel)
├── pyproject.toml                  # Dépendances, pytest, ruff
├── README.md                       # Point d'entrée : lancement, API, déploiement
│
├── docs/
│   ├── 01-architecture.md          # Flux, schéma Redis, décisions, matrice sécurité
│   ├── 02-project-structure.md     # Ce fichier
│   ├── 03-code.md                  # Visite guidée du code — où modifier quoi
│   ├── 04-tests.md                 # Lancer les tests, lint, diagnostic SMTP, E2E
│   ├── 05-deployment.md            # Mise en production : env vars, Docker, Dokploy, checklist
│   ├── 06-django-integration.md    # Client Django copier-coller + fallback Twilio
│   ├── 07-integration-client.md    # Intégration API : HMAC par langage, erreurs, expéditeur
│   └── 08-emails-reels.md          # Gmail app password, providers, dépannage livraison
│
├── nginx/
│   └── otp-email-service.conf      # Reverse proxy HTTPS + rate limit réseau
│
├── mailserver/                     # Serveur mail auto-hébergé (option alternative à Gmail)
│   ├── README.md
│   ├── docker-compose.yml
│   └── mailserver.env
│
├── scripts/
│   ├── run-local.bat               # Lancement Windows : .env si présent, sinon démo console
│   ├── run-local.sh                # Idem Linux/macOS
│   └── test-smtp.py                # Diagnostic SMTP autonome (hors app, dialogue complet)
│
├── examples/
│   ├── send_otp.py                 # Envoyer un OTP en une commande
│   ├── verify_otp.py               # Vérifier un code en une commande
│   ├── client_example.py           # Client Python complet avec signature HMAC
│   ├── simulation_ecommerce.py     # Scénario e-commerce multi-tenant de bout en bout
│   ├── live_test.py                # Test contre un déploiement réel (secrets via env)
│   └── otp-email-service.postman_collection.json  # Collection Postman (HMAC auto)
│
├── src/
│   ├── __init__.py
│   ├── main.py                     # App FastAPI + lifespan (init Redis, provider, tenant default)
│   ├── config.py                   # Toutes les variables d'env via pydantic-settings
│   │
│   ├── api/                        # Transport HTTP — API REST /v1
│   │   ├── dependencies.py         # Redis/provider/service + IP client fiable + CSRF web
│   │   ├── middleware.py           # Logs JSON (request_id, IP, durée) + headers de sécurité
│   │   ├── routes.py               # /v1/otp/send, /v1/otp/verify, /v1/email/send, /health
│   │   └── admin_routes.py         # /v1/admin/tenants CRUD (X-Admin-Key)
│   │
│   ├── core/                       # Domaine — sans HTTP
│   │   ├── auth.py                 # API key + timestamp ±300 s + HMAC + nonce anti-rejeu
│   │   ├── models.py               # Schémas Pydantic requêtes/réponses
│   │   ├── otp.py                  # OTPStore : stockage hashé + vérification atomique
│   │   ├── security.py             # generate_code, hash_code, compare_digest
│   │   └── tenants.py              # Modèle Tenant + TenantStore Redis (clés hashées)
│   │
│   ├── email_providers/            # Infrastructure d'envoi — interface commune
│   │   ├── base.py                 # EmailProvider (abstract) + SenderIdentity
│   │   ├── gmail.py                # SMTP STARTTLS via asyncio.to_thread
│   │   ├── brevo.py                # API REST Brevo v3 (httpx, BREVO_API_KEY)
│   │   ├── resend.py               # API REST Resend (httpx, RESEND_API_KEY)
│   │   └── console.py              # Simulation : email dans les logs (dev uniquement)
│   │
│   ├── services/                   # Orchestration métier
│   │   ├── otp_service.py          # Routage OTP : purposes, pays, rollout, envoi, vérif
│   │   ├── email_service.py        # Emails transactionnels (welcome/notification/…)
│   │   ├── email_templates.py      # Templates HTML/texte (FR/EN, brand, TTL dynamique)
│   │   ├── rate_limiter.py         # Quotas/cooldowns atomiques (SET NX + INCR)
│   │   └── events.py               # Journal d'événements (logs structurés)
│   │
│   └── web/                        # Interface navigateur (HTML server-side)
│       ├── routes.py               # /, /register, /login, /dashboard, /demo
│       ├── admin.py                # /admin : gestion tenants, utilisateurs, journal
│       ├── users.py                # Comptes web : PBKDF2 600k, sessions Redis
│       ├── i18n.py                 # Sélection de langue + safe_next anti open-redirect
│       ├── translations.py         # Chaînes FR/EN des pages
│       └── docs_page.py            # Page /documentation
│
└── tests/
    └── test_core.py                # Tests unitaires : OTP, auth, tenants, modèles
```

## Principes de structuration

| Principe | Application |
|----------|-------------|
| **Séparation des responsabilités** | `api/` = HTTP, `core/` = domaine, `services/` = orchestration, `email_providers/` = infrastructure, `web/` = UI serveur. |
| **Injection de dépendances** | Redis, provider et services injectés via FastAPI dependencies (`api/dependencies.py`) — facilite les tests. |
| **Configuration par env** | `src/config.py` centralise tout via `pydantic-settings` ; les variables d'environnement priment sur `.env`. |
| **Pas de secrets en dur** | `.env.example` = placeholders uniquement ; `.env` ignoré par Git ; clés API stockées hashées. |
| **Isolation multi-tenant** | Toutes les clés Redis préfixées `t:{tenant_id}:` ; expéditeur résolu tenant → global. |
| **Documentation vivante** | `/docs` et `/redoc` générés par FastAPI ; `docs/` couvre le reste. |

## Prochaine étape

`03-code.md` : visite guidée du code — cycle de vie d'une requête, où
modifier quoi.
