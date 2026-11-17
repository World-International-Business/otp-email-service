# Envoyer de vrais emails en local et en production

Ce guide couvre le passage du mode **simulation** (provider `console`) à
l'envoi **réel** d'emails via un compte connecté (Gmail SMTP, Brevo ou Resend),
ainsi que le dépannage des problèmes de livraison.

## Modèle : qui envoie, qui reçoit

```
COMPTE EXPÉDITEUR           EXPÉDITEUR AFFICHÉ        DESTINATAIRE
SMTP_USER +                 FROM_EMAIL                n'importe quelle adresse
SMTP_PASSWORD               FROM_NAME                 (champ Email / to_email)
(compte connecté)
```

- **Le compte qui envoie** est défini par `SMTP_USER` + `SMTP_PASSWORD`
  (variables d'environnement globales, ou par tenant dans `/dashboard/config`)
- **Le destinataire** est libre : n'importe quelle adresse reçoit l'OTP ou
  l'email transactionnel
- **Chaque tenant** peut connecter son propre compte email : si le tenant a
  des champs `smtp_*` configurés, ses envois passent par son compte au lieu
  du compte global

## Les deux modes de lancement local

`scripts/run-local.*` se comporte différemment selon la présence de `.env` :

| Cas | Comportement |
|-----|--------------|
| **Pas de `.env`** | Mode démo rapide : `EMAIL_PROVIDER=console` (emails dans les logs serveur), `USE_FAKEREDIS=true`, `DEBUG=true` (le code OTP s'affiche dans la réponse `/demo`), `DEMO_ENABLED=true` |
| **`.env` présent** | La config vient **entièrement** du fichier `.env`. Les variables héritées du shell (`EMAIL_PROVIDER`, `SMTP_*`, `DEBUG`…) sont purgées par le script pour ne pas écraser `.env` |

⚠️ **Piège fréquent** : les variables d'environnement du shell sont
prioritaires sur `.env`. Si `sent: true` arrive en ~5 ms sans email reçu,
le provider `console` est actif — le script `run-local` purge maintenant
ces variables, mais un lancement manuel (`uvicorn` direct) dans un
terminal ayant des `set`/`$env:` antérieurs reproduira le problème.

## Configuration Gmail SMTP (pas à pas)

### Étape 1 — Validation en deux étapes (obligatoire)

`myaccount.google.com` → **Sécurité** → **Validation en deux étapes** → activer.
Sans 2FA, Google ne propose pas de mot de passe d'application.

### Étape 2 — Mot de passe d'application

Toujours dans **Sécurité**, soit :

- taper « **mots de passe des applications** » dans la barre de recherche
  du compte Google, soit
- ouvrir **Validation en deux étapes** → tout en bas → **Mots de passe des
  applications**, soit
- aller directement sur `myaccount.google.com/apppasswords`

Donner un nom à l'app (ex. `otp-email-service`) → Google affiche
**16 caractères** (`xxxx xxxx xxxx xxxx`) **une seule fois**.

⚠️ Ce n'est **pas** le mot de passe normal du compte. Avec le mot de passe
normal, l'authentification SMTP est refusée (`535`).

### Étape 3 — `.env`

```ini
EMAIL_PROVIDER=gmail
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=mon.adresse@gmail.com       # le compte qui envoie
SMTP_PASSWORD=abcdefghijklmnop        # les 16 caractères (espaces acceptés)
FROM_EMAIL=mon.adresse@gmail.com      # doit correspondre au compte (ou alias vérifié)
FROM_NAME=MonApp
USE_FAKEREDIS=true
DEBUG=false                           # debug_code non affiché -> code par mail uniquement
DEMO_ENABLED=true                     # /demo accessible en local
```

### Étape 4 — Vérifier les credentials hors application

```bash
venv\Scripts\python scripts\test-smtp.py              # envoie un test à soi-même
venv\Scripts\python scripts\test-smtp.py autre@x.com  # vers un autre destinataire
```

Le script affiche le **dialogue SMTP complet** et un verdict clair :

- `Gmail a ACCEPTÉ le message` → credentials valides
- `AUTH REFUSÉE` → mot de passe d'application faux ou 2FA inactive
- `DESTINATAIRE REFUSÉ` → adresse cible rejetée par le serveur

### Étape 5 — Lancer et tester

```bash
scripts\run-local.bat   # doit afficher « config chargée depuis .env »
```

Un vrai envoi SMTP prend **1 à 3 secondes** — c'est normal. Les envois
quasi instantanés (~5 ms) signifient que le provider console est actif.

### Étape 6 — Vérifier la livraison

`sent: true` signifie « le serveur SMTP a **accepté** le message », pas
« livré ». En cas de non-réception :

1. **« Messages envoyés »** du compte expéditeur — si l'email y figure,
   Google l'a transmis ; le problème est côté destinataire
2. **Spam / Indésirables** et onglet **Promotions** du destinataire —
   les premiers envois d'un compte app-password y atterrissent souvent ;
   marquer « Pas spam »
3. **Alerte de sécurité Google** sur le compte expéditeur — la première
   utilisation d'un app password depuis une nouvelle machine peut être
   bloquée ; vérifier les notifications d'« activité inhabituelle »
4. **Bounce** — un message « Delivery Status Notification (Failure) »
   dans la boîte de l'expéditeur indique un refus définitif
5. Limite Gmail : **~500 emails/jour** — au-delà, envois bloqués

## Adresses professionnelles (domaine personnalisé)

Le provider `gmail` est en réalité un **SMTP générique** — il fonctionne
avec n'importe quel serveur, y compris les boîtes pro sur domaine
personnalisé. `EMAIL_PROVIDER=smtp` est un alias équivalent.

### Configuration d'une adresse pro

```
SMTP_HOST     = le serveur SMTP de l'hébergeur
SMTP_PORT     = 587 (STARTTLS) ou 465 (SSL — détecté automatiquement)
SMTP_USER     = l'adresse pro complète (ex. contact@mondomaine.com)
SMTP_PASSWORD = le mot de passe de la boîte (ou app password si Workspace)
FROM_EMAIL    = la même adresse pro
```

| Hébergeur | SMTP_HOST | Port |
|-----------|-----------|------|
| Google Workspace | `smtp.gmail.com` | 587 |
| IONOS | `smtp.ionos.com` | 587 |
| OVH | `ssl0.ovh.net` | 465 |
| Zoho | `smtp.zoho.com` | 465 |
| cPanel / hébergeur mutualisé | `mail.mondomaine.com` | 465 ou 587 |
| Auto-hébergé | voir `mailserver/` | 587 |

La connexion **SSL implicite est détectée automatiquement quand le port
est 465** ; `SMTP_USE_SSL=true/false` permet de forcer si besoin. Chaque
tenant peut aussi régler host/port/user/password dans `/dashboard/config`
ou via l'API admin.

### Délivrabilité avec un domaine pro

Pour que les emails signés `contact@mondomaine.com` n'atterrissent pas en
spam, ajouter dans le DNS du domaine :

- **SPF** : `v=spf1 include:spf.mxhébergeur ~all` (autorise le serveur)
- **DKIM** : clé fournie par l'hébergeur mail
- **DMARC** : `v=DMARC1; p=quarantine` minimum

⚠️ Envoyer en `contact@mondomaine.com` **via smtp.gmail.com d'un compte
perso** ne fonctionne pas : Google réécrit le `From` sur l'adresse du
compte (sauf alias « Envoyer en tant que » vérifié dans Gmail).

## Autres providers

| Provider | Variables | Notes |
|----------|-----------|-------|
| `console` | — | Simulation : emails dans les logs serveur (dev uniquement) |
| `gmail` / `smtp` | `SMTP_*` | SMTP générique : Gmail perso, Workspace, adresses pro (OVH, Zoho, IONOS, cPanel, auto-hébergé). SSL auto sur 465 |
| `brevo` | `BREVO_API_KEY` | API REST, 300 emails/jour gratuit, expéditeur à vérifier |
| `resend` | `RESEND_API_KEY` | API REST, tier gratuit limité à l'adresse du compte sans domaine vérifié |

Sans clé configurée, le provider correspondant refuse de démarrer
(`ValueError` au boot) — fail fast plutôt qu'échec à l'envoi.

## Production

- `EMAIL_PROVIDER=gmail` convient au faible volume ; pour du volume réel,
  privilégier Brevo/Resend ou un SMTP dédié (le projet inclut `mailserver/`)
- `DEBUG=false` et `DEMO_ENABLED=false` **obligatoires**
- `PUBLIC_BASE_URL=https://...` active HSTS et le flag `Secure` des cookies
- Les erreurs SMTP remontent dans les logs JSON : chercher
  `"message": "smtp_send_error"` avec `error` détaillé

## Troubleshooting — récapitulatif

| Symptôme | Cause probable | Action |
|----------|----------------|--------|
| `sent:true` ~5 ms, rien reçu | Provider `console` actif | Vérifier `EMAIL_PROVIDER` effectif ; relancer via `run-local` |
| `502` + `Connection unexpectedly closed` | Auth refusée / placeholder | Remplir `SMTP_PASSWORD` (app password) |
| `smtp_send_error ... 535` | Mot de passe normal ou 2FA absente | 2FA + régénérer l'app password |
| `sent:true` ~2 s, rien reçu | Spam, retard, ou blocage silencieux Google | « Messages envoyés » + Spam + alerte sécurité du compte |
| Emails reçus mais code expiré | `OTP_TTL_SECONDS` trop court | Augmenter dans `.env` (défaut 300 s) |
