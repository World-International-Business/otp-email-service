"""Page de documentation intégrée au site (/documentation)."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from src.config import Settings, get_settings
from src.web.routes import _landing_page

router = APIRouter(tags=["web"], include_in_schema=False)


def _code_block(code: str) -> str:
    return f"""<div class="bg-slate-950 rounded-xl overflow-hidden">
<div class="flex items-center justify-between px-4 py-2.5 border-b border-white/10">
  <div class="flex gap-1.5"><span class="w-2.5 h-2.5 rounded-full bg-rose-400"></span><span class="w-2.5 h-2.5 rounded-full bg-amber-400"></span><span class="w-2.5 h-2.5 rounded-full bg-emerald-400"></span></div>
  <button onclick="navigator.clipboard.writeText(this.closest('div').nextElementSibling.textContent)" class="text-[11px] font-semibold text-slate-400 hover:text-white transition">Copier</button>
</div>
<pre class="p-5 text-[13px] leading-relaxed font-mono text-slate-300 overflow-auto">{code}</pre>
</div>"""


@router.get("/documentation", response_class=HTMLResponse)
async def documentation(
    request: Request, settings: Settings = Depends(get_settings)
) -> HTMLResponse:
    base = str(request.base_url).rstrip("/")

    python_code = f"""# pip install requests
import hashlib, hmac, json, time, requests

BASE_URL    = "{base}"
API_KEY     = "VOTRE_CLE_API"
HMAC_SECRET = b"VOTRE_SECRET_HMAC"

def sign(body: bytes) -> dict:
    ts  = str(int(time.time()))
    sig = hmac.new(HMAC_SECRET, body + ts.encode(), hashlib.sha256).hexdigest()
    return {{"Content-Type": "application/json", "X-API-Key": API_KEY,
            "X-Timestamp": ts, "X-HMAC-Signature": sig}}

# 1. Envoyer un OTP
body = json.dumps({{"user_id": "UUID_UTILISATEUR", "email": "client@exemple.com",
                   "country_code": "FR", "purpose": "login_2fa", "language": "fr"}}).encode()
r = requests.post(BASE_URL + "/v1/otp/send", data=body, headers=sign(body))
data = r.json()   # {{"channel":"email","request_id":"req_...","expires_in":300}}

# 2. Verifier le code saisi par l'utilisateur
body = json.dumps({{"request_id": data["request_id"], "code": "123456"}}).encode()
r = requests.post(BASE_URL + "/v1/otp/verify", data=body, headers=sign(body))
print(r.json())   # {{"verified": true}}"""

    node_code = f"""// Node 18+ — aucune dépendance (fetch et crypto natifs)
import crypto from "node:crypto";

const BASE_URL = "{base}";
const API_KEY = "VOTRE_CLE_API";
const HMAC_SECRET = "VOTRE_SECRET_HMAC";

function sign(body) {{
  const ts = String(Math.floor(Date.now() / 1000));
  const sig = crypto.createHmac("sha256", HMAC_SECRET)
                    .update(body + ts).digest("hex");
  return {{ "Content-Type": "application/json", "X-API-Key": API_KEY,
           "X-Timestamp": ts, "X-HMAC-Signature": sig }};
}}

const body = JSON.stringify({{
  user_id: "UUID_UTILISATEUR", email: "client@exemple.com",
  country_code: "FR", purpose: "login_2fa", language: "fr"
}});
const res = await fetch(BASE_URL + "/v1/otp/send", {{
  method: "POST", headers: sign(body), body
}});
console.log(await res.json());"""

    curl_code = f"""BODY='{{"request_id":"req_...","code":"123456"}}'
TS=$(date +%s)
SIG=$(printf '%s' "${{BODY}}${{TS}}" | openssl dgst -sha256 -hmac "VOTRE_SECRET_HMAC" | cut -d' ' -f2)

curl -X POST {base}/v1/otp/verify \\
  -H "Content-Type: application/json" \\
  -H "X-API-Key: VOTRE_CLE_API" \\
  -H "X-Timestamp: $TS" \\
  -H "X-HMAC-Signature: $SIG" \\
  -d "$BODY\""""

    otp_body = '{\n  "user_id": "550e8400-e29b-41d4-a716-446655440000",\n  "email": "client@exemple.com",\n  "country_code": "FR",\n  "purpose": "login_2fa",\n  "language": "fr"\n}'
    verify_body = '{\n  "request_id": "req_xxxxxxxx",\n  "code": "123456"\n}\n\n# Réponse\n{ "verified": true, "attempts_remaining": 0 }'
    email_body = '{\n  "to_email": "client@exemple.com",\n  "type": "notification",\n  "language": "fr",\n  "data": { "title": "Commande expédiée", "message": "Arrivée prévue sous 48h." }\n}'

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

    content = f"""
<div class="bg-gradient-to-b from-slate-50 to-white">
<div class="max-w-6xl mx-auto px-6 py-14">
  <div class="grid lg:grid-cols-[220px_1fr] gap-10">

    <aside class="hidden lg:block">
      <div class="sticky top-24 space-y-1 text-sm">
        <p class="text-xs font-bold text-slate-400 uppercase tracking-wider mb-3">Sommaire</p>
        <a href="#doc-intro" class="block py-1.5 text-slate-600 hover:text-brand-600 font-medium">Introduction</a>
        <a href="#doc-auth" class="block py-1.5 text-slate-600 hover:text-brand-600 font-medium">Authentification</a>
        <a href="#doc-otp" class="block py-1.5 text-slate-600 hover:text-brand-600 font-medium">Envoyer un OTP</a>
        <a href="#doc-verify" class="block py-1.5 text-slate-600 hover:text-brand-600 font-medium">Vérifier un code</a>
        <a href="#doc-email" class="block py-1.5 text-slate-600 hover:text-brand-600 font-medium">Emails</a>
        <a href="#doc-sdks" class="block py-1.5 text-slate-600 hover:text-brand-600 font-medium">Exemples par langage</a>
        <a href="#doc-errors" class="block py-1.5 text-slate-600 hover:text-brand-600 font-medium">Erreurs &amp; limites</a>
      </div>
    </aside>

    <div class="space-y-10 min-w-0">
      <div id="doc-intro">
        <span class="badge bg-brand-50 text-brand-700 border border-brand-100">DOCUMENTATION</span>
        <h1 class="text-3xl md:text-4xl font-extrabold mt-4">Intégrer MailHub</h1>
        <p class="text-slate-500 mt-3 leading-relaxed">Une API REST unique pour vos codes OTP et emails
        transactionnels. Toutes les requêtes sont signées <strong>HMAC-SHA256</strong>.
        Référence interactive complète : <a href="/docs" class="text-indigo-600 font-semibold">Swagger UI</a>.</p>
        <div class="mt-5 card p-4 flex flex-wrap items-center gap-3 text-sm">
          <span class="text-slate-500 font-medium">URL de base :</span>
          <code class="!text-sm">{base}</code>
        </div>
      </div>

      <div id="doc-auth" class="card p-7">
        <h2 class="font-bold text-xl mb-2">1. Authentification</h2>
        <p class="text-sm text-slate-500 mb-4">Chaque requête <code>/v1/*</code> exige 3 headers.
        La signature couvre <code>body + timestamp</code> — rejetée au-delà de 5 minutes.</p>
        <table class="w-full text-sm">
          <thead><tr class="text-left text-xs uppercase tracking-wider text-slate-400 border-b">
            <th class="py-2.5 pr-4">Header</th><th class="py-2.5">Description</th></tr></thead>
          <tbody class="divide-y divide-slate-100">
            <tr><td class="py-3 pr-4"><code>X-API-Key</code></td><td class="py-3 text-slate-600">Votre clé API tenant</td></tr>
            <tr><td class="py-3 pr-4"><code>X-Timestamp</code></td><td class="py-3 text-slate-600">Timestamp Unix (secondes)</td></tr>
            <tr><td class="py-3 pr-4"><code>X-HMAC-Signature</code></td><td class="py-3 text-slate-600"><code>hex(HMAC_SHA256(secret, body + timestamp))</code></td></tr>
          </tbody>
        </table>
      </div>

      <div id="doc-otp" class="card p-7">
        <h2 class="font-bold text-xl mb-2">2. Envoyer un OTP</h2>
        <p class="text-sm text-slate-500 mb-4"><code>POST /v1/otp/send</code> — envoie un code à 6 chiffres par email.
        Pays à risque → <code>channel: "sms_fallback"</code>.</p>
        {_code_block(otp_body)}
      </div>

      <div id="doc-verify" class="card p-7">
        <h2 class="font-bold text-xl mb-2">3. Vérifier un code</h2>
        <p class="text-sm text-slate-500 mb-4"><code>POST /v1/otp/verify</code> — usage unique, 5 tentatives max, TTL 5 min.</p>
        {_code_block(verify_body)}
      </div>

      <div id="doc-email" class="card p-7">
        <h2 class="font-bold text-xl mb-2">4. Emails transactionnels</h2>
        <p class="text-sm text-slate-500 mb-4"><code>POST /v1/email/send</code> — types :
        <code>welcome</code>, <code>notification</code>, <code>transaction</code>, <code>custom</code>.</p>
        {_code_block(email_body)}
      </div>

      <div id="doc-sdks" class="card p-7">
        <h2 class="font-bold text-xl mb-5">5. Exemples par langage</h2>
        <p class="text-xs font-bold text-slate-400 uppercase tracking-wider mb-3">Python</p>
        {_code_block(python_code)}
        <p class="text-xs font-bold text-slate-400 uppercase tracking-wider mt-6 mb-3">Node.js</p>
        {_code_block(node_code)}
        <p class="text-xs font-bold text-slate-400 uppercase tracking-wider mt-6 mb-3">cURL / Bash</p>
        {_code_block(curl_code)}
        <p class="text-xs font-bold text-slate-400 uppercase tracking-wider mt-6 mb-3">PHP</p>
        {_code_block(php_code)}
      </div>

      <div id="doc-errors" class="card p-7">
        <h2 class="font-bold text-xl mb-4">6. Erreurs &amp; limites</h2>
        <table class="w-full text-sm">
          <thead><tr class="text-left text-xs uppercase tracking-wider text-slate-400 border-b">
            <th class="py-2.5 pr-4">Code</th><th class="py-2.5">Signification</th></tr></thead>
          <tbody class="divide-y divide-slate-100 text-slate-600">
            <tr><td class="py-3 pr-4"><code>401</code></td><td class="py-3">Headers d'auth manquants ou clé invalide</td></tr>
            <tr><td class="py-3 pr-4"><code>403</code></td><td class="py-3">Signature HMAC invalide, timestamp expiré ou purpose non autorisé</td></tr>
            <tr><td class="py-3 pr-4"><code>429</code></td><td class="py-3">Rate limit : 3 envois / 10 min par utilisateur et par IP</td></tr>
            <tr><td class="py-3 pr-4"><code>422</code></td><td class="py-3">Payload invalide (user_id doit être un UUID)</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</div>
</div>
"""
    return _landing_page(content, debug=settings.debug)
