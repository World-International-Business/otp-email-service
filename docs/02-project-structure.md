# 2. Arborescence du projet — otp-email-service

```
otp-email-service/
├── .env.example                    # Variables d'environnement d'exemple (HORS git)
├── .gitignore                      # Exclut .env, __pycache__, .pytest_cache, etc.
├── Dockerfile                      # Image Python 3.11 slim avec healthcheck
├── docker-compose.yml              # Microservice + Redis localement
├── pyproject.toml                  # Dépendances et configuration pytest
├── README.md                       # Documentation utilisateur / développeur
│
├── docs/
│   ├── 01-architecture.md          # Livrable 1 : flux et schéma Redis
│   ├── 02-project-structure.md     # Livrable 2 : cette arborescence
│   ├── 03-code.md                  # Livrable 3 : explications du code
│   ├── 04-tests.md                 # Livrable 4 : stratégie et exécution des tests
│   ├── 05-deployment.md            # Livrable 5 : Docker, Nginx, VPS
│   └── 06-django-integration.md    # Livrable 6 : code client Django
│
├── nginx/
│   └── otp-email-service.conf      # Reverse proxy HTTPS + compression + rate limit
│
├── scripts/
│   └── deploy.sh                   # Déploiement VPS Ubuntu + systemd + logrotate
│
├── examples/
│   ├── curl_send.sh                # Exemple curl POST /v1/otp/send
│   ├── curl_verify.sh              # Exemple curl POST /v1/otp/verify
│   ├── client_example.py           # Client Python avec HMAC
│   └── otp-email-service.postman_collection.json
│
├── src/
│   ├── __init__.py
│   ├── main.py                     # Point d'entrée FastAPI (app, lifespan, routes)
│   ├── config.py                   # Configuration centralisée via pydantic-settings
│   │
│   ├── api/
│   │   ├── __init__.py
│   │   ├── dependencies.py         # Dépendances FastAPI (Redis, config)
│   │   ├── middleware.py           # Logging JSON + IPs + request_id
│   │   └── routes.py               # Définition des routes /send, /verify, /health
│   │
│   ├── core/
│   │   ├── __init__.py
│   │   ├── auth.py                 # Authentification API key + HMAC + timestamp
│   │   ├── models.py               # Schémas Pydantic (request/response)
│   │   ├── otp.py                  # Génération et hashing des OTP
│   │   └── security.py             # Helpers sécurisés (compare_digest, hash user_id)
│   │
│   ├── email_providers/
│   │   ├── __init__.py
│   │   ├── base.py                 # Interface abstraite EmailProvider
│   │   ├── gmail.py                # Implémentation SMTP Gmail
│   │   ├── brevo.py                # Classe prête (non implémentée)
│   │   └── resend.py               # Classe prête (non implémentée)
│   │
│   ├── services/
│   │   ├── __init__.py
│   │   ├── otp_service.py          # Logique métier send/verify
│   │   └── rate_limiter.py         # Rate limiting Redis
│   │
│   └── templates/
│       ├── otp_email_fr.html       # Template HTML FR
│       ├── otp_email_en.html       # Template HTML EN
│       └── otp_email.txt           # Template texte brut
│
└── tests/
    ├── __init__.py
    ├── conftest.py                 # Fixtures pytest (Redis, config mock, client)
    ├── test_auth.py                # Tests authentification service-à-service
    ├── test_otp.py                 # Tests génération, hash, vérification
    ├── test_rate_limiter.py        # Tests rate limit et cooldown
    └── test_integration.py         # Tests flux complet
```

## Principes de structuration

| Principe | Application |
|----------|-------------|
| **Séparation des responsabilités** | `api/` pour le transport HTTP, `core/` pour les règles métier, `services/` pour l'orchestration, `email_providers/` pour l'infrastructure. |
| **Injection de dépendances** | Redis et la config sont injectés via FastAPI dependencies pour faciliter les tests. |
| **Configuration par env** | `src/config.py` centralise toutes les variables via `pydantic-settings` avec validation. |
| **Pas de secrets en dur** | `.env.example` ne contient que des placeholders ; `.env` est ignoré par Git. |
| **Documentation vivante** | FastAPI génère `/docs` et `/redoc` automatiquement ; le README guide les utilisateurs. |
| **Tests isolés** | `conftest.py` fournit un Redis de test et un client HTTP authentifié. |

## Prochaine étape

Livrable 3 : **Code complet** — écriture de tous les fichiers `src/`.
