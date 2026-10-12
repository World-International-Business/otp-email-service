"""Interface web de la plateforme — landing, auth, dashboard, démo."""

import logging
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, EmailStr

from src.api.dependencies import get_email_service, get_otp_service
from src.config import Settings, get_settings
from src.core.models import EmailSendRequest, EmailType, Language
from src.core.tenants import (
    Tenant,
    TenantStore,
    generate_api_key,
    generate_hmac_secret,
    hash_api_key,
)
from src.services.email_service import EmailService
from src.services.otp_service import OTPService
from src.web.users import SessionStore, WebUserStore

router = APIRouter(tags=["web"], include_in_schema=False)
logger = logging.getLogger(__name__)


# =====================================================================
# Templates HTML — design moderne (Tailwind)
# =====================================================================

LOGO_SVG = """<svg class="w-9 h-9" viewBox="0 0 40 40" fill="none">
<rect width="40" height="40" rx="10" fill="url(#g)"/>
<path d="M10 14.5l10 7 10-7" stroke="#fff" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>
<path d="M11 13.5h18a1.5 1.5 0 011.5 1.5v10a1.5 1.5 0 01-1.5 1.5H11a1.5 1.5 0 01-1.5-1.5V15a1.5 1.5 0 011.5-1.5z" stroke="#fff" stroke-width="2.4"/>
<defs><linearGradient id="g" x1="0" y1="0" x2="40" y2="40"><stop stop-color="#4f7cff"/><stop offset="1" stop-color="#00c6a2"/></linearGradient></defs>
</svg>"""


def _head(title: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} — MailHub</title>
<script src="https://cdn.tailwindcss.com"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap" rel="stylesheet">
<script>
tailwind.config = {{ theme: {{ extend: {{ fontFamily: {{ sans: ['Inter','sans-serif'] }},
  colors: {{ brand: {{50:'#eef4ff',100:'#dce8ff',500:'#3b6ef6',600:'#2d5ad9',700:'#2447b8'}} }} }} }} }};
</script>
<style>
  body {{ font-family: 'Inter', sans-serif; -webkit-font-smoothing: antialiased; }}
  .hero-bg {{ background: radial-gradient(ellipse 80% 60% at 50% -10%, rgba(59,110,246,.35), transparent),
              linear-gradient(180deg, #0b1226 0%, #111a3a 100%); }}
  .grid-pattern {{ background-image: linear-gradient(rgba(255,255,255,.045) 1px, transparent 1px),
              linear-gradient(90deg, rgba(255,255,255,.045) 1px, transparent 1px);
              background-size: 44px 44px; }}
  .card {{ background:#fff; border:1px solid #e7ecf4; border-radius:1rem;
         box-shadow:0 1px 2px rgba(16,24,40,.05); }}
  .btn-primary {{ background:#3b6ef6; color:#fff; padding:.7rem 1.4rem; border-radius:.6rem;
         font-weight:600; transition:.15s; display:inline-block; }}
  .btn-primary:hover {{ background:#2d5ad9; transform:translateY(-1px);
         box-shadow:0 6px 16px rgba(59,110,246,.35); }}
  .input {{ width:100%; border:1px solid #d5dce8; border-radius:.6rem; padding:.65rem .9rem;
         font-size:.9rem; transition:.15s; background:#fff; }}
  .input:focus {{ outline:none; border-color:#3b6ef6; box-shadow:0 0 0 3px rgba(59,110,246,.15); }}
  code {{ background:#eef2f9; color:#2447b8; padding:.15rem .45rem; border-radius:.35rem; font-size:.82rem; }}
  .badge {{ font-size:.7rem; font-weight:700; letter-spacing:.05em; padding:.25rem .65rem; border-radius:999px; }}
  .m-post {{ background:#dbeafe; color:#1d4ed8; }}
  .m-get  {{ background:#dcfce7; color:#15803d; }}
  .m-patch {{ background:#ffedd5; color:#c2410c; }}
  .m-del  {{ background:#fee2e2; color:#b91c1c; }}
  .navlink {{ color:#64748b; font-weight:500; transition:.15s; }}
  .navlink:hover {{ color:#0f172a; }}
</style>
</head>"""


def _footer() -> str:
    return """
<footer class="border-t border-slate-200 bg-white">
  <div class="max-w-6xl mx-auto px-6 py-10 flex flex-col md:flex-row items-center justify-between gap-4">
    <div class="flex items-center gap-2 text-slate-500 text-sm">
      MailHub — API d'emails transactionnels &amp; OTP
    </div>
    <div class="flex items-center gap-6 text-sm text-slate-500">
      <a href="/docs" class="hover:text-slate-900">Documentation</a>
      <a href="/health" class="hover:text-slate-900">Statut</a>
      <a href="/demo" class="hover:text-slate-900">Démo</a>
    </div>
  </div>
</footer>"""


def _landing_page(content: str) -> HTMLResponse:
    return HTMLResponse(f"""{_head("Accueil")}
<body class="bg-slate-50 text-slate-800">
<nav class="bg-white/90 backdrop-blur border-b border-slate-200 sticky top-0 z-20">
  <div class="max-w-6xl mx-auto px-6 h-16 flex items-center justify-between">
    <a href="/" class="flex items-center gap-2.5 font-extrabold text-lg text-slate-900">
      {LOGO_SVG}
      MailHub
    </a>
    <div class="flex items-center gap-7 text-sm">
      <a href="/demo" class="navlink">Démo</a>
      <a href="/docs" class="navlink">API Docs</a>
      <a href="/login" class="navlink">Connexion</a>
      <a href="/register" class="btn-primary">Commencer gratuitement</a>
    </div>
  </div>
</nav>
{content}
{_footer()}
</body></html>""")


def _dashboard_page(user, tenant, content: str, active: str = "dashboard") -> HTMLResponse:
    icons = {
        "dashboard": '<svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>',
        "demo": '<svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M5 3l14 9-14 9V3z"/></svg>',
        "docs": '<svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M4 4h13l3 3v13a1 1 0 01-1 1H4a1 1 0 01-1-1V5a1 1 0 011-1z"/></svg>',
    }
    items = [("dashboard", "Dashboard", "/dashboard"), ("demo", "Tester l'API", "/demo")]
    nav = "".join(
        f'<a href="{url}" class="flex items-center gap-3 px-4 py-2.5 rounded-lg text-sm font-medium '
        f'{"bg-brand-600 text-white shadow-lg shadow-brand-600/30" if key == active else "text-slate-400 hover:bg-white/5 hover:text-white"} transition">'
        f'{icons[key]}{label}</a>'
        for key, label, url in items
    )
    return HTMLResponse(f"""{_head("Dashboard")}
<body class="bg-slate-100 text-slate-800">
<div class="flex min-h-screen">
  <aside class="w-64 bg-slate-900 text-white flex flex-col fixed inset-y-0 z-10">
    <div class="h-16 flex items-center gap-2.5 px-6 border-b border-white/10">
      {LOGO_SVG}
      <span class="font-extrabold">MailHub</span>
    </div>
    <nav class="flex-1 px-3 py-5 space-y-1">{nav}
      <a href="/docs" class="flex items-center gap-3 px-4 py-2.5 rounded-lg text-sm font-medium text-slate-400 hover:bg-white/5 hover:text-white transition">{icons['docs']}API Docs</a>
    </nav>
    <div class="px-5 py-5 border-t border-white/10">
      <div class="flex items-center gap-3">
        <div class="w-9 h-9 rounded-full bg-brand-600 flex items-center justify-center text-sm font-bold uppercase">
          {user.email[0]}
        </div>
        <div class="min-w-0">
          <p class="text-xs font-semibold text-slate-200 truncate">{user.email}</p>
          <p class="text-[11px] text-slate-500 truncate">{tenant.brand_name}</p>
        </div>
      </div>
      <a href="/logout" class="mt-4 inline-flex items-center gap-1.5 text-xs text-slate-400 hover:text-white transition">
        <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4M16 17l5-5-5-5M21 12H9"/></svg>
        Se déconnecter
      </a>
    </div>
  </aside>
  <main class="ml-64 flex-1 p-8 lg:p-10">{content}</main>
</div>
</body></html>""")


# =====================================================================
# Auth helpers
# =====================================================================

async def _current_user(request: Request):
    token = request.cookies.get("session_id")
    if not token:
        return None
    sessions = SessionStore(request.app.state.redis)
    user_id = await sessions.get_user_id(token)
    if not user_id:
        return None
    return await WebUserStore(request.app.state.redis).get(user_id)


async def _require_user(request: Request):
    user = await _current_user(request)
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


# =====================================================================
# Pages publiques
# =====================================================================

ICON = {
    "lock": """<svg class="w-6 h-6" fill="none" stroke="#3b6ef6" stroke-width="2" viewBox="0 0 24 24"><rect x="4" y="10" width="16" height="11" rx="2"/><path d="M8 10V7a4 4 0 118 0v3"/></svg>""",
    "mail": """<svg class="w-6 h-6" fill="none" stroke="#3b6ef6" stroke-width="2" viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7l9 6 9-6"/></svg>""",
    "layers": """<svg class="w-6 h-6" fill="none" stroke="#3b6ef6" stroke-width="2" viewBox="0 0 24 24"><path d="M12 2l9 5-9 5-9-5 9-5z"/><path d="M3 12l9 5 9-5"/><path d="M3 17l9 5 9-5"/></svg>""",
    "shield": """<svg class="w-6 h-6" fill="none" stroke="#3b6ef6" stroke-width="2" viewBox="0 0 24 24"><path d="M12 2l8 4v6c0 5-3.5 8.5-8 10-4.5-1.5-8-5-8-10V6l8-4z"/><path d="M9 12l2 2 4-4"/></svg>""",
    "gauge": """<svg class="w-6 h-6" fill="none" stroke="#3b6ef6" stroke-width="2" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/></svg>""",
    "code": """<svg class="w-6 h-6" fill="none" stroke="#3b6ef6" stroke-width="2" viewBox="0 0 24 24"><path d="M8 6l-6 6 6 6M16 6l6 6-6 6"/></svg>""",
}


@router.get("/", response_class=HTMLResponse)
async def home() -> HTMLResponse:
    features = [
        (ICON["lock"], "OTP sécurisé",
         "Codes 6 chiffres cryptographiques, hashés en Redis, usage unique, TTL 5 min, anti-rejeu HMAC."),
        (ICON["mail"], "Emails transactionnels",
         "Bienvenue, notifications, transactions ou email 100% libre — en français et en anglais."),
        (ICON["layers"], "Multi-tenant natif",
         "Chaque application obtient ses propres clés, son branding, ses quotas et ses données isolées."),
        (ICON["shield"], "Sécurité service-à-service",
         "Clé API + signature HMAC-SHA256 + timestamp anti-rejeu. Requêtes non signées rejetées."),
        (ICON["gauge"], "Rate limiting intégré",
         "3 envois / 10 min par utilisateur et par IP, cooldown 60 s, 5 tentatives de vérification max."),
        (ICON["code"], "Intégration en 5 minutes",
         "Une requête POST signée et c'est parti. SDK d'exemple dans votre dashboard."),
    ]
    features_html = "".join(
        f"""<div class="card p-6 hover:shadow-lg transition">
          <div class="w-11 h-11 rounded-xl bg-brand-50 flex items-center justify-center mb-4">{icon}</div>
          <h3 class="font-bold mb-1.5">{t}</h3>
          <p class="text-sm text-slate-500 leading-relaxed">{d}</p>
        </div>"""
        for icon, t, d in features
    )

    endpoints = [
        ("POST", "/v1/otp/send", "Envoyer un code OTP par email (ou fallback SMS)", "m-post"),
        ("POST", "/v1/otp/verify", "Vérifier un code OTP — usage unique, 5 tentatives", "m-post"),
        ("POST", "/v1/email/send", "Email transactionnel : welcome, notification, transaction, custom", "m-post"),
        ("POST", "/v1/admin/tenants", "Créer un tenant — réservé admin (X-Admin-Key)", "m-post"),
        ("GET", "/v1/admin/tenants", "Lister / gérer les tenants — admin", "m-get"),
        ("GET", "/health", "Healthcheck Redis + version", "m-get"),
    ]
    rows = "".join(
        f"""<tr class="hover:bg-slate-50 transition">
          <td class="py-3.5"><span class="badge {cls}">{m}</span></td>
          <td class="py-3.5"><code>{ep}</code></td>
          <td class="py-3.5 text-slate-600">{d}</td>
        </tr>"""
        for m, ep, d, cls in endpoints
    )

    content = f"""
<section class="hero-bg grid-pattern text-white relative overflow-hidden">
  <div class="max-w-6xl mx-auto px-6 py-28 text-center relative">
    <span class="badge bg-white/10 text-blue-200 border border-white/20 backdrop-blur">
      FastAPI · Redis · Multi-tenant · HMAC
    </span>
    <h1 class="mt-8 text-5xl md:text-6xl font-black tracking-tight leading-[1.05]">
      Envoyez vos OTP<br>et emails transactionnels<br>
      <span class="bg-gradient-to-r from-blue-400 to-emerald-400 bg-clip-text text-transparent">en 5 minutes.</span>
    </h1>
    <p class="mt-7 text-lg text-slate-300 max-w-2xl mx-auto leading-relaxed">
      Une seule API pour vos codes de vérification, emails de bienvenue,
      notifications et confirmations de transactions. Créez un compte,
      récupérez vos clés, intégrez — c'est parti.
    </p>
    <div class="mt-11 flex items-center justify-center gap-4">
      <a href="/register" class="btn-primary text-base px-8 py-3.5">Créer un compte gratuit</a>
      <a href="/demo" class="px-8 py-3.5 font-semibold text-white/90 border border-white/20 rounded-xl hover:bg-white/10 transition">Essayer la console de test</a>
    </div>
    <div class="mt-16 flex items-center justify-center gap-8 text-xs text-slate-400">
      <span>✓ Sans carte bancaire</span><span>✓ Self-hostable</span><span>✓ Open Source prêt</span>
    </div>
  </div>
</section>

<section class="max-w-6xl mx-auto px-6 -mt-12 relative z-10">
  <div class="grid md:grid-cols-3 gap-5">{features_html}</div>
</section>

<section class="max-w-6xl mx-auto px-6 py-20">
  <div class="text-center mb-10">
    <h2 class="text-3xl font-extrabold">Comment ça marche</h2>
    <p class="text-slate-500 mt-2">Trois étapes et votre application envoie des emails.</p>
  </div>
  <div class="grid md:grid-cols-3 gap-6">
    <div class="card p-6 text-center">
      <div class="w-10 h-10 mx-auto rounded-full bg-brand-600 text-white font-bold flex items-center justify-center mb-4">1</div>
      <h3 class="font-bold mb-2">Créez votre compte</h3>
      <p class="text-sm text-slate-500">Un tenant + une clé API + un secret HMAC sont générés instantanément.</p>
    </div>
    <div class="card p-6 text-center">
      <div class="w-10 h-10 mx-auto rounded-full bg-brand-600 text-white font-bold flex items-center justify-center mb-4">2</div>
      <h3 class="font-bold mb-2">Configurez votre expéditeur</h3>
      <p class="text-sm text-slate-500">Votre adresse email et votre SMTP dans le dashboard — test intégré.</p>
    </div>
    <div class="card p-6 text-center">
      <div class="w-10 h-10 mx-auto rounded-full bg-brand-600 text-white font-bold flex items-center justify-center mb-4">3</div>
      <h3 class="font-bold mb-2">Intégrez en un appel</h3>
      <p class="text-sm text-slate-500">POST signé HMAC vers <code>/v1/otp/send</code> ou <code>/v1/email/send</code>.</p>
    </div>
  </div>
</section>

<section class="max-w-6xl mx-auto px-6 pb-20">
  <div class="card overflow-hidden">
    <div class="px-8 pt-7 pb-5 border-b border-slate-100">
      <h2 class="font-bold text-xl">Endpoints API</h2>
      <p class="text-sm text-slate-500 mt-1">Toutes les routes sous <code>/v1/*</code> requièrent une signature HMAC.</p>
    </div>
    <table class="w-full text-sm">
      <thead><tr class="text-left text-slate-400 text-xs uppercase tracking-wider border-b bg-slate-50">
        <th class="py-3 px-8 font-semibold">Méthode</th>
        <th class="py-3 font-semibold">Endpoint</th>
        <th class="py-3 font-semibold">Description</th>
      </tr></thead>
      <tbody class="divide-y divide-slate-100 px-8">{rows}</tbody>
    </table>
  </div>
</section>

<section class="max-w-6xl mx-auto px-6 pb-24">
  <div class="hero-bg grid-pattern rounded-2xl p-12 text-center text-white relative overflow-hidden">
    <h2 class="text-3xl font-extrabold relative">Prêt à envoyer votre premier email ?</h2>
    <p class="text-slate-300 mt-3 relative">Compte gratuit, clés instantanées, aucune carte requise.</p>
    <a href="/register" class="btn-primary inline-block mt-7 relative">Commencer maintenant →</a>
  </div>
</section>
"""
    return _landing_page(content)


def _auth_form(title: str, action: str, fields: str, link: str) -> HTMLResponse:
    return _landing_page(f"""
<div class="min-h-[80vh] flex items-center justify-center px-6 py-16">
  <div class="card p-8 w-full max-w-md">
    <h1 class="text-2xl font-extrabold mb-1">{title}</h1>
    <p class="text-sm text-slate-500 mb-6">Accédez à votre espace MailHub</p>
    <form method="post" action="{action}" class="space-y-4">
      {fields}
      <button type="submit" class="btn-primary w-full">{title}</button>
    </form>
    <p class="text-xs text-slate-400 mt-6 text-center">{link}</p>
  </div>
</div>
""")


@router.get("/register", response_class=HTMLResponse)
async def register_page() -> HTMLResponse:
    fields = """
      <div><label class="text-xs font-semibold text-slate-600">Nom de votre application</label>
      <input name="app_name" class="input mt-1" placeholder="Ma Boutique" required></div>
      <div><label class="text-xs font-semibold text-slate-600">Marque (dans vos emails)</label>
      <input name="brand_name" class="input mt-1" placeholder="MaBoutique" required></div>
      <div><label class="text-xs font-semibold text-slate-600">Email</label>
      <input name="email" type="email" class="input mt-1" placeholder="vous@exemple.com" required></div>
      <div><label class="text-xs font-semibold text-slate-600">Mot de passe</label>
      <input name="password" type="password" class="input mt-1" minlength="8" required></div>
    """
    link = 'Déjà un compte ? <a href="/login" class="text-blue-600 font-semibold">Se connecter</a>'
    return _auth_form("Créer un compte", "/register", fields, link)


@router.post("/register")
async def register(
    request: Request,
    app_name: str = Form(...),
    brand_name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
):
    redis = request.app.state.redis
    users = WebUserStore(redis)
    if await users.get_by_email(email):
        return _auth_form(
            "Créer un compte", "/register",
            '<p class="text-sm text-red-600">Cet email est déjà utilisé.</p>',
            '<a href="/login" class="text-blue-600">Se connecter</a>',
        )

    # Auto-provisioning : créer le tenant + credentials
    tenant_id = f"t_{uuid.uuid4().hex[:10]}"
    api_key = generate_api_key()
    hmac_secret = generate_hmac_secret()
    tenant = Tenant(
        tenant_id=tenant_id,
        name=app_name,
        api_key_hash=hash_api_key(api_key),
        hmac_secret=hmac_secret,
        brand_name=brand_name,
        allowed_purposes=["login_2fa", "order_confirmation", "password_reset"],
        is_active=True,
    )
    await TenantStore(redis).create(tenant)

    # Stocker les credentials en clair temporairement pour les afficher
    # une fois dans le dashboard (puis régénérables)
    await redis.setex(f"webuser:creds:{tenant_id}", 86400, f"{api_key}|{hmac_secret}")

    user = await users.create(email, password, tenant_id, app_name)
    token = await SessionStore(redis).create(user.user_id)

    resp = RedirectResponse("/dashboard", status_code=303)
    resp.set_cookie("session_id", token, httponly=True, samesite="lax")
    return resp


@router.get("/login", response_class=HTMLResponse)
async def login_page() -> HTMLResponse:
    fields = """
      <div><label class="text-xs font-semibold text-slate-600">Email</label>
      <input name="email" type="email" class="input mt-1" required></div>
      <div><label class="text-xs font-semibold text-slate-600">Mot de passe</label>
      <input name="password" type="password" class="input mt-1" required></div>
    """
    link = 'Pas de compte ? <a href="/register" class="text-blue-600 font-semibold">Créer un compte</a>'
    return _auth_form("Connexion", "/login", fields, link)


@router.post("/login")
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
):
    user = await WebUserStore(request.app.state.redis).authenticate(email, password)
    if not user:
        return _auth_form(
            "Connexion", "/login",
            '<p class="text-sm text-red-600">Email ou mot de passe incorrect.</p>',
            '<a href="/register" class="text-blue-600">Créer un compte</a>',
        )
    token = await SessionStore(request.app.state.redis).create(user.user_id)
    resp = RedirectResponse("/dashboard", status_code=303)
    resp.set_cookie("session_id", token, httponly=True, samesite="lax")
    return resp


@router.get("/logout")
async def logout(request: Request):
    token = request.cookies.get("session_id")
    if token:
        await SessionStore(request.app.state.redis).destroy(token)
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie("session_id")
    return resp


# =====================================================================
# Dashboard
# =====================================================================

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    user = await _require_user(request)
    redis = request.app.state.redis
    tenant = await TenantStore(redis).get(user.tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant introuvable")

    # Credentials (affichés si encore en cache — sinon régénérables)
    creds = await redis.get(f"webuser:creds:{tenant.tenant_id}")
    if creds:
        api_key, hmac_secret = creds.split("|")
        creds_html = f"""
        <div class="mt-4 space-y-3">
          <div>
            <p class="text-xs font-semibold text-slate-500 mb-1">Clé API (header <code>X-API-Key</code>)</p>
            <div class="flex gap-2"><code class="flex-1 text-xs break-all bg-slate-50 p-2 rounded">{api_key}</code>
            <button onclick="navigator.clipboard.writeText('{api_key}')" class="text-xs btn-primary">Copier</button></div>
          </div>
          <div>
            <p class="text-xs font-semibold text-slate-500 mb-1">Secret HMAC (signature des requêtes)</p>
            <div class="flex gap-2"><code class="flex-1 text-xs break-all bg-slate-50 p-2 rounded">{hmac_secret}</code>
            <button onclick="navigator.clipboard.writeText('{hmac_secret}')" class="text-xs btn-primary">Copier</button></div>
          </div>
        </div>"""
    else:
        creds_html = """
        <p class="mt-4 text-sm text-amber-600 bg-amber-50 rounded-lg p-3">
          Vos clés ont été affichées lors de l'inscription. Contactez l'admin pour les régénérer.
        </p>"""

    stats_keys = [
        ("Emails envoyés", "stats:emails_sent"),
        ("OTP envoyés", "stats:otps_sent"),
        ("OTP vérifiés", "stats:otps_verified"),
    ]
    stats_cards = ""
    for label, key in stats_keys:
        val = await redis.get(f"{tenant.key_prefix()}{key}") or "0"
        stats_cards += f"""
        <div class="card p-5">
          <p class="text-xs font-semibold text-slate-500 uppercase tracking-wide">{label}</p>
          <p class="text-3xl font-extrabold mt-1">{val}</p>
        </div>"""

    base_url = str(request.base_url).rstrip("/")

    content = f"""
<h1 class="text-2xl font-extrabold mb-1">Dashboard</h1>
<p class="text-sm text-slate-500 mb-8">Bienvenue {user.app_name} — voici votre espace API.</p>

<div class="grid md:grid-cols-3 gap-5 mb-8">{stats_cards}</div>

<div class="card p-6 mb-6">
  <div class="flex items-center justify-between mb-2">
    <h2 class="font-bold">Vos credentials API</h2>
    <span class="badge bg-emerald-100 text-emerald-700">ACTIF</span>
  </div>
  <p class="text-sm text-slate-500">Tenant ID : <code>{tenant.tenant_id}</code> — URL de base : <code>{base_url}</code></p>
  {creds_html}
  <form method="post" action="/dashboard/regenerate-keys" class="mt-4">
    <button class="text-xs font-semibold text-rose-600 border border-rose-200 rounded-lg px-4 py-2 hover:bg-rose-50">
      Régénérer mes clés (invalide les anciennes)
    </button>
  </form>
</div>

<div class="card p-6 mb-6">
  <h2 class="font-bold mb-1">Configuration de votre expéditeur email</h2>
  <p class="text-sm text-slate-500 mb-4">Configurez l'adresse email utilisée pour envoyer VOS emails
  (Gmail : utilisez un <a class="text-blue-600 underline" href="https://myaccount.google.com/apppasswords" target="_blank">mot de passe d'application</a>).</p>
  <form method="post" action="/dashboard/config" class="grid md:grid-cols-2 gap-4">
    <div><label class="text-xs font-semibold text-slate-600">Marque affichée</label>
    <input name="brand_name" class="input mt-1" value="{tenant.brand_name}"></div>
    <div><label class="text-xs font-semibold text-slate-600">Adresse expéditrice</label>
    <input name="from_email" type="email" class="input mt-1" value="{tenant.from_email or ''}" placeholder="no-reply@votredomaine.com"></div>
    <div><label class="text-xs font-semibold text-slate-600">Nom expéditeur</label>
    <input name="from_name" class="input mt-1" value="{tenant.from_name or ''}" placeholder="{tenant.brand_name}"></div>
    <div><label class="text-xs font-semibold text-slate-600">SMTP Host</label>
    <input name="smtp_host" class="input mt-1" value="{_cfg(tenant,'smtp_host') or 'smtp.gmail.com'}"></div>
    <div><label class="text-xs font-semibold text-slate-600">SMTP User</label>
    <input name="smtp_user" class="input mt-1" value="{tenant.smtp_user or ''}" placeholder="votre.email@gmail.com"></div>
    <div><label class="text-xs font-semibold text-slate-600">SMTP Password (app password)</label>
    <input name="smtp_password" type="password" class="input mt-1" placeholder="16 caractères"></div>
    <div class="md:col-span-2 flex gap-3">
      <button class="btn-primary">Sauvegarder</button>
    </div>
  </form>
  <form method="post" action="/dashboard/test-email" class="mt-3 flex gap-2 items-center">
    <input name="test_to" type="email" class="input max-w-xs" placeholder="envoyer un email de test à..." required>
    <button class="text-xs font-semibold text-blue-600 border border-blue-200 rounded-lg px-4 py-2.5 hover:bg-blue-50">Tester l'envoi</button>
  </form>
</div>

<div class="card p-6 mb-6">
  <h2 class="font-bold mb-1">Intégrer l'API dans votre projet</h2>
  <p class="text-sm text-slate-500 mb-4">Chaque requête doit être signée : <code>HMAC-SHA256(body + timestamp, votre secret)</code>.</p>
  <div class="bg-slate-900 text-slate-100 rounded-lg p-5 text-xs overflow-auto font-mono">
<pre># Python — client prêt à l'emploi (pip install requests)
import hashlib, hmac, json, time, requests

BASE_URL    = "{base_url}"
API_KEY     = "VOTRE_CLE_API"
HMAC_SECRET = b"VOTRE_SECRET_HMAC"

def sign(body: bytes) -> dict:
    ts = str(int(time.time()))
    sig = hmac.new(HMAC_SECRET, body + ts.encode(), hashlib.sha256).hexdigest()
    return {{"Content-Type":"application/json","X-API-Key":API_KEY,
            "X-Timestamp":ts,"X-HMAC-Signature":sig}}

# Envoyer un OTP
body = json.dumps({{"user_id":"USER_UUID","email":"client@exemple.com",
  "country_code":"FR","purpose":"login_2fa","language":"fr"}}).encode()
r = requests.post(BASE_URL+"/v1/otp/send", data=body, headers=sign(body))
print(r.json())   # {{"channel":"email","request_id":"req_...","expires_in":300}}

# Vérifier le code
body = json.dumps({{"request_id":"req_...","code":"123456"}}).encode()
r = requests.post(BASE_URL+"/v1/otp/verify", data=body, headers=sign(body))
print(r.json())   # {{"verified":true,"attempts_remaining":0}}</pre>
  </div>
  <p class="text-xs text-slate-500 mt-3">Node.js / PHP / cURL : voir <a href="/docs" class="text-blue-600 underline">la documentation Swagger</a>.</p>
</div>

<div class="card p-6">
  <h2 class="font-bold mb-4">Votre configuration</h2>
  <div class="grid md:grid-cols-2 gap-4 text-sm">
    <div><p class="text-xs font-semibold text-slate-500">Purposes autorisés</p><p class="mt-1">{', '.join(f'<code>{p}</code>' for p in tenant.allowed_purposes)}</p></div>
    <div><p class="text-xs font-semibold text-slate-500">Rollout OTP</p><p class="font-semibold mt-1">{tenant.rollout_percentage} %</p></div>
    <div><p class="text-xs font-semibold text-slate-500">Pays en fallback SMS</p><p class="mt-1">{', '.join(tenant.risky_countries) or '—'}</p></div>
    <div><p class="text-xs font-semibold text-slate-500">Expiration OTP</p><p class="font-semibold mt-1">{tenant.otp_ttl_seconds} s</p></div>
  </div>
</div>
"""
    return _dashboard_page(user, tenant, content)


def _cfg(tenant: Tenant, key: str):
    return getattr(tenant, key, None)


@router.post("/dashboard/config")
async def dashboard_config(
    request: Request,
    brand_name: str = Form(...),
    from_email: str = Form(""),
    from_name: str = Form(""),
    smtp_host: str = Form(""),
    smtp_user: str = Form(""),
    smtp_password: str = Form(""),
):
    user = await _require_user(request)
    store = TenantStore(request.app.state.redis)
    updates = {
        "brand_name": brand_name.strip(),
        "from_email": from_email.strip() or None,
        "from_name": from_name.strip() or None,
        "smtp_host": smtp_host.strip() or None,
        "smtp_user": smtp_user.strip() or None,
    }
    if smtp_password.strip():
        updates["smtp_password"] = smtp_password.strip()
    await store.update(user.tenant_id, updates)
    return RedirectResponse("/dashboard", status_code=303)


@router.post("/dashboard/regenerate-keys")
async def dashboard_regenerate(request: Request):
    user = await _require_user(request)
    redis = request.app.state.redis
    store = TenantStore(redis)
    tenant = await store.get(user.tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant introuvable")

    # Nouvelle clé + secret ; on remplace l'ancien mapping de la clé
    old_hash = tenant.api_key_hash
    new_key = generate_api_key()
    new_secret = generate_hmac_secret()
    tenant.api_key_hash = hash_api_key(new_key)
    tenant.hmac_secret = new_secret
    await redis.set(f"tenant:{tenant.tenant_id}", tenant.model_dump_json())
    await redis.delete(f"tenant:bykey:{old_hash}")
    await redis.set(f"tenant:bykey:{tenant.api_key_hash}", tenant.tenant_id)
    await redis.setex(f"webuser:creds:{tenant.tenant_id}", 86400, f"{new_key}|{new_secret}")
    return RedirectResponse("/dashboard", status_code=303)


@router.post("/dashboard/test-email")
async def dashboard_test_email(
    request: Request,
    test_to: str = Form(...),
    settings: Settings = Depends(get_settings),
):
    user = await _require_user(request)
    tenant = await TenantStore(request.app.state.redis).get(user.tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant introuvable")

    service = request.app.state.email_service
    req = EmailSendRequest(
        to_email=test_to,
        type=EmailType.custom,
        language=Language.fr,
        subject=f"Test d'envoi — {tenant.brand_name}",
        text_body=(
            f"Ceci est un email de test envoyé via votre tenant {tenant.tenant_id}.\n"
            "Si vous le recevez, votre configuration SMTP fonctionne."
        ),
    )
    await service.send(tenant, req)
    return RedirectResponse("/dashboard", status_code=303)


# =====================================================================
# Démo interactive (accessible sans compte en DEBUG)
# =====================================================================

def _demo_page() -> str:
    return """
<div class="max-w-4xl mx-auto py-10 px-6">
  <h1 class="text-2xl font-extrabold mb-1">Console de test</h1>
  <p class="text-sm text-slate-500 mb-8">Testez les endpoints en direct. Les emails sont loggés côté serveur (provider console).</p>

  <div class="card p-6 mb-6">
    <h2 class="font-bold mb-4">1. Envoyer un OTP</h2>
    <div class="grid md:grid-cols-2 gap-4">
      <div><label class="text-xs font-semibold text-slate-600">Tenant</label>
      <input id="otpTenant" class="input mt-1" value="default"></div>
      <div><label class="text-xs font-semibold text-slate-600">Email</label>
      <input id="email" type="email" class="input mt-1" value="test@example.com"></div>
      <div><label class="text-xs font-semibold text-slate-600">Code pays</label>
      <input id="country" class="input mt-1" value="FR" maxlength="3"></div>
      <div><label class="text-xs font-semibold text-slate-600">Langue</label>
      <select id="language" class="input mt-1"><option value="fr">Français</option><option value="en">English</option></select></div>
    </div>
    <button onclick="sendOtp()" class="btn-primary mt-4">Envoyer l'OTP</button>
    <pre id="sendResult" class="mt-4 text-xs bg-slate-900 text-emerald-300 rounded-lg p-4 hidden overflow-auto"></pre>
    <div id="otpCode" class="mt-3 text-center text-3xl font-extrabold tracking-[.4em] text-blue-600 hidden"></div>
  </div>

  <div class="card p-6 mb-6">
    <h2 class="font-bold mb-4">2. Vérifier le code</h2>
    <div class="grid md:grid-cols-2 gap-4">
      <div><label class="text-xs font-semibold text-slate-600">Request ID</label>
      <input id="requestId" class="input mt-1" placeholder="req_..."></div>
      <div><label class="text-xs font-semibold text-slate-600">Code</label>
      <input id="code" class="input mt-1" maxlength="6" placeholder="123456"></div>
    </div>
    <button onclick="verifyOtp()" class="btn-primary mt-4">Vérifier</button>
    <pre id="verifyResult" class="mt-4 text-xs bg-slate-900 text-emerald-300 rounded-lg p-4 hidden overflow-auto"></pre>
  </div>

  <div class="card p-6">
    <h2 class="font-bold mb-4">3. Email transactionnel</h2>
    <div class="grid md:grid-cols-2 gap-4">
      <div><label class="text-xs font-semibold text-slate-600">Tenant</label>
      <input id="emailTenant" class="input mt-1" value="default"></div>
      <div><label class="text-xs font-semibold text-slate-600">Destinataire</label>
      <input id="emailTo" type="email" class="input mt-1" value="test@example.com"></div>
      <div><label class="text-xs font-semibold text-slate-600">Type</label>
      <select id="emailType" class="input mt-1">
        <option value="welcome">welcome</option><option value="notification" selected>notification</option>
        <option value="transaction">transaction</option><option value="custom">custom</option>
      </select></div>
      <div><label class="text-xs font-semibold text-slate-600">Langue</label>
      <select id="emailLang" class="input mt-1"><option value="fr">Français</option><option value="en">English</option></select></div>
      <div class="md:col-span-2"><label class="text-xs font-semibold text-slate-600">Titre / Sujet</label>
      <input id="emailTitle" class="input mt-1" value="Votre commande est expédiée"></div>
      <div class="md:col-span-2"><label class="text-xs font-semibold text-slate-600">Message</label>
      <input id="emailMessage" class="input mt-1" value="Votre colis arrive dans 48h."></div>
    </div>
    <button onclick="sendEmail()" class="btn-primary mt-4">Envoyer l'email</button>
    <pre id="emailResult" class="mt-4 text-xs bg-slate-900 text-emerald-300 rounded-lg p-4 hidden overflow-auto"></pre>
  </div>
</div>

<script>
function show(id, data, ok) {
  const el = document.getElementById(id);
  el.classList.remove('hidden');
  el.className = el.className.replace(/text-(emerald|rose)-300/, ok ? 'text-emerald-300' : 'text-rose-300');
  el.textContent = JSON.stringify(data, null, 2);
}
async function sendOtp() {
  const res = await fetch("/demo/send", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({email: email.value, country_code: country.value, language: language.value,
      purpose: "login_2fa", tenant_id: otpTenant.value || "default"})});
  const data = await res.json(); show("sendResult", data, res.ok);
  if (data.request_id) requestId.value = data.request_id;
  if (data.debug_code) { const c = document.getElementById("otpCode");
    c.classList.remove("hidden"); c.textContent = "Code : " + data.debug_code; code.value = data.debug_code; }
}
async function verifyOtp() {
  const res = await fetch("/demo/verify", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({request_id: requestId.value, code: code.value,
      tenant_id: otpTenant.value || "default"})});
  show("verifyResult", await res.json(), (await res.json()).verified);
}
async function sendEmail() {
  const t = emailType.value;
  const payload = {to_email: emailTo.value, type: t, language: emailLang.value,
    tenant_id: emailTenant.value || "default", data: {}};
  if (t === "welcome") payload.data = {name: "John Doe"};
  else if (t === "custom") { payload.subject = emailTitle.value; payload.text_body = emailMessage.value; }
  else payload.data = t === "notification" ? {title: emailTitle.value, message: emailMessage.value}
                    : {amount: "149,90", currency: "EUR", status: "confirmée"};
  const res = await fetch("/demo/email", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify(payload)});
  show("emailResult", await res.json(), res.ok);
}
</script>
"""


@router.get("/demo", response_class=HTMLResponse)
async def demo(settings: Settings = Depends(get_settings)) -> HTMLResponse:
    if not settings.debug:
        raise HTTPException(status_code=404)
    return _landing_page(f'<div class="bg-slate-50">{_demo_page()}</div>')


# =====================================================================
# Endpoints /demo/* (proxys non signés — DEBUG uniquement)
# =====================================================================

class DemoSendRequest(BaseModel):
    email: EmailStr
    country_code: str = "FR"
    language: Language = Language.fr
    purpose: str = "login_2fa"
    tenant_id: str = "default"


class DemoVerifyRequest(BaseModel):
    request_id: str
    code: str
    tenant_id: str = "default"


async def _demo_tenant(request: Request, tenant_id: str) -> Tenant:
    tenant = await TenantStore(request.app.state.redis).get(tenant_id)
    if tenant is None or not tenant.is_active:
        raise HTTPException(status_code=404, detail=f"Tenant '{tenant_id}' not found")
    return tenant


@router.post("/demo/send", include_in_schema=False)
async def demo_send(
    payload: DemoSendRequest,
    request: Request,
    service: OTPService = Depends(get_otp_service),
    settings: Settings = Depends(get_settings),
):
    if not settings.debug:
        raise HTTPException(status_code=404)
    tenant = await _demo_tenant(request, payload.tenant_id)
    response = await service.send_otp(
        tenant=tenant,
        user_id=str(uuid.uuid4()),
        email=str(payload.email),
        country_code=payload.country_code,
        purpose=payload.purpose,
        language=payload.language,
        client_ip="127.0.0.1",
        include_debug_code=True,
    )
    data = response.model_dump()
    if getattr(response, "debug_code", None):
        data["debug_code"] = response.debug_code
    return data


@router.post("/demo/verify", include_in_schema=False)
async def demo_verify(
    payload: DemoVerifyRequest,
    request: Request,
    service: OTPService = Depends(get_otp_service),
    settings: Settings = Depends(get_settings),
):
    if not settings.debug:
        raise HTTPException(status_code=404)
    tenant = await _demo_tenant(request, payload.tenant_id)
    return await service.verify(tenant, payload.request_id, payload.code)


class DemoEmailRequest(BaseModel):
    to_email: EmailStr
    type: EmailType = EmailType.notification
    language: Language = Language.fr
    data: dict = {}
    subject: str | None = None
    text_body: str | None = None
    tenant_id: str = "default"


@router.post("/demo/email", include_in_schema=False)
async def demo_email(
    payload: DemoEmailRequest,
    request: Request,
    service: EmailService = Depends(get_email_service),
    settings: Settings = Depends(get_settings),
):
    if not settings.debug:
        raise HTTPException(status_code=404)
    tenant = await _demo_tenant(request, payload.tenant_id)
    req = EmailSendRequest(
        to_email=payload.to_email,
        type=payload.type,
        language=payload.language,
        data=payload.data,
        subject=payload.subject,
        text_body=payload.text_body,
    )
    return await service.send(tenant, req)
