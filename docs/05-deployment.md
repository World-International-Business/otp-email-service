# 5. Déploiement en production

## Référence des variables d'environnement

Toutes les variables sont lues par `src/config.py` (pydantic-settings).
Les variables d'environnement **priment** sur `.env`.

### Obligatoires (crash au boot si absentes)

| Variable | Contrainte | Rôle |
|----------|------------|------|
| `API_KEY` | ≥ 32 car. | Clé API du tenant `default` — générer avec `secrets.token_hex(32)` |
| `HMAC_SECRET` | ≥ 16 car. | Secret de signature du tenant `default` |
| `ADMIN_API_KEY` | ≥ 32 car. | Login `/admin` + header `X-Admin-Key` |
| `FROM_EMAIL` | email valide | Expéditeur affiché (fallback global) |

### Email

| Variable | Défaut | Rôle |
|----------|--------|------|
| `EMAIL_PROVIDER` | `gmail` | `gmail` (= `smtp`, générique) / `brevo` / `resend` / `console` |
| `SMTP_HOST` / `SMTP_PORT` | `smtp.gmail.com` / `587` | Serveur SMTP — `465` = SSL auto (adresses pro : OVH, Zoho…) |
| `SMTP_USE_SSL` | auto | Forcer SSL implicite (`true`) ou STARTTLS (`false`) |
| `SMTP_USER` / `SMTP_PASSWORD` | — | Compte expéditeur — **app password**, pas le mot de passe normal |
| `FROM_NAME` | `SendBID` | Nom d'expéditeur affiché |
| `BREVO_API_KEY` / `RESEND_API_KEY` | — | Requis si le provider correspondant est choisi |

### Application

| Variable | Défaut | Rôle |
|----------|--------|------|
| `REDIS_URL` | `redis://localhost:6379/0` | Redis externe (obligatoire en prod) |
| `USE_FAKEREDIS` | `false` | Redis en mémoire — **local/test uniquement** |
| `PUBLIC_BASE_URL` | `""` | URL publique HTTPS — requis pour les magic links, active HSTS + cookie `Secure` |
| `PORT` / `HOST` | `8000` / `0.0.0.0` | Écoute (les scripts locaux passent `8001`) |
| `DEBUG` | `false` | Active `debug_code` dans les réponses — **`false` en prod** |
| `DEMO_ENABLED` | `false` | Ouvre `/demo` non authentifié — **`false` en prod** |
| `LOG_LEVEL` | `INFO` | Niveau des logs JSON |

### Règles métier (tenant `default` au boot)

| Variable | Défaut | Rôle |
|----------|--------|------|
| `RISKY_COUNTRIES` | `CN,RU,BY,IR,KP` | Pays routés vers `sms_fallback` |
| `ROLLOUT_PERCENTAGE` | `100` | % déterministe d'users éligibles email |
| `MAX_SENDS_PER_USER_PER_10MIN` / `MAX_SENDS_PER_IP_PER_10MIN` | `3` / `3` | Quotas OTP |
| `COOLDOWN_SECONDS` | `60` | Délai min entre deux envois au même user |
| `MAX_VERIFY_ATTEMPTS` | `5` | Tentatives de vérification max |
| `OTP_TTL_SECONDS` | `300` | Durée de vie du code |
| `WEB_SIGNUP_ENABLED` / `SIGNUP_INVITE_CODE` | `true` / — | Inscription publique `/register` |

## Docker Compose

```bash
cp .env.example .env          # remplir les valeurs prod
docker compose up -d --build
docker compose logs -f app
```

- Redis n'est **jamais exposé** sur l'hôte (réseau interne `otp-network`)
- Healthchecks : Redis (`redis-cli ping`) et app (`/health`)
- Le réseau externe `dokploy-network` est **optionnel** : absent → ignoré ;
  présent → Traefik/Dokploy peut router le domaine vers l'app

## Dokploy

1. Connecter le repo GitHub → type *Compose* → `docker-compose.yml`
2. Onglet *Environment* : coller les variables de `.env` (jamais commitées)
3. Onglet *Domains* : configurer le domaine (Traefik sur `dokploy-network`)
4. Mettre `PUBLIC_BASE_URL=https://votre-domaine` dans l'env
5. Deploy — le healthcheck `/health` pilote le rollout

## VPS « à la main »

```bash
python -m venv venv && venv/bin/pip install -e .
REDIS_URL=redis://localhost:6379/0 venv/bin/uvicorn src.main:app --host 0.0.0.0 --port 8001
```

Derrière `nginx/otp-email-service.conf` (HTTPS + rate limit réseau).
Un service systemd minimal suffit (`Restart=always`, env via
`EnvironmentFile=`).

## Génération et rotation des secrets

```bash
python -c "import secrets; print(secrets.token_hex(32))"   # API_KEY / ADMIN_API_KEY
python -c "import secrets; print(secrets.token_hex(32))"   # HMAC_SECRET
```

**Rotation** : changer les variables d'env → redémarrer. Le tenant
`default` est resynchronisé au boot (`seed_default_tenant`). Les sessions
admin actives sont invalidées (le hash stocké ne correspond plus).
⚠️ Si des secrets ont fuité dans l'historique git, les régénérer **et**
les révoquer — modifier le code ne révoque rien.

## Checklist mise en production

- [ ] `DEBUG=false` — sinon `debug_code` expose les OTP dans les réponses
- [ ] `DEMO_ENABLED=false` — sinon endpoints d'envoi non authentifiés
- [ ] `USE_FAKEREDIS=false` + `REDIS_URL` réel — fakeredis perd tout au reboot
- [ ] `PUBLIC_BASE_URL=https://…` — HSTS, cookies `Secure`, magic links
- [ ] `API_KEY`/`HMAC_SECRET`/`ADMIN_API_KEY` générés, jamais dans git
- [ ] `SMTP_*` testés : `scripts/test-smtp.py` → « ACCEPTÉ »
- [ ] `EMAIL_PROVIDER` réel (`gmail`/`brevo`/`resend`), pas `console`
- [ ] `WEB_SIGNUP_ENABLED=false` ou `SIGNUP_INVITE_CODE` défini si ouvert
- [ ] HTTPS terminé (Nginx/Traefik) — le service ne sert que du HTTP
- [ ] `/health` remonte au monitoring ; logs JSON collectés (`smtp_send_error`, `http_request`)
- [ ] Secrets historiques compromis → **rotés** (voir section rotation)

## Option : serveur mail auto-hébergé

`mailserver/` contient un compose docker-mailserver + `mailserver.env`
pour s'affranchir des quotas Gmail (~500/jour). Une fois monté, pointer
`SMTP_HOST`/`SMTP_PORT`/`SMTP_USER`/`SMTP_PASSWORD` du service (ou d'un
tenant) vers ce serveur — aucun changement de code nécessaire.
