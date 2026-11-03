# Serveur mail maison — mail.aiforge.world

Guide complet pour héberger votre propre serveur SMTP (docker-mailserver)
sur votre VPS, avec le domaine géré chez IONOS.

> Remplacez `<IP_VPS>` par l'IP publique de votre VPS partout où elle apparaît.

---

## 1. Vérifier que le VPS autorise le port 25

Sur le VPS :

```bash
curl -v telnet://gmail-smtp-in.l.google.com:25
```

- `Connected` → OK, continuez.
- Timeout/refus → le provider bloque le port 25. Ouvrez un ticket chez eux
  pour le faire débloquer, sinon le projet est impossible (les mails
  sortants passent tous par le port 25 vers Gmail/Outlook/etc.).

Vérifier aussi que l'IP n'est pas déjà blacklistée :
https://mxtoolbox.com/SuperTool.aspx → `blacklist:<IP_VPS>`

## 2. DNS chez IONOS

IONOS → Domaines → aiforge.world → DNS → Ajouter :

| Type | Nom / Host       | Valeur                        | TTL  |
|------|------------------|-------------------------------|------|
| A    | `mail`           | `<IP_VPS>`                    | 3600 |
| MX   | `@`              | `10 mail.aiforge.world`       | 3600 |
| TXT  | `@`              | `v=spf1 a:mail.aiforge.world ~all` | 3600 |
| TXT  | `_dmarc`         | `v=DMARC1; p=quarantine; rua=mailto:postmaster@aiforge.world` | 3600 |

Attention : chez IONOS le MX s'écrit `10 mail.aiforge.world` (priorité + cible).
Si un enregistrement SPF `v=spf1` existe déjà, ne pas en créer un second —
fusionnez les deux en une seule entrée.

## 3. Reverse DNS (PTR)

Dans le panneau de votre **provider VPS** (pas IONOS, sauf si le VPS
est chez IONOS) : configurer le reverse DNS de `<IP_VPS>` → `mail.aiforge.world`.

- IONOS VPS : Serveurs → votre VPS → Adresse IP → Reverse DNS
- OVH : IP → Reverse DNS
- Vérification : `nslookup <IP_VPS>` doit répondre `mail.aiforge.world`

## 4. Certificat TLS

Sur le VPS :

```bash
apt update && apt install -y certbot
certbot certonly --standalone -d mail.aiforge.world
```

Renouvellement auto : certbot installe un timer systemd par défaut.
Ajoutez un hook pour recharger le conteneur :

```bash
cat > /etc/letsencrypt/renewal-hooks/deploy/mailserver.sh <<'EOF'
#!/bin/bash
docker compose -f /root/mailserver/docker-compose.yml restart mailserver
EOF
chmod +x /etc/letsencrypt/renewal-hooks/deploy/mailserver.sh
```

## 5. Déployer le serveur

```bash
mkdir -p /root/mailserver && cd /root/mailserver
# copier docker-compose.yml et mailserver.env de ce dossier
docker compose up -d
docker compose logs -f mailserver   # attendre "mailserver is up and running"
```

## 6. Créer la boîte d'envoi + DKIM

```bash
# Compte d'envoi pour le service OTP
docker exec -ti mailserver setup email add noreply@aiforge.world 'MOT_DE_PASSE_FORT'

# postmaster pour les rapports DMARC
docker exec -ti mailserver setup email add postmaster@aiforge.world 'MOT_DE_PASSE_FORT'

# Générer la clé DKIM
docker exec -ti mailserver setup config dkim
```

La dernière commande crée
`./docker-data/dms/config/opendkim/keys/aiforge.world/mail.txt`.

Ouvrez ce fichier, puis chez IONOS créez le TXT qu'il indique :

| Type | Nom            | Valeur                                  |
|------|----------------|-----------------------------------------|
| TXT  | `mail._domainkey` | `v=DKIM1; h=sha256; k=rsa; p=...`    |

## 7. Tests de déliverabilité

```bash
# Envoi d'un mail de test depuis le VPS
docker exec -ti mailserver setup email send noreply@aiforge.world test@mail-tester.com "test"
```

1. https://www.mail-tester.com → notez l'adresse, envoyez-lui un mail → viser **10/10**
2. Vérifier SPF/DKIM/DMARC : https://mxtoolbox.com/domain/aiforge.world
3. Créer un compte sur https://postmaster.google.com et vérifier `aiforge.world`
4. Envoyer un vrai mail vers une adresse Gmail/Outlook perso → vérifier
   qu'il arrive en boîte (pas spam) et que les en-têtes montrent
   `spf=pass dkim=pass dmarc=pass`

## 8. Brancher le service OTP

Dans le dashboard/admin du tenant (ou `.env` global) :

```
EMAIL_PROVIDER=smtp
SMTP_HOST=mail.aiforge.world
SMTP_PORT=587
SMTP_USER=noreply@aiforge.world
SMTP_PASSWORD=MOT_DE_PASSE_FORT
```

Puis bouton "Test email" dans le dashboard — le code existant passe
déjà par SMTP, rien à modifier.

## 9. Warm-up (obligatoire, 4-6 semaines)

Une IP neuve = réputation nulle. Pour ne pas finir en spam :

- Semaine 1 : 10-20 vrais mails/jour max
- Puis doubler le volume chaque semaine
- Ne jamais faire d'envoi massif d'un coup
- Surveiller Google Postmaster : la réputation doit monter à "Medium/High"

## Dépannage rapide

| Symptôme                              | Cause probable                     |
|---------------------------------------|-------------------------------------|
| Timeout sur port 25 sortant           | Provider bloque le port 25          |
| Rejet Gmail "PTR record"              | Reverse DNS absent/mauvais          |
| `dkim=fail`                           | TXT `mail._domainkey` mal copié     |
| Tout arrive en spam                   | IP neuve → warm-up ou blacklistée   |
| `relay access denied`                 | SMTP_USER/PASSWORD incorrects       |
