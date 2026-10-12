# 1. Architecture + Diagrammes — otp-email-service

## Vue d'ensemble

Le microservice **otp-email-service** est un service Python/FastAPI autonome, appelé par le backend Django de SendBID/PayBID uniquement pour les OTP de login 2FA. Il remplace progressivement les SMS Twilio pour ce use case, tout en préservant le SMS comme canal de secours pour certains pays et usages non supportés.

### Rôles

| Composant | Rôle |
|-----------|------|
| **Mobile app** | Déclenche le besoin de 2FA (login) auprès du backend Django. |
| **Django backend** | Point d'entrée métier ; décide d'appeler le microservice OTP ou Twilio selon la réponse `channel`. |
| **otp-email-service** | Génère, envoie et vérifie les OTP par email ; gère rate limiting, rollout, routage pays, logs/métriques. |
| **Redis** | Stockage court-terme des OTP hashés, rate limits, compteurs de tentatives, états de vérification. |
| **SMTP Gmail existant** | Canal d'envoi d'emails transactionnels ; réutilisé sans création de nouvelles credentials. |
| **Twilio (backend)** | Canal SMS fallback pour pays à risque, usages interdits dans le service, ou panne email. |
| **Nginx + Let's Encrypt** | Reverse proxy, HTTPS, compression, rate limit global côté réseau. |

---

## Diagramme de flux complet

```
┌─────────────────┐         ┌──────────────────┐         ┌──────────────────────┐
│   Mobile App    │────────▶│  Django Backend  │────────▶│  otp-email-service   │
│  (React Native) │         │  (DRF / Twilio)  │         │   (FastAPI / Redis)  │
└─────────────────┘         └──────────────────┘         └──────────────────────┘
                                     │                              │
                                     │  POST /v1/otp/send           │
                                     │  + API key + HMAC(body+ts)   │
                                     │                              ▼
                                     │                     ┌──────────────────┐
                                     │                     │   FastAPI API    │
                                     │                     │  (auth, validate)│
                                     │                     └────────┬─────────┘
                                     │                              │
                                     │                              ▼
                                     │                     ┌──────────────────┐
                                     │                     │  Decision layer  │
                                     │                     │ purpose=login_2fa?
                                     │                     │ country_code risky?
                                     │                     │ rollout active?    
                                     │                     └────────┬─────────┘
                                     │                              │
                                     │            ┌─────────────────┼─────────────────┐
                                     │            ▼                 ▼                 ▼
                                     │    ┌───────────┐    ┌─────────────┐    ┌─────────────┐
                                     │    │  Refus    │    │ SMS fallback│    │ Email path  │
                                     │    │  Purpose  │    │  channel:   │    │  channel:   │
                                     │    │ interdit  │    │ sms_fallback│    │    email    │
                                     │    └───────────┘    └─────────────┘    └──────┬──────┘
                                     │            │                 │                │
                                     │            ▼                 ▼                ▼
                                     │     Retourne 400/403   Retourne             secrets.generate
                                     │                        sms_fallback         Redis SET hash+TTL
                                     │                        Django appelle       Envoi email (SMTP/...
                                     │                        Twilio directement   logging/métriques
                                     │
                                     │  Retour au mobile :
                                     │  {
                                     │    "channel": "email",
                                     │    "request_id": "req_...",
                                     │    "expires_in": 300
                                     │  }
```

---

## Flux détaillé `POST /v1/otp/send`

```
1. Django → microservice
   Headers : X-API-Key, X-HMAC-Signature, X-Timestamp
   Body    : {user_id, email, country_code, purpose, language}

2. Authentification service-à-service
   ├── Vérifier API key
   ├── Vérifier timestamp |now - ts| <= 300 s
   └── Recalculer HMAC-SHA256(body + timestamp, secret) et comparer

3. Validation métier
   ├── purpose == "login_2fa" ? sinon 403 "purpose not allowed"
   └── email valide ? sinon 422

4. Routage pays + rollout
   ├── country_code dans RISKY_COUNTRIES ? → channel = sms_fallback
   │                                    → ne pas générer ni stocker d'OTP
   └── sinon, hash(user_id) % 100 < ROLLOUT_PERCENTAGE ?
       ├── non → channel = sms_fallback (Django continue avec Twilio)
       └── oui → channel = email

5. Rate limiting (si channel = email)
   ├── user_id : max 3 envois / 10 min
   ├── IP source : max 3 envois / 10 min
   └── cooldown 60 s entre deux envois au même user_id

6. Génération & stockage
   ├── code = secrets.randbelow(1_000_000), formaté 6 chiffres
   ├── hash = sha256(code)
   ├── request_id = uuid4
   └── Redis :
       SET otp:{request_id}  {hash, user_id_hashed, attempts, verified, created_at}
       EXPIRE 300

7. Envoi email (provider abstrait)
   ├── Provider choisi par EMAIL_PROVIDER (gmail|brevo|resend)
   ├── Template FR ou EN selon language
   ├── Contenu HTML + texte brut
   └── Ne jamais inclure le code en clair dans les logs

8. Réponse
   { "channel": "email", "request_id": "req_...", "expires_in": 300 }
```

## Flux détaillé `POST /v1/otp/verify`

```
1. Django → microservice
   Body : {request_id, code}

2. Authentification service-à-service (même mécanisme)

3. Lecture Redis
   GET otp:{request_id}
   └── Si inexistant / expiré : retourne {verified: false, attempts_remaining: 0}

4. Vérification usage unique
   └── verified == true ? retourne {verified: false, attempts_remaining: 0}

5. Vérification tentatives
   └── attempts >= 5 ? retourne {verified: false, attempts_remaining: 0}

6. Comparaison sécurisée
   └── hmac.compare_digest(sha256(code), stored_hash)
       ├── OK  : verified = true, supprimer le code
       │         retourne {verified: true, attempts_remaining: 0}
       └── KO  : attempts += 1
                 retourne {verified: false, attempts_remaining: 5 - attempts}
```

---

## Schéma Redis

### Clés OTP

| Clé | Type | TTL | Contenu (hash / JSON) | Description |
|-----|------|-----|------------------------|-------------|
| `otp:{request_id}` | Hash | 300 s | `code_hash`, `user_id_hash`, `attempts`, `verified`, `created_at` | Code hashé, métadonnées, état de vérification. |
| `otp:send:count:user:{user_id_hash}` | String | 600 s | entier | Nombre d'envois pour un user sur 10 min. |
| `otp:send:count:ip:{ip_hash}` | String | 600 s | entier | Nombre d'envois pour une IP sur 10 min. |
| `otp:send:cooldown:user:{user_id_hash}` | String | 60 s | timestamp | Dernier envoi ; bloque les renvois trop rapides. |
| `otp:verify:attempts:{request_id}` | String | 300 s | entier | Tentatives de vérification échouées. |

### Exemple de contenu `otp:{request_id}`

```json
{
  "code_hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  "user_id_hash": "a8f5f167f44f4964e6c998dee827110c9a0c5e1e7a5b6e5f9d7c7e8f9a0b1c2d3",
  "attempts": 0,
  "verified": false,
  "created_at": "2026-10-07T12:34:56Z"
}
```

---

## Décisions architecturales clés

| Choix | Justification |
|-------|---------------|
| **FastAPI + Python 3.11+** | Aligné avec la stack Django existante ; async natif pour haute charge. |
| **Redis unique source de vérité** | TTL natifs, haute performance, pas de persistance OTP en base relationnelle. |
| **Codes stockés hashés** | En cas de dump Redis, les OTP ne sont pas exploitables. |
| **HMAC + timestamp** | Empêche rejeu et usurpation ; clé API complète le contrôle d'accès. |
| **Provider email abstrait** | Gmail aujourd'hui ; Brevo/Resend prêts sans refacto quand la volumétrie augmente. |
| **Feature flag déterministe** | `hash(user_id) % 100` garantit qu'un même utilisateur reste dans le même bucket si le pourcentage augmente. |
| **Pas de code en clair dans les logs** | Réduit le risque en cas de fuite de logs. |

---

## Sécurité : matrice des décisions

| Situation | Comportement | HTTP |
|-----------|--------------|------|
| Purpose interdit | Refus explicite | 403 |
| Pays à risque | `channel: sms_fallback` | 200 |
| Utilisateur hors rollout | `channel: sms_fallback` | 200 |
| Rate limit atteint | 429 avec retry-after | 429 |
| Cooldown actif | 429 avec retry-after | 429 |
| Code invalide / expiré / déjà utilisé | `verified: false`, message générique | 200 |
| Authentification manquante | 401 Unauthorized | 401 |
| HMAC invalide ou timestamp dépassé | 403 Forbidden | 403 |

---

## Documentation API & Developer Experience

L'API est conçue pour être **auto-documentée, testable et utilisable sans friction** par les équipes backend Django, mobile et DevOps.

### 1. Documentation interactive OpenAPI (Swagger / ReDoc)

FastAPI génère automatiquement une documentation OpenAPI 3.1 à partir des schémas Pydantic.

| Endpoint interactif | Usage |
|---------------------|-------|
| `GET /docs`         | Swagger UI : tester les endpoints, voir les modèles de requête/réponse. |
| `GET /redoc`        | ReDoc : documentation propre, idéale pour partage externe. |
| `GET /openapi.json` | Schéma brut pour génération de clients SDK. |

Tous les champs des requêtes/réponses sont **typés et validés** par Pydantic ; les descriptions et exemples sont intégrés directement dans le code.

### 2. Schémas de requête/réponse documentés

Exemple de ce que verra un consommateur dans Swagger :

**POST /v1/otp/send** — Requête
```json
{
  "user_id": "550e8400-e29b-41d4-a716-446655440000",
  "email": "user@example.com",
  "country_code": "FR",
  "purpose": "login_2fa",
  "language": "fr"
}
```

**POST /v1/otp/send** — Réponse
```json
{
  "channel": "email",
  "request_id": "req_7f8a9b2c",
  "expires_in": 300
}
```

**POST /v1/otp/verify** — Réponse
```json
{
  "verified": true,
  "attempts_remaining": 4
}
```

### 3. README professionnel à la racine

Le README inclura :
- Objectif du service et cas d'usage autorisés
- Stack technique et architecture rapide
- Installation locale (Docker Compose)
- Variables d'environnement obligatoires
- Authentification service-à-service (clé API + HMAC)
- Exemples d'appels avec `curl`
- Guide de contribution et lancement des tests
- Checklist de déploiement en production

### 4. Guide d'intégration Django

Un document dédié (`06-django-integration.md`) fournira le **code exact à copier-coller** dans le backend existant, incluant :
- Le client HTTP sécurisé (HMAC + timestamp)
- La logique de fallback Twilio
- La gestion des erreurs

### 5. Collection Postman / exemples curl

Un dossier `examples/` contiendra :
- `curl_send.sh` et `curl_verify.sh`
- Une collection Postman exportée (`otp-email-service.postman_collection.json`)
- Un script Python `client_example.py` montrant le calcul HMAC

### 6. Healthcheck documenté

`GET /health` retourne un JSON standardisé avec l'état Redis et la version du service, utile pour les sondes Docker, Nginx et monitoring.

```json
{
  "status": "ok",
  "redis": "ok",
  "version": "1.0.0"
}
```

---

## Rollout progressif

```
Phase 1 : ROLLOUT_PERCENTAGE=0
          → Tous les appels retournent sms_fallback, Django garde Twilio.
          → Valide la connectivité, l'auth, les logs.

Phase 2 : ROLLOUT_PERCENTAGE=5
          → 5 % des utilisateurs (déterministes par hash user_id) reçoivent l'email.
          → Mesure délivrabilité, taux de vérification, support client.

Phase 3 : ROLLOUT_PERCENTAGE=50 puis 100
          → Augmentation progressive sans redéploiement (variable d'env).

Les pays à risque restent en SMS quel que soit le rollout.
```

---

## Étape suivante

Livrable 2 : **Arborescence du projet** (`02-project-structure.md`).
