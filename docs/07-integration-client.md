# 7. Intégration client — otp-email-service

Document à remettre au client (ou à son développeur / agent IA) pour intégrer
l'API dans une application mobile ou web.

---

## 0. Vue d'ensemble

Cette API permet d'envoyer des **codes OTP par email** et des **emails
transactionnels** (bienvenue, notification, transaction, custom).

| Méthode | Endpoint | Rôle |
|---------|----------|------|
| `POST` | `/v1/otp/send` | Envoyer un code OTP à 6 chiffres par email |
| `POST` | `/v1/otp/verify` | Vérifier le code saisi par l'utilisateur |
| `POST` | `/v1/email/send` | Envoyer un email transactionnel |
| `GET` | `/health` | Vérifier que le service est en ligne |

**URL de base** : `https://<DOMAINE_DU_SERVICE>` (à remplacer par l'URL de
production fournie par l'admin — jamais `localhost`).

---

## 1. ⚠️ RÈGLE DE SÉCURITÉ — À LIRE AVANT TOUT

**Les clés ne doivent JAMAIS être embarquées dans l'application mobile.**
N'importe qui peut extraire une clé d'un APK/IPA et envoyer des emails à vos
frais.

```
App mobile ──► Backend / Cloud Function ──► otp-email-service
              (la signature HMAC se fait ICI)
```

Si le projet n'a pas de backend, utiliser une **Cloud Function**
(Firebase Cloud Functions, Supabase Edge Functions, Cloudflare Workers…)
de ~30 lignes qui reçoit la requête de l'app et la signe avant de la
transférer à l'API. C'est le seul endroit où vivent `api_key` et
`hmac_secret`.

---

## 2. Authentification : signature HMAC-SHA256

Chaque appel à `/v1/*` exige **3 headers** calculés à chaque requête :

| Header | Valeur |
|--------|--------|
| `X-API-Key` | La clé API du tenant (fournie une seule fois par l'admin) |
| `X-Timestamp` | Timestamp Unix en **secondes** (heure du téléphone à ±5 min max) |
| `X-HMAC-Signature` | `hex( HMAC_SHA256( corps_JSON_brut + timestamp , hmac_secret ) )` |

Détail important : la signature porte sur **le corps brut (bytes) suivi du
timestamp** — pas de séparateur entre les deux. Le corps envoyé doit être
exactement celui signé (mêmes espaces, même ordre des champs).

### Codes d'erreur d'authentification

| Code | Cause |
|------|-------|
| `401` | Headers manquants, clé inconnue ou tenant désactivé |
| `403` | Signature invalide, timestamp hors fenêtre ±300 s, purpose interdit |
| `429` | Rate limit (header `Retry-After` indique l'attente en secondes) |
| `422` | Corps JSON invalide (champ manquant, email malformé…) |

---

## 3. Exemples de signature par langage

### Python (backend)

```python
import hashlib, hmac, json, time, requests

BASE_URL    = "https://<DOMAINE>"
API_KEY     = "<api_key>"
HMAC_SECRET = "<hmac_secret>".encode()

def call(path: str, payload: dict) -> dict:
    body = json.dumps(payload).encode()
    ts   = str(int(time.time()))
    sig  = hmac.new(HMAC_SECRET, body + ts.encode(), hashlib.sha256).hexdigest()
    r = requests.post(BASE_URL + path, data=body, headers={
        "Content-Type": "application/json",
        "X-API-Key": API_KEY, "X-Timestamp": ts, "X-HMAC-Signature": sig,
    }, timeout=10)
    r.raise_for_status()
    return r.json()
```

### Node.js / Supabase Edge Function

```js
const crypto = require("crypto");

async function call(path, payload) {
  const body = JSON.stringify(payload);
  const ts   = Math.floor(Date.now() / 1000).toString();
  const sig  = crypto.createHmac("sha256", HMAC_SECRET)
                     .update(body + ts).digest("hex");
  const r = await fetch(BASE_URL + path, {
    method: "POST", body,
    headers: {
      "Content-Type": "application/json",
      "X-API-Key": API_KEY, "X-Timestamp": ts, "X-HMAC-Signature": sig,
    },
  });
  if (!r.ok) throw new Error(`API ${r.status}: ${await r.text()}`);
  return r.json();
}
```

### Dart / Flutter (si signature côté serveur impossible — déconseillé)

```dart
import 'dart:convert';
import 'package:crypto/crypto.dart';
import 'package:http/http.dart' as http;

Future<Map<String, dynamic>> callApi(String path, Map<String, dynamic> payload) async {
  final body = jsonEncode(payload);
  final ts   = (DateTime.now().millisecondsSinceEpoch ~/ 1000).toString();
  final sig  = Hmac(sha256, utf8.encode(hmacSecret))
      .convert(utf8.encode(body + ts)).toString();
  final r = await http.post(Uri.parse('$baseUrl$path'), body: body, headers: {
    'Content-Type': 'application/json',
    'X-API-Key': apiKey, 'X-Timestamp': ts, 'X-HMAC-Signature': sig,
  });
  return jsonDecode(r.body);
}
```

---

## 4. Envoyer un OTP — `POST /v1/otp/send`

```json
{
  "user_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "email": "client@exemple.com",
  "country_code": "FR",
  "purpose": "login_2fa",
  "language": "fr"
}
```

| Champ | Obligatoire | Contraintes |
|-------|-------------|-------------|
| `user_id` | oui | **UUID valide** (générer avec `uuid()` côté app) |
| `email` | oui | Email valide |
| `country_code` | oui | Code ISO 2-3 lettres : `FR`, `CI`, `US`… |
| `purpose` | oui | Doit être dans la liste autorisée du tenant |
| `language` | non | `fr` (défaut) ou `en` |

### Réponse — cas normal

```json
{ "channel": "email", "request_id": "req_XXXX", "expires_in": 300 }
```

➡️ Stocker `request_id` : il sert à la vérification.

### Réponse — cas `sms_fallback`

```json
{ "channel": "sms_fallback", "request_id": "", "expires_in": 0 }
```

➡️ L'API a refusé l'envoi email pour cet utilisateur (pays à risque ou
rollout). **Le client doit alors envoyer le code par son propre canal SMS**
(ex. Twilio) — l'API ne l'envoie pas elle-même.

---

## 5. Vérifier un OTP — `POST /v1/otp/verify`

```json
{ "request_id": "req_XXXX", "code": "123456" }
```

```json
{ "verified": true, "attempts_remaining": 0 }
```

- `verified: false` + `attempts_remaining > 0` → mauvais code, réessayer.
- `attempts_remaining: 0` → code bloqué ou expiré → renvoyer un OTP.
- Le code est **à usage unique** : une fois vérifié, il est supprimé.

---

## 6. Email transactionnel — `POST /v1/email/send`

```json
{
  "to_email": "client@exemple.com",
  "type": "welcome",
  "language": "fr",
  "data": { "name": "Awa" }
}
```

| `type` | Variables `data` |
|--------|------------------|
| `welcome` | `{name}` |
| `notification` | `{title, message}` |
| `transaction` | `{amount, currency, status}` |
| `custom` | `subject` + `text_body` obligatoires (hors `data`), `html_body` optionnel |

Réponse : `{ "sent": true, "message_id": "msg_...", "to_email": "..." }`

---

## 7. Checklist pour l'agent IA qui intègre

1. Demander à l'admin : `BASE_URL`, `api_key`, `hmac_secret`, liste des
   `allowed_purposes`.
2. Les clés vivent **côté serveur uniquement** (variables d'env de la
   Cloud Function / backend).
3. `user_id` = UUID généré par requête (ou UUID stable de l'utilisateur).
4. Gérer les 2 réponses de `/send` : `email` (attendre le code) et
   `sms_fallback` (déclencher le canal SMS du projet, ou afficher une erreur).
5. Gérer le `429` avec `Retry-After` : afficher « réessayez dans X s ».
6. Tester d'abord contre `/health` puis un envoi réel.
7. Endpoint testable sans signature en DEBUG : `/demo` (console web).

---

## 8. Limites à connaître

| Limite | Valeur tenant « illimité » | Où |
|--------|---------------------------|-----|
| Cooldown entre envois | `cooldown_seconds` (min 1) | config tenant |
| Envois / user / 10 min | `max_sends_per_user_per_10min` | config tenant |
| Envois / IP / 10 min | `max_sends_per_ip_per_10min` | config tenant |
| Tentatives de vérification | `max_verify_attempts` | config tenant |
| Validité du code | `otp_ttl_seconds` | config tenant |

Le vrai plafond reste le **provider SMTP** (Gmail ≈ 500 emails/jour compte
standard, 2000 Workspace ; Brevo/Resend selon plan). Pour du gros volume,
configurer un provider payant dans le tenant (`smtp_user`/`smtp_password`)
ou via `EMAIL_PROVIDER`.
