# 3. Visite guidée du code — où modifier quoi

Ce document explique comment le code s'articule, le cycle de vie d'une
requête, et où intervenir pour les modifications courantes.

## Point d'entrée : `src/main.py`

Le lifespan FastAPI fait l'initialisation au démarrage :

1. Charge `Settings` (`config.py`) — crash explicite si une variable
   obligatoire manque
2. Connecte Redis (`redis.asyncio`) ou **fakeredis** si `USE_FAKEREDIS=true`
3. Instancie le provider email selon `EMAIL_PROVIDER` (`gmail`, `brevo`,
   `resend`, `console`) — les providers à clé (`brevo`/`resend`) échouent
   au boot si leur clé manque
4. `seed_default_tenant()` : crée/met à jour le tenant `default` depuis
   les variables d'env (`API_KEY`, `HMAC_SECRET`, quotas…) — une rotation
   des secrets d'env s'applique donc automatiquement au redémarrage
5. Monte les routers : `api.routes` (`/v1`), `api.admin_routes`
   (`/v1/admin`), `web.routes` + `web.admin` (pages)

## Cycle de vie : `POST /v1/otp/send`

```
Request
  │
  ├─ middleware.py          → log JSON (request_id, IP, durée) + headers sécurité
  │
  ├─ api/routes.py          → dépendances FastAPI
  │     └─ dependencies.py  → get_tenant : X-API-Key → sha256 → tenant:bykey:*
  │                         → verify_hmac : timestamp ±300 s + signature
  │                           + nonce Redis anti-rejeu (1 signature = 1 usage)
  │                         → get_client_ip : X-Forwarded-For honoré
  │                           UNIQUEMENT depuis un pair de confiance
  │
  ├─ services/otp_service.py::send_otp
  │     ├─ purpose autorisé ?           → 403 sinon
  │     ├─ country_code ∈ risky_countries ? → channel: sms_fallback
  │     ├─ hash(user_id) % 100 < rollout ?  → sinon sms_fallback
  │     ├─ rate_limiter.try_send        → réservation ATOMIQUE :
  │     │     cooldown (SET NX) + compteurs user/IP (INCR) → 429 si dépassé
  │     ├─ OTPStore.create              → code 6 chiffres, hash stocké, TTL
  │     ├─ provider.send_email          → sender = tenant.smtp_* || global
  │     └─ échec provider               → release_send (rend le slot) + 502
  │
  └─ Response {channel, request_id, expires_in}  (+ debug_code si DEBUG)
```

`POST /v1/otp/verify` : `verify_otp()` — `INCR` du compteur de tentatives
(aligné sur le TTL, ne le prolonge pas), `compare_digest` sur les hashes,
**suppression de la clé** en cas de succès → usage unique garanti.

`POST /v1/email/send` : même chaîne, via `email_service` + templates
transactionnels (`welcome`, `notification`, `transaction`, `custom`) et
quota **par destinataire**.

## Résolution de l'expéditeur (point clé)

`otp_service._sender(tenant)` / `email_service._sender(tenant)` :

```python
smtp_user     = tenant.smtp_user     or settings.smtp_user
smtp_password = tenant.smtp_password or settings.smtp_password
from_email    = tenant.from_email    or settings.from_email
```

→ Le client configure son propre compte dans `/dashboard/config` (ou
l'admin dans le tenant) et **ses emails partent de son compte**.
Sans config tenant → compte global de l'opérateur.

## Où modifier quoi

| Besoin | Fichier |
|--------|---------|
| Nouvelle variable d'env | `src/config.py` (+ `.env.example`, docs) |
| Nouvel endpoint métier | `src/api/routes.py` + service + `core/models.py` |
| Endpoint admin | `src/api/admin_routes.py` (+ `web/admin.py` si UI) |
| Quotas / cooldown par défaut | `Tenant` dans `src/core/tenants.py` |
| Règles de routage (pays, rollout, purposes) | `src/services/otp_service.py` |
| Nouveau provider email | `src/email_providers/` : copier `base.py` → implémenter `send_email`, brancher dans `main.py` |
| Templates d'emails (FR/EN, marque, TTL) | `src/services/email_templates.py` |
| Textes/pages web | `src/web/translations.py`, `routes.py`, `admin.py` |
| Sécurité HTTP (headers, logs) | `src/api/middleware.py` |
| Règles HMAC / anti-rejeu | `src/core/auth.py` |
| Format des codes OTP (longueur, hash) | `src/core/security.py` + `core/otp.py` |

## Conventions à respecter

- **Rien en clair dans Redis** : API keys, codes OTP, sessions admin →
  hashés (`hash_code` = SHA-256). `webuser:creds:` est l'exception
  assumée (clés affichables 24 h, TTL).
- **Atomicité Redis** : jamais de check-then-act — `SET NX`, `INCR`,
  pipelines (`rate_limiter.try_send`/`release_send` en sont le modèle).
- **Synchrone → thread** : toute lib bloquante (`smtplib`) passe par
  `asyncio.to_thread` — jamais d'appel bloquant dans l'event loop.
- **Échappement HTML** : toute donnée utilisateur rendue dans une page
  ou un email passe par `_e()` / échappement explicite.
- **`set(ex=…)`** : `setex` est déprécié dans redis-py.
- **PATCH** : `model_dump(exclude_unset=True)` — champ absent = inchangé,
  `null` explicite = effacé.

## Flux web (pages)

`web/routes.py` : pages publiques (`/`, `/register`, `/login`), dashboard
client (`/dashboard` — stats, config SMTP, rotation clés), démo
(`/demo` — uniquement si `DEMO_ENABLED=true`). Toutes les routes POST
passent `require_same_origin` (anti-CSRF). Sessions : cookie
`session`/`adminsession` (HttpOnly, SameSite=Lax, Secure si HTTPS,
admin scopé `path=/admin`).

`web/users.py` : PBKDF2 `600_000` itérations, format versionné
`pbkdf2$itérations$hex` — les anciens hashes sont migrés au login.

## Prochaine étape

`04-tests.md` : lancer la suite, le lint, le diagnostic SMTP et les
scénarios E2E.
