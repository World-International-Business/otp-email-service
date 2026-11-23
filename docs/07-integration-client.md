# 7. Intégration client — otp-email-service (référence complète)

Document à remettre au développeur (ou à l'agent IA) qui intègre l'API dans
une application web, mobile ou backend. Tout ce qu'il faut savoir, étape par
étape, avec le détail exact des requêtes et des réponses.

---

## 0. Vue d'ensemble

| Méthode | Endpoint | Rôle | Auth |
|---------|----------|------|------|
| `POST` | `/v1/otp/send` | Envoyer un code OTP à 6 chiffres par email | HMAC |
| `POST` | `/v1/otp/verify` | Vérifier le code saisi par l'utilisateur | HMAC |
| `GET` | `/v1/otp/confirm` | Lien magique de vérification (clic dans l'email) | aucune (le lien est le secret) |
| `POST` | `/v1/email/send` | Envoyer un email transactionnel | HMAC |
| `GET` | `/health` | Healthcheck (`status`, `redis`, `version`) | aucune |
| `POST` | `/v1/admin/tenants` | Créer un tenant (clé + secret affichés une fois) | `X-Admin-Key` |
| `GET` | `/v1/admin/tenants` | Lister les tenants | `X-Admin-Key` |
| `GET/PATCH/DELETE` | `/v1/admin/tenants/{id}` | Gérer un tenant | `X-Admin-Key` |

**URL de base** : `https://<DOMAINE_DU_SERVICE>` (jamais `localhost` en
production). En local : `http://localhost:8001`.

**Cycle de vie d'un OTP** :

```
App ──POST /v1/otp/send──► API ──► code 6 chiffres envoyé par email
App ◄── {request_id} ────  API
Utilisateur saisit le code dans l'app
App ──POST /v1/otp/verify──► API ◄── {verified: true/false}
```

Le code est à **usage unique** (supprimé après vérification réussie ou épuisement
des tentatives), valide `otp_ttl_seconds` secondes (défaut : 300 s / 5 min).

---

## 1. Obtenir ses credentials

Deux façons d'obtenir `api_key` + `hmac_secret` :

| Voie | Comment |
|------|---------|
| **Self-service** | Créer un compte sur `/register` → le dashboard affiche les credentials du tenant associé |
| **Par l'admin** | `POST /v1/admin/tenants` avec `X-Admin-Key`, ou le panneau `/admin` — la réponse contient `api_key` et `hmac_secret`, **affichés une seule fois** |

⚠️ `hmac_secret` n'est **jamais** renvoyé par l'API après création. Le perdre =
régénérer les credentials.

---

## 2. ⚠️ RÈGLE DE SÉCURITÉ — À LIRE AVANT TOUT

**Les clés ne vivent jamais dans l'application mobile ou le front-end.**
N'importe qui peut extraire une clé d'un APK/IPA/bundle JS et envoyer des
emails à vos frais.

```
App mobile / front ──► Backend / Cloud Function ──► otp-email-service
                        (la signature HMAC se fait ICI,
                         api_key + hmac_secret en variables d'env)
```

Sans backend, utiliser une **Cloud Function** (~30 lignes : Firebase,
Supabase Edge, Cloudflare Workers…) qui reçoit la requête de l'app et la
signe avant de la transférer.

---

## 3. Authentification — signature HMAC-SHA256

Chaque appel à `/v1/otp/*` et `/v1/email/*` exige **3 headers** (+1 recommandé) :

| Header | Valeur |
|--------|--------|
| `X-API-Key` | La clé API du tenant |
| `X-Timestamp` | Timestamp Unix en **secondes** (pas millisecondes) |
| `X-HMAC-Signature` | `hex(HMAC_SHA256(secret, corps_JSON_brut + timestamp [+ "|" + nonce]))` |
| `X-Nonce` *(optionnel, recommandé)* | Valeur unique par requête (uuid4) — voir note |

> **Pourquoi X-Nonce ?** Sans lui, deux requêtes au **corps identique** dans la
> **même seconde** produisent la même signature → la seconde est rejetée
> `403 "Replay detected"`. Cas typique : polling de `GET /v1/email/status/{id}`.
> Avec un nonce unique par requête, chaque signature est distincte. Rétro-
> compatible : les clients sans X-Nonce continuent de fonctionner.

### Recette exacte

```text
1. body     = les bytes EXACTS du JSON envoyé (avant tout réencodage)
2. ts       = str(int(time.time()))                     # secondes
3. nonce    = uuid.uuid4().hex                          # unique par requête
4. message  = body + ts.encode() + b"|" + nonce.encode()  # concaténation
5. sig      = hmac_sha256(secret, message).hexdigest()  # 64 caractères hex
6. headers  = X-API-Key, X-Timestamp: ts, X-Nonce: nonce,
              X-HMAC-Signature: sig, Content-Type: application/json
```

### Idempotence — `Idempotency-Key`

Les `POST /v1/otp/send` et `/v1/email/send` acceptent un header
`Idempotency-Key: <clé unique>` : le premier appel est exécuté, sa réponse
stockée 24 h ; tout appel suivant avec la même clé **rejoue la réponse
d'origine** (même request_id, même message_id) — un retry réseau ne crée
jamais un second envoi ni un nouvel OTP.

### Statut d'envoi — `GET /v1/email/status/{message_id}`

Réponse de `POST /v1/email/send` : `status="queued"` — la livraison est
asynchrone avec retries (60 s, 5 min, 15 min puis dead). Interrogez ce
endpoint pour connaître l'état : `queued | retry | sent | dead`, plus
`attempts`, `error`, `updated`. Scopé au tenant émetteur (404 sinon).

### Webhooks sortants — statuts en push

Si `webhook_url` est configurée (dashboard ou admin), MailHub POST à votre
endpoint, signé par `X-MailHub-Signature: sha256=<HMAC(body, hmac_secret)>` :

| `X-MailHub-Event` | Données (`data`) |
|---|---|
| `email.sent` | message_id, to, type, subject |
| `email.failed` | message_id, to, type, error, attempts, will_retry |
| `email.dead` | message_id, to, type, error, attempts (plus de retry) |
| `otp.sent` | request_id, to, purpose, expires_in |
| `otp.failed` | request_id, to, purpose, error |
| `otp.verified` | request_id |

3 tentatives (immédiat, +60 s, +5 min). Répondez 2xx pour acquitter.

### Ce que le serveur vérifie (dans l'ordre)

| Étape | Échec → |
|-------|---------|
| Les 3 headers présents | `401 "Not authenticated"` |
| `X-API-Key` correspond à un tenant actif | `401 "Unauthorized"` |
| `X-Timestamp` est un entier | `403 "Invalid timestamp"` |
| `|ts_serveur − ts_client| ≤ 300 s` | `403 "Timestamp too old"` |
| Signature correcte | `403 "Invalid signature"` |
| Signature jamais utilisée (nonce Redis, TTL 310 s) | `403 "Replay detected"` |

### Pièges qui coûtent des heures

- **Signer les bytes exacts envoyés.** Si la librairie HTTP re-sérialise le
  JSON (ordre des clés, espaces, Unicode), la signature ne correspond plus.
  Signer la chaîne finale puis l'envoyer telle quelle.
- **Timestamp en secondes**, pas millisecondes → sinon `Timestamp too old`.
- **Horloge synchronisée** (NTP) : ±5 min de tolérance seulement.
- **Une signature = un usage.** Même corps + même timestamp re-soumis dans les
  5 min → `Replay detected`. Toujours régénérer ts + signature.
- Le timestamp signé est la **chaîne** envoyée dans le header (`"1731234567"`),
  pas un entier reformaté.

---

## 4. Envoyer un OTP — `POST /v1/otp/send`

### Corps

```json
{
  "user_id": "550e8400-e29b-41d4-a716-446655440000",
  "email": "client@exemple.com",
  "country_code": "FR",
  "purpose": "login_2fa",
  "language": "fr",
  "magic_link": true
}
```

| Champ | Type | Obligatoire | Contraintes |
|-------|------|-------------|-------------|
| `user_id` | string | oui | **UUID valide** (`550e8400-…`), sinon `422` |
| `email` | string | oui | Adresse email valide — **n'importe quel destinataire** |
| `country_code` | string | oui | ISO 3166-1 alpha-2 : `FR`, `CI`, `US`… (2 lettres) |
| `purpose` | string | oui | Doit être dans `allowed_purposes` du tenant, sinon `403` |
| `language` | string | non | `fr` ou `en` — **défaut `en`** |
| `magic_link` | bool | non | `true` → l'email contient un bouton « vérifier en un clic » |

### Réponse — envoi email

```json
{ "channel": "email", "request_id": "req_XXXXXXXXXXXXXXXX", "expires_in": 300 }
```

➡️ **Stocker `request_id`** — indispensable pour `/v1/otp/verify`.
`debug_code` apparaît en plus **uniquement si `DEBUG=true`** (jamais en prod).

### Réponse — `sms_fallback`

```json
{ "channel": "sms_fallback", "request_id": "", "expires_in": 0 }
```

➡️ L'API a **refusé** l'envoi email (pays dans `risky_countries` du tenant ou
hors rollout). Le client déclenche alors **son propre canal SMS** (ex. Twilio) —
l'API ne l'envoie pas.

### Erreurs spécifiques

| Code | `detail` | Cause |
|------|----------|-------|
| `403` | purpose non autorisé | `purpose` absent de `allowed_purposes` du tenant |
| `422` | erreurs de validation | `user_id` non-UUID, email malformé, `country_code` ≠ 2 lettres |
| `429` | `Too many OTP requests` | Quota dépassé — lire le header **`Retry-After`** (secondes) |
| `502` | `Failed to send email: <raison>` | Provider SMTP en échec (voir §8) |

---

## 5. Vérifier un OTP — `POST /v1/otp/verify`

```json
{ "request_id": "req_XXXXXXXXXXXXXXXX", "code": "123456" }
```

| Champ | Contraintes |
|-------|-------------|
| `request_id` | Exactement celui retourné par `/send` — et **même tenant/clé API** |
| `code` | Chaîne de **6 chiffres** (autre format → `422` sans même vérifier) |

### Réponses

```json
{ "verified": true,  "attempts_remaining": 0 }   // succès — code consommé
{ "verified": false, "attempts_remaining": 2 }   // mauvais code, réessayer
{ "verified": false, "attempts_remaining": 0 }   // bloqué / expiré / déjà utilisé
```

Règles : max `max_verify_attempts` essais (défaut 5) · usage unique · un OTP
n'est vérifiable que par le tenant qui l'a émis.

---

## 6. Lien magique — `GET /v1/otp/confirm`

Généré dans l'email quand `magic_link: true`. **Non signé** : connaître le lien
= connaître le code.

```
GET /v1/otp/confirm?request_id=req_...&code=123456&t=<tenant_id>
```

Retourne une page HTML « ✓ Code vérifié » ou « Lien invalide ou expiré ».
Consomme le code exactement comme `/v1/otp/verify` — ensuite la requête
`verify` renverra `verified: false` (normal : déjà utilisé).

---

## 7. Email transactionnel — `POST /v1/email/send`

```json
{
  "to_email": "client@exemple.com",
  "type": "notification",
  "language": "fr",
  "data": { "title": "Commande expédiée", "message": "Arrivée sous 48 h." }
}
```

| Champ | Obligatoire | Notes |
|-------|-------------|-------|
| `to_email` | oui | Destinataire — n'importe quelle adresse valide |
| `type` | oui | `welcome` · `notification` · `transaction` · `custom` |
| `language` | non | `fr` / `en`, défaut `en` |
| `data` | non | Variables du template (voir ci-dessous) |
| `subject` | **si `custom`** | Sujet libre |
| `text_body` | **si `custom`** | Corps texte libre |
| `html_body` | non (`custom`) | Corps HTML libre |

| `type` | Variables `data` |
|--------|------------------|
| `welcome` | `{name}` |
| `notification` | `{title, message}` |
| `transaction` | `{amount, currency, status}` |
| `custom` | `subject` + `text_body` en champs racine (obligatoires), `html_body` optionnel |

Réponse :

```json
{ "sent": true, "message_id": "msg_...", "to_email": "client@exemple.com" }
```

⚠️ `sent: true` = « accepté par le serveur SMTP », **pas** « livré ».
Échec provider → `502 "Failed to send email: <raison exacte>"` (`535 auth
refusée`, `connexion fermée`, `550 expéditeur non autorisé`…).

---

## 8. Expéditeur — quel compte envoie vos emails

Le **destinataire** est toujours l'adresse de la requête. L'**expéditeur** se
résout en cascade à chaque envoi :

| Priorité | Source | Quand |
|----------|--------|-------|
| 1 | Champs `smtp_*` / `from_*` **du tenant** | Configurés dans `/dashboard` ou via l'admin |
| 2 | Variables **globales** `.env` (`SMTP_*`, `FROM_*`) | Fallback |

**Connecter sa propre boîte** (dans `/dashboard` ou via `PATCH
/v1/admin/tenants/{id}`) :

| Champ tenant | Rôle |
|--------------|------|
| `smtp_host` | Serveur SMTP de **l'hébergeur de la boîte** — ex. `smtp.gmail.com`, `smtp.ionos.fr`, `ssl0.ovh.net`, `smtp.zoho.com` |
| `smtp_port` | `587` = STARTTLS · `465` = SSL implicite (auto-détecté) |
| `smtp_user` | Compte qui envoie — adresse pro acceptée |
| `smtp_password` | Mot de passe de la boîte / app password. **Vide à l'update = conserve l'ancien** |
| `smtp_use_ssl` | `true`/`false` pour forcer — vide = auto par port |
| `from_email` / `from_name` / `brand_name` | Expéditeur affiché + branding |

Règles critiques :

- `smtp_user` décide **qui envoie vraiment**. Un `from_email` étranger au
  compte SMTP → rejet (`550 Sender address is not allowed`) ou réécriture
  (Gmail force le `From` sur le compte authentifié, sauf alias vérifié).
- `smtp.gmail.com` n'accepte **que les comptes Google** (Gmail / Workspace +
  app password). Une boîte IONOS/OVH/Zoho va sur **son** serveur.
- `sent: true` ≠ livré : vérifier « Messages envoyés » de l'expéditeur, Spam
  du destinataire, limites d'envoi (Gmail ≈ 500/j). Guide : `docs/08`.

---

## 9. Exemples complets par langage

### Python — client complet

```python
import hashlib, hmac, json, time, uuid, requests

BASE_URL    = "https://<DOMAINE>"
API_KEY     = "<api_key>"
HMAC_SECRET = "<hmac_secret>".encode()

def call(path: str, payload: dict) -> dict:
    body = json.dumps(payload).encode()          # bytes exacts signés
    ts   = str(int(time.time()))
    sig  = hmac.new(HMAC_SECRET, body + ts.encode(), hashlib.sha256).hexdigest()
    r = requests.post(BASE_URL + path, data=body, timeout=15, headers={
        "Content-Type": "application/json",
        "X-API-Key": API_KEY, "X-Timestamp": ts, "X-HMAC-Signature": sig,
    })
    if r.status_code == 429:
        time.sleep(int(r.headers.get("Retry-After", 60)))
        return call(path, payload)               # retry simple
    r.raise_for_status()
    return r.json()

# --- flux complet ---
resp = call("/v1/otp/send", {
    "user_id": str(uuid.uuid4()), "email": "client@exemple.com",
    "country_code": "FR", "purpose": "login_2fa", "language": "fr",
})
if resp["channel"] == "sms_fallback":
    ...  # déclencher votre canal SMS (Twilio…)
else:
    request_id = resp["request_id"]               # à stocker
    code = input("Code reçu par email : ")
    print(call("/v1/otp/verify", {"request_id": request_id, "code": code}))
```

### Node.js — aucune dépendance

```js
import crypto from "node:crypto";

const BASE_URL = "https://<DOMAINE>";
const API_KEY = "<api_key>", HMAC_SECRET = "<hmac_secret>";

async function call(path, payload) {
  const body = JSON.stringify(payload);
  const ts = String(Math.floor(Date.now() / 1000));
  const sig = crypto.createHmac("sha256", HMAC_SECRET)
                    .update(body + ts).digest("hex");
  const r = await fetch(BASE_URL + path, {
    method: "POST", body,
    headers: { "Content-Type": "application/json",
               "X-API-Key": API_KEY, "X-Timestamp": ts, "X-HMAC-Signature": sig },
  });
  if (!r.ok) throw new Error(`API ${r.status}: ${await r.text()}`);
  return r.json();
}
```

### PHP

```php
$body = json_encode(["request_id" => "req_...", "code" => "123456"]);
$ts   = (string) time();
$sig  = hash_hmac("sha256", $body . $ts, $HMAC_SECRET);
$ch   = curl_init("$BASE_URL/v1/otp/verify");
curl_setopt_array($ch, [
  CURLOPT_RETURNTRANSFER => true, CURLOPT_POST => true,
  CURLOPT_POSTFIELDS => $body,
  CURLOPT_HTTPHEADER => ["Content-Type: application/json",
    "X-API-Key: $API_KEY", "X-Timestamp: $ts", "X-HMAC-Signature: $sig"],
]);
$r = json_decode(curl_exec($ch), true); curl_close($ch);
```

### cURL / Bash

```bash
BODY='{"request_id":"req_...","code":"123456"}'
TS=$(date +%s)
SIG=$(printf '%s' "${BODY}${TS}" | openssl dgst -sha256 -hmac "$HMAC_SECRET" | cut -d' ' -f2)
curl -X POST $BASE_URL/v1/otp/verify \
  -H "Content-Type: application/json" -H "X-API-Key: $API_KEY" \
  -H "X-Timestamp: $TS" -H "X-HMAC-Signature: $SIG" -d "$BODY"
```

### Dart / Flutter (dernier recours — voir §2)

```dart
final body = jsonEncode(payload);
final ts = (DateTime.now().millisecondsSinceEpoch ~/ 1000).toString();
final sig = Hmac(sha256, utf8.encode(hmacSecret))
    .convert(utf8.encode(body + ts)).toString();
await http.post(Uri.parse('$baseUrl$path'), body: body, headers: {
  'Content-Type': 'application/json',
  'X-API-Key': apiKey, 'X-Timestamp': ts, 'X-HMAC-Signature': sig,
});
```

---

## 10. Tableau complet des erreurs

| HTTP | `detail` | Action |
|------|----------|--------|
| `401` | `Not authenticated` | Ajouter les 3 headers |
| `401` | `Unauthorized` | `X-API-Key` inconnu ou tenant désactivé |
| `403` | `Invalid timestamp` / `Timestamp too old` | ts en secondes + horloge à l'heure |
| `403` | `Invalid signature` | Signer les **bytes exacts** envoyés |
| `403` | `Replay detected` | Nouveau ts + nouvelle signature à chaque requête |
| `403` | purpose | `purpose` non présent dans `allowed_purposes` |
| `422` | validation Pydantic | Champ manquant/format (`user_id` UUID, `code` 6 chiffres) |
| `429` | `Too many OTP requests` | Attendre `Retry-After` s — quota tenant dépassé |
| `502` | `Failed to send email: <raison>` | Config SMTP du tenant/global : host, port, user, password, `from_email` autorisé |

## 11. Limites & quotas

| Limite | Champ tenant | Défaut |
|--------|--------------|--------|
| Envois OTP / user / 10 min | `max_sends_per_user_per_10min` | 3 |
| Envois OTP / IP / 10 min | `max_sends_per_ip_per_10min` | 3 |
| Cooldown entre envois | `cooldown_seconds` | 60 |
| Tentatives de vérification | `max_verify_attempts` | 5 |
| Validité du code | `otp_ttl_seconds` | 300 s |

Toutes modifiables par l'admin (`PATCH /v1/admin/tenants/{id}`). Le vrai
plafond reste le provider SMTP (Gmail ≈ 500/j, Workspace 2000/j,
Brevo/Resend selon plan).

## 12. Checklist intégrateur

1. `BASE_URL` + `api_key` + `hmac_secret` + `allowed_purposes` en main.
2. Clés **côté serveur uniquement** (env vars de la Cloud Function).
3. Tester `GET /health` → `{ "status": "ok", "redis": "ok" }`.
4. Signer les bytes exacts ; ts en secondes ; horloge NTP.
5. `user_id` = UUID ; `code` transmis en **string** 6 chiffres.
6. Gérer les 2 branches de `/send` : `email` et `sms_fallback`.
7. Gérer `429` via `Retry-After`, `502` en remontant l'erreur email.
8. `/v1/otp/verify` avec **la même clé API** que `/send`.
9. Configurer l'expéditeur du tenant (`/dashboard` ou admin) + « Tester
   l'envoi » avant de brancher la prod.
10. `DEMO_ENABLED=false` côté serveur en production.
