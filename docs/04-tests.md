# 4. Tests, lint et vérifications

## Tests unitaires

```bash
venv\Scripts\python -m pytest -q        # Windows
venv/bin/python -m pytest -q            # Linux/macOS
```

`tests/test_core.py` couvre : génération/hash OTP, modèles Pydantic,
tenants, helpers sécurité. État attendu : **11 passed**.

Les tests n'ont pas besoin de Redis externe ni de credentials — tout est
isolé (fakeredis / objets en mémoire).

## Lint

```bash
venv\Scripts\python -m ruff check src tests examples scripts
```

Ruff est configuré dans `pyproject.toml`. État attendu : 0 erreur.

## Diagnostic SMTP (hors application)

Teste les credentials **avant** de lancer le serveur — affiche le
dialogue SMTP complet et un verdict clair :

```bash
venv\Scripts\python scripts\test-smtp.py               # test vers SMTP_USER
venv\Scripts\python scripts\test-smtp.py autre@x.com   # vers un autre destinataire
```

| Verdict | Signification |
|---------|---------------|
| `Gmail a ACCEPTÉ le message` | Credentials OK, email dans la boîte |
| `AUTH REFUSÉE` | Mauvais mot de passe d'application / 2FA inactive |
| `DESTINATAIRE REFUSÉ` | Adresse cible rejetée par le serveur SMTP |

## Vérifications E2E manuelles (serveur lancé)

```bash
scripts\run-local.bat    # ou .sh
```

### 1. Healthcheck

```bash
curl http://localhost:8001/health
# {"status":"ok", ...}
```

### 2. Envoi OTP via les exemples (auth HMAC réelle)

```bash
venv\Scripts\python examples\send_otp.py test@example.com FR fr
# → {"channel":"email","request_id":"req_...","expires_in":300}
```

En mode console : le code apparaît dans les **logs du serveur**.
En mode réel (`.env` + Gmail) : le code arrive par email.

```bash
venv\Scripts\python examples\verify_otp.py req_xxxx 123456
# → {"verified":true,...}
```

### 3. Pages web

| URL | Attendu |
|-----|---------|
| `/` | 200 landing |
| `/demo` | 200 si `DEMO_ENABLED=true`, **404** sinon |
| `/login`, `/register` | 200 |
| `/admin` | Formulaire de login admin |
| `/docs` | Swagger UI |
| `/documentation` | Page doc |

### 4. Scénarios de non-régression à connaître

| Scénario | Comportement attendu |
|----------|----------------------|
| Rejouer la même requête signée HMAC | 403 « Replay detected » |
| Timestamp > ±300 s | 403 |
| 4e envoi au même user en 10 min | 429 + `Retry-After` |
| Échec provider → retry | 502 puis 200 (slot rate-limit rendu) |
| `country_code` = `FRA` ou `F` | 422 (alpha-2 strict, minuscules OK) |
| `Origin` hostile sur un POST web | 403 CSRF |
| `verified` après une vérif réussie | impossible (usage unique) |
| OTP expiré | `verified:false` — les tentatives n'allongent pas le TTL |

## Écrire un test

Modèle dans `tests/test_core.py` : pytest + fakeredis, pas d'I/O réseau.
Pour un nouveau comportement : écrire le test qui échoue → implémenter →
vérifier vert. Garder `pytest` + `ruff` verts avant chaque commit.

## Prochaine étape

`05-deployment.md` : mise en production.
