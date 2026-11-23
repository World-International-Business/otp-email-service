"""Page de documentation intégrée au site (/documentation)."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from src.web.i18n import I18nRoute
from src.web.routes import UI_ICONS, _current_user, _landing_page

router = APIRouter(tags=["web"], include_in_schema=False, route_class=I18nRoute)


def _code_block(code: str) -> str:
    return f"""<div class="bg-[#0a0b14] rounded-xl overflow-hidden">
<div class="flex items-center justify-between px-4 py-2.5 border-b border-white/10">
  <div class="flex gap-1.5"><span class="w-2.5 h-2.5 rounded-full bg-rose-400"></span><span class="w-2.5 h-2.5 rounded-full bg-amber-400"></span><span class="w-2.5 h-2.5 rounded-full bg-emerald-400"></span></div>
  <button onclick="copyText(this.closest('div').nextElementSibling.textContent)" class="text-[11px] font-semibold text-slate-400 hover:text-white transition">Copier</button>
</div>
<pre class="p-5 text-[13px] leading-relaxed font-mono text-slate-300 overflow-auto">{code}</pre>
</div>"""


def _table(headers: list[str], rows: list[list[str]], min_w: int = 440) -> str:
    th = "".join(
        f'<th class="py-2.5 {"pr-4" if i < len(headers) - 1 else ""}">{h}</th>'
        for i, h in enumerate(headers)
    )
    body = ""
    for row in rows:
        tds = "".join(
            f'<td class="py-3 {"pr-4" if i < len(row) - 1 else ""} text-slate-600">{c}</td>'
            for i, c in enumerate(row)
        )
        body += f"<tr>{tds}</tr>"
    return f"""<div class="overflow-x-auto"><table class="w-full text-sm min-w-[{min_w}px]">
  <thead><tr class="text-left text-xs uppercase tracking-wider text-slate-400 border-b">{th}</tr></thead>
  <tbody class="divide-y divide-slate-100">{body}</tbody>
</table></div>"""


def _card(anchor: str, title: str, inner: str) -> str:
    return f"""<div id="{anchor}" class="card p-5 sm:p-7">
  <h2 class="font-bold text-xl mb-2">{title}</h2>
  {inner}
</div>"""


@router.get("/documentation", response_class=HTMLResponse)
async def documentation(request: Request) -> HTMLResponse:
    base = str(request.base_url).rstrip("/")

    python_code = f"""# pip install requests
import hashlib, hmac, json, time, uuid, requests

BASE_URL    = "{base}"
API_KEY     = "VOTRE_CLE_API"
HMAC_SECRET = b"VOTRE_SECRET_HMAC"

def call(path: str, payload: dict) -> dict:
    body = json.dumps(payload).encode()           # bytes EXACTS signes
    ts   = str(int(time.time()))                  # secondes
    sig  = hmac.new(HMAC_SECRET, body + ts.encode(), hashlib.sha256).hexdigest()
    r = requests.post(BASE_URL + path, data=body, timeout=15, headers={{
        "Content-Type": "application/json",
        "X-API-Key": API_KEY, "X-Timestamp": ts, "X-HMAC-Signature": sig,
    }})
    r.raise_for_status()
    return r.json()

# 1. Envoyer un OTP
resp = call("/v1/otp/send", {{
    "user_id": str(uuid.uuid4()), "email": "client@exemple.com",
    "country_code": "FR", "purpose": "login_2fa", "language": "fr",
}})
request_id = resp["request_id"]                   # a stocker pour /verify

# 2. Verifier le code saisi par l'utilisateur
print(call("/v1/otp/verify", {{"request_id": request_id, "code": "123456"}}))"""

    node_code = f"""// Node 18+ — aucune dependance (fetch et crypto natifs)
import crypto from "node:crypto";

const BASE_URL = "{base}";
const API_KEY = "VOTRE_CLE_API";
const HMAC_SECRET = "VOTRE_SECRET_HMAC";

async function call(path, payload) {{
  const body = JSON.stringify(payload);
  const ts = String(Math.floor(Date.now() / 1000));
  const sig = crypto.createHmac("sha256", HMAC_SECRET)
                    .update(body + ts).digest("hex");
  const r = await fetch(BASE_URL + path, {{
    method: "POST", body,
    headers: {{ "Content-Type": "application/json",
               "X-API-Key": API_KEY, "X-Timestamp": ts, "X-HMAC-Signature": sig }},
  }});
  if (!r.ok) throw new Error(`API ${{r.status}}: ${{await r.text()}}`);
  return r.json();
}}

const resp = await call("/v1/otp/send", {{
  user_id: crypto.randomUUID(), email: "client@exemple.com",
  country_code: "FR", purpose: "login_2fa", language: "fr",
}});
console.log(resp);"""

    curl_code = f"""BODY='{{"request_id":"req_...","code":"123456"}}'
TS=$(date +%s)
SIG=$(printf '%s' "${{BODY}}${{TS}}" | openssl dgst -sha256 -hmac "VOTRE_SECRET_HMAC" | cut -d' ' -f2)

curl -X POST {base}/v1/otp/verify \\
  -H "Content-Type: application/json" \\
  -H "X-API-Key: VOTRE_CLE_API" \\
  -H "X-Timestamp: $TS" \\
  -H "X-HMAC-Signature: $SIG" \\
  -d "$BODY\""""

    php_code = f"""<?php
$BASE = "{base}";
$KEY  = "VOTRE_CLE_API";
$SEC  = "VOTRE_SECRET_HMAC";

$body = json_encode(["user_id" => "UUID_UTILISATEUR",
                     "email" => "client@exemple.com",
                     "country_code" => "FR",
                     "purpose" => "login_2fa",
                     "language" => "fr"]);
$ts  = (string) time();
$sig = hash_hmac("sha256", $body . $ts, $SEC);

$ch = curl_init($BASE . "/v1/otp/send");
curl_setopt_array($ch, [
  CURLOPT_RETURNTRANSFER => true, CURLOPT_POST => true,
  CURLOPT_POSTFIELDS => $body,
  CURLOPT_HTTPHEADER => ["Content-Type: application/json",
    "X-API-Key: $KEY", "X-Timestamp: $ts", "X-HMAC-Signature: $sig"],
]);
$response = json_decode(curl_exec($ch), true);
curl_close($ch);""".replace("<", "&lt;").replace(">", "&gt;")

    sign_recipe = """1. body     = les bytes EXACTS du JSON envoye (pas de re-serialisation)
2. ts       = str(int(time.time()))                    # secondes
3. message  = body + ts.encode()                       # SANS separateur
4. sig      = hex( HMAC_SHA256(secret, message) )      # 64 caracteres hex
5. headers  = X-API-Key + X-Timestamp: ts + X-HMAC-Signature: sig"""

    otp_body = '{\n  "user_id": "550e8400-e29b-41d4-a716-446655440000",\n  "email": "client@exemple.com",\n  "country_code": "FR",\n  "purpose": "login_2fa",\n  "language": "fr",\n  "magic_link": true\n}'
    otp_resp = '{\n  "channel": "email",\n  "request_id": "req_XXXXXXXXXXXXXXXX",\n  "expires_in": 300\n}'
    fallback_resp = '{\n  "channel": "sms_fallback",\n  "request_id": "",\n  "expires_in": 0\n}\n\n# L\'API refuse l\'envoi (pays a risque / rollout) —\n# a vous de declencher votre canal SMS (Twilio...)'
    verify_body = '{\n  "request_id": "req_XXXXXXXXXXXXXXXX",\n  "code": "123456"\n}'
    verify_resp = '{ "verified": true,  "attempts_remaining": 0 }   // succes\n{ "verified": false, "attempts_remaining": 2 }   // mauvais code\n{ "verified": false, "attempts_remaining": 0 }   // bloque / expire'
    email_body = '{\n  "to_email": "client@exemple.com",\n  "type": "notification",\n  "language": "fr",\n  "data": { "title": "Commande expédiée", "message": "Arrivée sous 48 h." }\n}'
    custom_body = (
        '{\n  "to_email": "client@exemple.com",\n  "type": "custom",\n'
        '  "subject": "Confirmez votre compte",\n'
        '  "text_body": "Cliquez pour activer votre compte : https://app.exemple.com/confirm?token=abc",\n'
        '  "html_body": "<p>Activez votre compte :</p><p><a href=\\"https://app.exemple.com/confirm?token=abc\\" '
        'style=\\"background:#5060a8;color:#fff;padding:12px 24px;border-radius:8px;text-decoration:none\\">'
        'Confirmer mon compte</a></p>"\n}'
    )
    confirm_url = base + "/v1/otp/confirm?request_id=req_...&code=123456&t=<tenant_id>"

    card_credentials = _card("doc-credentials", "1. Obtenir vos credentials", f"""
        <p class="text-sm text-slate-500 mb-4">Deux voies pour obtenir <code>api_key</code> +
        <code>hmac_secret</code> :</p>
        {_table(["Voie", "Comment"], [
            ["<strong>Self-service</strong>", "Créez un compte sur <code>/register</code> — le dashboard affiche les credentials de votre tenant"],
            ["<strong>Par l'admin</strong>", "<code>POST /v1/admin/tenants</code> (header <code>X-Admin-Key</code>) ou le panneau <code>/admin</code> — la réponse contient la clé et le secret"],
        ])}
        <p class="text-xs text-slate-400 mt-3">{UI_ICONS['warn']} <code>hmac_secret</code> n'est affiché qu'une seule fois — conservez-le en lieu sûr.</p>
      """)

    card_auth = _card("doc-auth", "2. Authentification — signature HMAC-SHA256", f"""
        <p class="text-sm text-slate-500 mb-4">Chaque requête <code>/v1/*</code> exige 3 headers,
        recalculés à chaque appel :</p>
        {_table(["Header", "Valeur"], [
            ["<code>X-API-Key</code>", "Votre clé API tenant"],
            ["<code>X-Timestamp</code>", "Timestamp Unix en <strong>secondes</strong>"],
            ["<code>X-HMAC-Signature</code>", "<code>hex(HMAC_SHA256(secret, body + timestamp))</code>"],
        ])}
        <p class="text-xs font-bold text-slate-400 uppercase tracking-wider mt-5 mb-3">Recette exacte</p>
        {_code_block(sign_recipe)}
        <p class="text-sm text-slate-500 mt-4 mb-3">Vérifications côté serveur, dans l'ordre :</p>
        {_table(["Vérification", "Échec"], [
            ["3 headers présents", "<code>401 Not authenticated</code>"],
            ["Clé = tenant actif", "<code>401 Unauthorized</code>"],
            ["Timestamp entier, ± 5 min", "<code>403 Invalid timestamp / Timestamp too old</code>"],
            ["Signature correcte", "<code>403 Invalid signature</code>"],
            ["Signature jamais utilisée (anti-rejeu)", "<code>403 Replay detected</code>"],
        ])}
        <p class="text-xs text-slate-400 mt-3">Pièges : signez les <strong>bytes exacts</strong> envoyés
        (mêmes espaces, même ordre des champs) · timestamp en secondes · horloge synchronisée NTP ·
        une signature = un seul usage.</p>
      """)

    card_otp = _card("doc-otp", "3. Envoyer un OTP", f"""
        <p class="text-sm text-slate-500 mb-4"><code>POST /v1/otp/send</code> — envoie un code à
        6 chiffres par email (usage unique, TTL 5 min par défaut).</p>
        {_code_block(otp_body)}
        <div class="mt-4">{_table(["Champ", "Contraintes"], [
            ["<code>user_id</code>", "<strong>UUID valide</strong> — sinon 422"],
            ["<code>email</code>", "N'importe quelle adresse valide — c'est le destinataire"],
            ["<code>country_code</code>", "ISO alpha-2 : <code>FR</code>, <code>CI</code>, <code>US</code>…"],
            ["<code>purpose</code>", "Doit être dans <code>allowed_purposes</code> du tenant — sinon 403"],
            ["<code>language</code>", "<code>fr</code> / <code>en</code> — défaut <code>en</code>"],
            ["<code>magic_link</code>", "<code>true</code> → l'email contient un bouton de vérif en un clic"],
        ])}</div>
        <p class="text-xs font-bold text-slate-400 uppercase tracking-wider mt-5 mb-3">Réponse — email envoyé</p>
        {_code_block(otp_resp)}
        <p class="text-xs font-bold text-slate-400 uppercase tracking-wider mt-5 mb-3">Réponse — sms_fallback</p>
        {_code_block(fallback_resp)}
      """)

    card_verify = _card("doc-verify", "4. Vérifier un code", f"""
        <p class="text-sm text-slate-500 mb-4"><code>POST /v1/otp/verify</code> — avec la
        <strong>même clé API</strong> que le send. <code>code</code> = string de 6 chiffres
        (autre format → 422). Max 5 tentatives, usage unique.</p>
        {_code_block(verify_body)}
        <p class="text-xs font-bold text-slate-400 uppercase tracking-wider mt-4 mb-3">Réponses</p>
        {_code_block(verify_resp)}
      """)

    card_confirm = _card("doc-confirm", "5. Lien magique (optionnel)", f"""
        <p class="text-sm text-slate-500 mb-4">Quand <code>magic_link: true</code>, l'email contient un
        lien <strong>non signé</strong> — le lien lui-même est le secret :</p>
        {_code_block(confirm_url)}
        <p class="text-sm text-slate-500 mt-4">Le clic affiche « ✓ Code vérifié » et consomme le code —
        un <code>/v1/otp/verify</code> ultérieur renverra <code>verified: false</code> (déjà utilisé).</p>
      """)

    card_email = _card("doc-email", "6. Emails — tout type de contenu", f"""
        <p class="text-sm text-slate-500 mb-4"><code>POST /v1/email/send</code> — même auth HMAC.
        L'API n'envoie pas que des OTP : <strong>notifications, liens de confirmation ou de
        réinitialisation, reçus, factures, alertes, contenu HTML libre</strong> — tout passe
        par ce seul endpoint.</p>
        {_table(["Cas d'usage", "type", "Exemple"], [
            ["Bienvenue / onboarding", "<code>welcome</code>", "« Votre compte est prêt »"],
            ["Notification produit", "<code>notification</code>", "Commande expédiée, alerte sécurité"],
            ["Reçu / transaction", "<code>transaction</code>", "Montant + devise + statut"],
            ["<strong>Tout le reste</strong> — lien de vérif, reset MDP, facture, marketing", "<code>custom</code>", "Sujet + texte + HTML libres"],
        ])}
        <p class="text-xs font-bold text-slate-400 uppercase tracking-wider mt-5 mb-3">Corps de requête</p>
        {_code_block(email_body)}
        <div class="mt-4">{_table(["type", "Champs"], [
            ["<code>welcome</code>", "<code>data.name</code> (prénom affiché)"],
            ["<code>notification</code>", "<code>data.title</code> + <code>data.message</code>"],
            ["<code>transaction</code>", "<code>data.amount</code>, <code>data.currency</code>, <code>data.status</code>"],
            ["<code>custom</code>", "<code>subject</code> + <code>text_body</code> obligatoires · <code>html_body</code> optionnel"],
        ])}</div>
        <p class="text-xs font-bold text-slate-400 uppercase tracking-wider mt-5 mb-3">Exemple custom — lien de confirmation avec bouton</p>
        {_code_block(custom_body)}
        <p class="text-xs text-slate-400 mt-3">Branding automatique : sans <code>html_body</code>,
        le texte est mis en page avec votre branding (couleur, logo, pied de page)
        et <code>subject</code> devient le titre de l'email. Avec un
        <strong>fragment</strong> HTML (sans balise <code>&lt;html&gt;</code>), le fragment
        est enrobé du même branding <em>sans titre injecté</em> — incluez votre propre
        <code>&lt;h1&gt;</code> dans le fragment. Un document HTML complet
        (<code>&lt;html&gt;…&lt;/html&gt;</code>) est envoyé tel quel — utilisez alors du CSS
        <em>inline</em> et des liens en <code>https://</code> pour la compatibilité
        des clients mail.</p>
        <p class="text-sm text-slate-500 mt-4">Réponse : <code>{{"sent": true, "message_id": "msg_...", "to_email": "..."}}</code>
        — <code>sent</code> = accepté par le SMTP, pas « livré ».</p>
      """)

    card_sender = _card("doc-sender", "7. Expéditeur — quel compte envoie", f"""
        <p class="text-sm text-slate-500 mb-4">Le destinataire est l'adresse de la requête.
        L'expéditeur se résout en cascade : <strong>config SMTP du tenant</strong>
        (<code>/dashboard</code> ou admin) → <strong>variables globales</strong> du serveur.</p>
        {_table(["Champ tenant", "Rôle"], [
            ["<code>smtp_host</code> / <code>smtp_port</code>", "Serveur de l'hébergeur — <code>587</code> STARTTLS, <code>465</code> SSL auto"],
            ["<code>smtp_user</code> / <code>smtp_password</code>", "Compte qui envoie (adresse pro OK) — password vide = conserve l'ancien"],
            ["<code>from_email</code> / <code>from_name</code> / <code>brand_name</code>", "Expéditeur affiché + branding"],
        ])}
        <p class="text-xs text-slate-400 mt-3">Règle : <code>smtp_user</code> décide qui envoie vraiment —
        un <code>from_email</code> étranger au compte SMTP est rejeté ou réécrit. Bouton
        « Tester l'envoi » dans le dashboard pour valider avant de brancher.</p>
      """)

    card_sdks = _card("doc-sdks", "8. Exemples par langage", f"""
        <p class="text-xs font-bold text-slate-400 uppercase tracking-wider mb-3">Python — client complet</p>
        {_code_block(python_code)}
        <p class="text-xs font-bold text-slate-400 uppercase tracking-wider mt-6 mb-3">Node.js</p>
        {_code_block(node_code)}
        <p class="text-xs font-bold text-slate-400 uppercase tracking-wider mt-6 mb-3">cURL / Bash</p>
        {_code_block(curl_code)}
        <p class="text-xs font-bold text-slate-400 uppercase tracking-wider mt-6 mb-3">PHP</p>
        {_code_block(php_code)}
      """)

    card_errors = _card("doc-errors", "9. Erreurs &amp; limites", f"""
        <p class="text-xs font-bold text-slate-400 uppercase tracking-wider mb-3">Codes HTTP</p>
        {_table(["Code", "Signification"], [
            ["<code>401</code>", "Headers manquants (<code>Not authenticated</code>) ou clé inconnue / tenant désactivé (<code>Unauthorized</code>)"],
            ["<code>403</code>", "Signature invalide · timestamp hors ±5 min · replay détecté · purpose non autorisé"],
            ["<code>422</code>", "Payload invalide — <code>user_id</code> non-UUID, <code>code</code> ≠ 6 chiffres, email malformé"],
            ["<code>429</code>", "Quota dépassé — header <code>Retry-After</code> = secondes d'attente"],
            ["<code>502</code>", "<code>Failed to send email: &lt;raison&gt;</code> — config SMTP à corriger"],
        ])}
        <p class="text-xs font-bold text-slate-400 uppercase tracking-wider mt-6 mb-3">Quotas par défaut (modifiables par tenant)</p>
        {_table(["Limite", "Défaut"], [
            ["Envois OTP / utilisateur / 10 min", "3"],
            ["Envois OTP / IP / 10 min", "3"],
            ["Cooldown entre deux envois", "60 s"],
            ["Tentatives de vérification", "5"],
            ["Validité du code", "300 s"],
        ])}
        <p class="text-sm text-slate-500 mt-4">Référence complète : <code>docs/07-integration-client.md</code>
        dans le dépôt. Swagger interactif : <a href="/docs" class="text-brand-600 font-semibold hover:underline">/docs</a>.</p>
      """)

    content = f"""
<div class="bg-gradient-to-b from-slate-50 to-white">
<div class="max-w-6xl mx-auto px-4 sm:px-6 py-8 sm:py-14">
  <div class="grid lg:grid-cols-[230px_1fr] gap-10">

    <aside class="hidden lg:block">
      <div class="sticky top-24 space-y-1 text-sm">
        <p class="text-xs font-bold text-slate-400 uppercase tracking-wider mb-3">Sommaire</p>
        <a href="#doc-intro" class="block py-1.5 text-slate-600 hover:text-brand-600 font-medium">Introduction</a>
        <a href="#doc-credentials" class="block py-1.5 text-slate-600 hover:text-brand-600 font-medium">1. Credentials</a>
        <a href="#doc-auth" class="block py-1.5 text-slate-600 hover:text-brand-600 font-medium">2. Authentification</a>
        <a href="#doc-otp" class="block py-1.5 text-slate-600 hover:text-brand-600 font-medium">3. Envoyer un OTP</a>
        <a href="#doc-verify" class="block py-1.5 text-slate-600 hover:text-brand-600 font-medium">4. Vérifier un code</a>
        <a href="#doc-confirm" class="block py-1.5 text-slate-600 hover:text-brand-600 font-medium">5. Lien magique</a>
        <a href="#doc-email" class="block py-1.5 text-slate-600 hover:text-brand-600 font-medium">6. Emails transactionnels</a>
        <a href="#doc-sender" class="block py-1.5 text-slate-600 hover:text-brand-600 font-medium">7. Expéditeur</a>
        <a href="#doc-sdks" class="block py-1.5 text-slate-600 hover:text-brand-600 font-medium">8. Exemples par langage</a>
        <a href="#doc-errors" class="block py-1.5 text-slate-600 hover:text-brand-600 font-medium">9. Erreurs &amp; limites</a>
      </div>
    </aside>

    <div class="space-y-10 min-w-0">
      <div id="doc-intro">
        <span class="badge bg-brand-50 text-brand-700 border border-brand-100">DOCUMENTATION</span>
        <h1 class="text-3xl md:text-4xl font-extrabold mt-4">Intégrer MailHub</h1>
        <p class="text-slate-500 mt-3 leading-relaxed">Une API REST pour vos codes OTP et emails
        transactionnels. Toutes les requêtes <code>/v1/*</code> sont signées
        <strong>HMAC-SHA256</strong>. Le code OTP part par email — vos utilisateurs le
        lisent dans leur boîte et le saisissent chez vous.</p>
        <div class="mt-5 card p-4 flex flex-wrap items-center gap-3 text-sm">
          <span class="text-slate-500 font-medium">URL de base :</span>
          <code class="!text-sm">{base}</code>
        </div>
        <div class="mt-4 rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
          <strong>Règle de sécurité :</strong> <code>api_key</code> et <code>hmac_secret</code> ne vivent
          jamais dans une app mobile ou un front-end — la signature se fait côté serveur
          ou Cloud Function.
        </div>
      </div>

            {card_credentials}

      {card_auth}

      {card_otp}

      {card_verify}

      {card_confirm}

      {card_email}

      {card_sender}

      {card_sdks}

      {card_errors}


    </div>
  </div>
</div>
</div>
"""
    return _landing_page(content, logged_in=bool(await _current_user(request)))
