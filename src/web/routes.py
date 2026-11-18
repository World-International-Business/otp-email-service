"""Interface web de la plateforme — landing, auth, dashboard, démo."""

import hmac
import html
import json
import logging
import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, EmailStr

from src.api.dependencies import (
    get_client_ip,
    get_email_service,
    get_otp_service,
    require_same_origin,
)
from src.config import Settings, get_settings
from src.core.models import EmailSendRequest, EmailType, Language
from src.core.tenants import (
    PLANS,
    Tenant,
    TenantStore,
    generate_api_key,
    generate_hmac_secret,
    hash_api_key,
    plan_quota_updates,
)
from src.services.email_service import EmailService
from src.services.email_templates import otp_html
from src.services.events import get_events, mask_email
from src.services.otp_service import OTPService
from src.web.i18n import (
    DEFAULT_LANG,
    LANG_COOKIE,
    LANG_COOKIE_MAX_AGE,
    LANGUAGES,
    I18nRoute,
    safe_next,
)
from src.web.users import SessionStore, WebUserStore, hash_password_pair

router = APIRouter(tags=["web"], include_in_schema=False, route_class=I18nRoute)
logger = logging.getLogger(__name__)


def _e(value) -> str:
    """Échappe une valeur avant injection dans le HTML (anti-XSS)."""
    return html.escape(str(value), quote=True)


# =====================================================================
# Templates HTML — design moderne (Tailwind)
# =====================================================================

LOGO_SVG = """<svg class="w-9 h-9" viewBox="0 0 40 40" fill="none">
<rect width="40" height="40" rx="10" fill="url(#g)"/>
<path d="M10 14.5l10 7 10-7" stroke="#fff" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>
<path d="M11 13.5h18a1.5 1.5 0 011.5 1.5v10a1.5 1.5 0 01-1.5 1.5H11a1.5 1.5 0 01-1.5-1.5V15a1.5 1.5 0 011.5-1.5z" stroke="#fff" stroke-width="2.4"/>
<defs><linearGradient id="g" x1="0" y1="0" x2="40" y2="40"><stop stop-color="#6366f1"/><stop offset="1" stop-color="#a855f7"/></linearGradient></defs>
</svg>"""


def _head(title: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="fr" class="scroll-smooth">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} — MailHub</title>
<script src="https://cdn.tailwindcss.com"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap" rel="stylesheet">
<script>
tailwind.config = {{ theme: {{ extend: {{ fontFamily: {{ sans: ['Inter','sans-serif'] }},
  colors: {{ brand: {{50:'#eef2ff',100:'#e0e7ff',200:'#c7d2fe',300:'#a5b4fc',400:'#818cf8',
                     500:'#6366f1',600:'#4f46e5',700:'#4338ca',800:'#3730a3',900:'#312e81'}} }},
  boxShadow: {{ soft: '0 2px 12px rgba(99,102,241,.12)' }} }} }} }};
</script>
<style>
  :root {{ --brand: #6366f1; }}
  body {{ font-family: 'Inter', sans-serif; -webkit-font-smoothing: antialiased; text-rendering: optimizeLegibility; }}
  ::selection {{ background: rgba(99,102,241,.25); }}
  ::-webkit-scrollbar {{ width: 10px; }}
  ::-webkit-scrollbar-track {{ background: #f1f5f9; }}
  ::-webkit-scrollbar-thumb {{ background: #c7d2fe; border-radius: 999px; }}
  ::-webkit-scrollbar-thumb:hover {{ background: #a5b4fc; }}
  .hero-bg {{ background: radial-gradient(ellipse 90% 65% at 50% -15%, rgba(99,102,241,.5), transparent),
              radial-gradient(ellipse 45% 45% at 85% 15%, rgba(168,85,247,.22), transparent),
              radial-gradient(ellipse 35% 35% at 10% 30%, rgba(16,185,129,.12), transparent),
              linear-gradient(180deg, #090b1c 0%, #131735 55%, #1a1440 100%); }}
  .grid-pattern {{ background-image: linear-gradient(rgba(255,255,255,.05) 1px, transparent 1px),
              linear-gradient(90deg, rgba(255,255,255,.05) 1px, transparent 1px);
              background-size: 46px 46px;
              mask-image: radial-gradient(ellipse 70% 65% at 50% 0%, black 30%, transparent 100%); }}
  .orb {{ position:absolute; border-radius:999px; filter:blur(90px); opacity:.35; pointer-events:none; }}
  .card {{ background:#fff; border:1px solid #e8eaf2; border-radius:1.15rem;
         box-shadow:0 1px 3px rgba(15,23,42,.05); }}
  .card-lift:hover {{ transform:translateY(-4px); box-shadow:0 16px 32px rgba(15,23,42,.1);
         border-color:#c7d2fe; transition:all .22s ease; }}
  .btn-primary {{ background:linear-gradient(135deg,#6366f1 0%,#8b5cf6 100%); color:#fff;
         padding:.72rem 1.6rem; border-radius:.7rem; font-weight:600; transition:all .18s ease;
         display:inline-block; box-shadow:0 4px 14px rgba(99,102,241,.35); }}
  .btn-primary:hover {{ transform:translateY(-2px);
         box-shadow:0 10px 26px rgba(99,102,241,.45); filter:brightness(1.1); }}
  .btn-primary:active {{ transform:translateY(0); }}
  .btn-ghost {{ padding:.72rem 1.6rem; font-weight:600; color:rgba(255,255,255,.92);
         border:1px solid rgba(255,255,255,.22); border-radius:.7rem; transition:all .18s ease;
         backdrop-filter:blur(6px); }}
  .btn-ghost:hover {{ background:rgba(255,255,255,.1); border-color:rgba(255,255,255,.4); }}
  .btn-outline {{ padding:.6rem 1.3rem; font-weight:600; color:#4f46e5; border:1.5px solid #c7d2fe;
         border-radius:.7rem; transition:all .18s ease; display:inline-block; }}
  .btn-outline:hover {{ background:#eef2ff; border-color:#818cf8; }}
  .gradient-text {{ background:linear-gradient(90deg,#818cf8,#c084fc,#34d399);
         -webkit-background-clip:text; background-clip:text; color:transparent; }}
  .glass {{ background:rgba(255,255,255,.06); border:1px solid rgba(255,255,255,.12);
         backdrop-filter:blur(12px); }}
  .input {{ width:100%; border:1px solid #d5dce8; border-radius:.65rem; padding:.65rem .95rem;
         font-size:.9rem; transition:all .15s; background:#fff; }}
  .input:focus {{ outline:none; border-color:var(--brand); box-shadow:0 0 0 3px rgba(59,110,246,.15); }}
  .input::placeholder {{ color:#94a3b8; }}
  code {{ background:#eef2f9; color:#2447b8; padding:.15rem .45rem; border-radius:.35rem; font-size:.82rem; overflow-wrap:anywhere; }}
  .badge {{ font-size:.68rem; font-weight:700; letter-spacing:.06em; padding:.28rem .7rem; border-radius:999px; }}
  .m-post {{ background:#dbeafe; color:#1d4ed8; }}
  .m-get  {{ background:#dcfce7; color:#15803d; }}
  .m-patch {{ background:#ffedd5; color:#c2410c; }}
  .m-del  {{ background:#fee2e2; color:#b91c1c; }}
  .navlink {{ color:#64748b; font-weight:500; transition:.15s; }}
  .navlink:hover {{ color:#0f172a; }}
  @keyframes fadeUp {{ from {{ opacity:0; transform:translateY(18px); }} to {{ opacity:1; transform:none; }} }}
  .fade-up {{ animation:fadeUp .6s ease both; }}
  .fade-up-1 {{ animation-delay:.08s }} .fade-up-2 {{ animation-delay:.16s }} .fade-up-3 {{ animation-delay:.24s }}
  @media (max-width:640px) {{ .input {{ font-size:16px; }} }}
  #sidebar {{ inset-inline-start:0; }}
  @media (min-width:768px) {{ .sb-offset {{ margin-inline-start:16rem; }} }}
</style>
</head>"""


def _footer(logged_in: bool = False) -> str:
    account = (
        '<a href="/dashboard" class="hover:text-white transition">Mon espace</a>'
        if logged_in else
        '<a href="/register" class="hover:text-white transition">Créer un compte</a>\n'
        '        <a href="/login" class="hover:text-white transition">Connexion</a>'
    )
    return f"""
<footer class="bg-slate-950 text-slate-400">
  <div class="max-w-6xl mx-auto px-6 py-10 md:py-14 grid md:grid-cols-3 gap-8 md:gap-10">
    <div>
      <div class="flex items-center gap-2.5 text-white font-extrabold mb-3">{LOGO_SVG} MailHub</div>
      <p class="text-sm leading-relaxed">API d'emails transactionnels et de codes OTP.
      Sécurisée, multi-tenant, self-hostable.</p>
    </div>
    <div>
      <p class="text-white font-semibold text-sm mb-3">Produit</p>
      <div class="flex flex-col gap-2 text-sm">
        <a href="/documentation" class="hover:text-white transition">Documentation</a>
        {account}
      </div>
    </div>
    <div>
      <p class="text-white font-semibold text-sm mb-3">Statut</p>
      <a href="/health" class="inline-flex items-center gap-2 text-sm hover:text-white transition">
        <span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></span> Système opérationnel
      </a>
    </div>
  </div>
  <div class="border-t border-white/10">
    <div class="max-w-6xl mx-auto px-6 py-5 text-xs text-slate-500 flex flex-col sm:flex-row items-center justify-between gap-2 text-center">
      <span>© MailHub — World International Business</span>
      <span>FastAPI · Redis · HMAC-SHA256</span>
    </div>
  </div>
</footer>"""


def _landing_page(content: str, logged_in: bool = False) -> HTMLResponse:
    if logged_in:
        auth_desktop = ('      __LANG_SWITCH_LIGHT__\n'
                        '      <a href="/dashboard" class="btn-primary text-sm px-4 py-2.5">Mon espace →</a>')
        auth_mobile = '      <a href="/dashboard" class="btn-primary text-center text-sm my-3">Mon espace →</a>'
    else:
        auth_desktop = ('      <a href="/login" class="navlink">Connexion</a>\n'
                        '      __LANG_SWITCH_LIGHT__\n'
                        '      <a href="/register" class="btn-primary text-sm px-4 py-2.5">Commencer</a>')
        auth_mobile = ('      <a href="/login" class="navlink py-3">Connexion</a>\n'
                       '      <a href="/register" class="btn-primary text-center text-sm my-3">Commencer</a>')
    return HTMLResponse(f"""{_head("Accueil")}
<body class="bg-slate-50 text-slate-800">
<nav class="bg-white/90 backdrop-blur border-b border-slate-200 sticky top-0 z-20">
  <div class="max-w-6xl mx-auto px-4 sm:px-6 h-16 flex items-center justify-between">
    <a href="/" class="flex items-center gap-2.5 font-extrabold text-lg text-slate-900">
      {LOGO_SVG}
      MailHub
    </a>
    <div class="hidden md:flex items-center gap-6 text-sm">
      <a href="/#services" class="navlink">Services</a>
      <a href="/#pricing" class="navlink">Tarifs</a>
      <a href="/documentation" class="navlink">Documentation</a>
{auth_desktop}
    </div>
    <div class="flex md:hidden items-center gap-1">
      __LANG_SWITCH_LIGHT__
      <button type="button" aria-label="Menu"
              onclick="document.getElementById('mobileMenu').classList.toggle('hidden')"
              class="p-2 rounded-lg text-slate-600 hover:bg-slate-100">
        <svg class="w-6 h-6" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" d="M4 7h16M4 12h16M4 17h16"/></svg>
      </button>
    </div>
  </div>
  <div id="mobileMenu" class="hidden md:hidden border-t border-slate-200 bg-white">
    <div class="px-4 py-2 flex flex-col text-sm divide-y divide-slate-100">
      <a href="/#services" onclick="document.getElementById('mobileMenu').classList.add('hidden')" class="navlink py-3">Services</a>
      <a href="/#pricing" onclick="document.getElementById('mobileMenu').classList.add('hidden')" class="navlink py-3">Tarifs</a>
      <a href="/documentation" class="navlink py-3">Documentation</a>
{auth_mobile}
    </div>
  </div>
</nav>
{content}
{_footer(logged_in)}
</body></html>""")


def _dashboard_page(user, tenant, content: str, active: str = "dashboard", debug: bool = True) -> HTMLResponse:
    icons = {
        "dashboard": '<svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>',
        "stats": '<svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M4 20V10m5 10V4m5 16v-7m5 7V8"/></svg>',
        "plan": '<svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M12 2l2.5 6.5L21 9l-5 4.5L17.5 21 12 17l-5.5 4L8 13.5 3 9l6.5-.5z"/></svg>',
        "creds": '<svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><circle cx="8" cy="15" r="4"/><path d="M10.9 12.1L21 2m-4 4l3 3"/></svg>',
        "smtp": '<svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="M2 7l10 6L22 7"/></svg>',
        "branding": '<svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 019 9h-9z"/><circle cx="12" cy="12" r="3"/></svg>',
        "integration": '<svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M8 6l-5 6 5 6m8-12l5 6-5 6"/></svg>',
        "support": '<svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M21 12a8 8 0 01-8 8H5l-2 2V12a8 8 0 018-8h2a8 8 0 018 8z"/></svg>',
        "demo": '<svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M5 3l14 9-14 9V3z"/></svg>',
        "docs": '<svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M4 4h13l3 3v13a1 1 0 01-1 1H4a1 1 0 01-1-1V5a1 1 0 011-1z"/></svg>',
    }
    items = [
        ("dashboard", "Vue d'ensemble", "/dashboard"),
        ("stats", "Statistiques", "/dashboard#sec-stats"),
        ("plan", "Abonnement", "/dashboard#sec-plan"),
        ("creds", "Clés API", "/dashboard#sec-creds"),
        ("smtp", "Expéditeur SMTP", "/dashboard#sec-smtp"),
        ("branding", "Emails & marque", "/dashboard#sec-branding"),
        ("integration", "Intégration", "/dashboard#sec-integration"),
        ("support", "Support", "/dashboard#sec-support"),
        # Console de test : toujours visible — pour un client connecté elle
        # reste accessible même si la démo publique est désactivée.
        ("demo", "Tester l'API", "/demo"),
    ]
    nav = "".join(
        f'<a href="{url}" class="flex items-center gap-3 px-4 py-2.5 rounded-lg text-sm font-medium '
        f'{"bg-brand-600 text-white shadow-lg shadow-brand-600/30" if key == active else "text-slate-400 hover:bg-white/5 hover:text-white"} transition">'
        f'{icons[key]}{label}</a>'
        for key, label, url in items
    )
    return HTMLResponse(f"""{_head("Dashboard")}
<body class="bg-slate-100 text-slate-800">
<header class="md:hidden fixed top-0 inset-x-0 h-14 bg-slate-900 text-white flex items-center justify-between px-4 z-20">
  <a href="/dashboard" class="flex items-center gap-2 font-extrabold">{LOGO_SVG} MailHub</a>
  <div class="flex items-center gap-1">
    __LANG_SWITCH_HEADER__
    <button type="button" aria-label="Menu"
            onclick="document.getElementById('sidebar').classList.toggle('-translate-x-full');document.getElementById('sbOverlay').classList.toggle('hidden')"
            class="p-2 rounded-lg hover:bg-white/10">
      <svg class="w-6 h-6" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" d="M4 7h16M4 12h16M4 17h16"/></svg>
    </button>
  </div>
</header>
<div id="sbOverlay" class="hidden fixed inset-0 bg-black/50 z-20 md:hidden"
     onclick="document.getElementById('sidebar').classList.add('-translate-x-full');this.classList.add('hidden')"></div>
<div class="flex min-h-screen">
  <aside id="sidebar" class="w-64 bg-slate-900 text-white flex flex-col fixed inset-y-0 z-30 overflow-y-auto transform -translate-x-full md:translate-x-0 transition-transform duration-200">
    <div class="h-16 flex items-center gap-2.5 px-6 border-b border-white/10 shrink-0">
      {LOGO_SVG}
      <span class="font-extrabold">MailHub</span>
      <button type="button" aria-label="Fermer" class="ml-auto md:hidden p-1.5 rounded-lg hover:bg-white/10"
              onclick="document.getElementById('sidebar').classList.add('-translate-x-full');document.getElementById('sbOverlay').classList.add('hidden')">
        <svg class="w-5 h-5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" d="M6 6l12 12M18 6L6 18"/></svg>
      </button>
    </div>
    <nav class="flex-1 px-3 py-5 space-y-1">{nav}
      <a href="/documentation" class="flex items-center gap-3 px-4 py-2.5 rounded-lg text-sm font-medium text-slate-400 hover:bg-white/5 hover:text-white transition">{icons['docs']}Documentation</a>
    </nav>
    <div class="px-5 py-5 border-t border-white/10">
      <div class="mb-4">__LANG_SWITCH_SIDEBAR__</div>
      <div class="flex items-center gap-3">
        <div class="w-9 h-9 rounded-full bg-brand-600 flex items-center justify-center text-sm font-bold uppercase">
          {_e(user.email[0])}
        </div>
        <div class="min-w-0">
          <p class="text-xs font-semibold text-slate-200 truncate">{_e(user.email)}</p>
          <p class="text-[11px] text-slate-500 truncate">{_e(tenant.brand_name)}</p>
        </div>
      </div>
      <form method="post" action="/logout" class="mt-4">
        <button type="submit" class="inline-flex items-center gap-1.5 text-xs text-slate-400 hover:text-white transition bg-transparent border-0 p-0 cursor-pointer">
          <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4M16 17l5-5-5-5M21 12H9"/></svg>
          Se déconnecter
        </button>
      </form>
    </div>
  </aside>
  <main class="sb-offset flex-1 min-w-0 pt-20 px-4 pb-8 sm:px-6 md:pt-8 md:px-8 lg:p-10">{content}</main>
</div>
</body></html>""")


# =====================================================================
# Changement de langue
# =====================================================================

@router.get("/lang/{code}")
async def set_language(code: str, next_url: str = Query("/", alias="next")):
    """Enregistre la langue choisie dans un cookie puis revient à la page d'origine."""
    if code not in LANGUAGES:
        code = DEFAULT_LANG
    resp = RedirectResponse(safe_next(next_url), status_code=303)
    resp.set_cookie(LANG_COOKIE, code, max_age=LANG_COOKIE_MAX_AGE, samesite="lax", path="/")
    return resp


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
    "lock": """<svg class="w-6 h-6" fill="none" stroke="#6366f1" stroke-width="2" viewBox="0 0 24 24"><rect x="4" y="10" width="16" height="11" rx="2"/><path d="M8 10V7a4 4 0 118 0v3"/></svg>""",
    "mail": """<svg class="w-6 h-6" fill="none" stroke="#6366f1" stroke-width="2" viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7l9 6 9-6"/></svg>""",
    "layers": """<svg class="w-6 h-6" fill="none" stroke="#6366f1" stroke-width="2" viewBox="0 0 24 24"><path d="M12 2l9 5-9 5-9-5 9-5z"/><path d="M3 12l9 5 9-5"/><path d="M3 17l9 5 9-5"/></svg>""",
    "shield": """<svg class="w-6 h-6" fill="none" stroke="#6366f1" stroke-width="2" viewBox="0 0 24 24"><path d="M12 2l8 4v6c0 5-3.5 8.5-8 10-4.5-1.5-8-5-8-10V6l8-4z"/><path d="M9 12l2 2 4-4"/></svg>""",
    "gauge": """<svg class="w-6 h-6" fill="none" stroke="#6366f1" stroke-width="2" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/></svg>""",
    "code": """<svg class="w-6 h-6" fill="none" stroke="#6366f1" stroke-width="2" viewBox="0 0 24 24"><path d="M8 6l-6 6 6 6M16 6l6 6-6 6"/></svg>""",
    "globe": """<svg class="w-6 h-6" fill="none" stroke="#8b5cf6" stroke-width="2" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a15 15 0 010 18M12 3a15 15 0 000 18"/></svg>""",
    "chart": """<svg class="w-6 h-6" fill="none" stroke="#10b981" stroke-width="2" viewBox="0 0 24 24"><path d="M4 20V10M10 20V4M16 20v-8M22 20H2"/></svg>""",
}


@router.get("/", response_class=HTMLResponse)
async def home(request: Request, settings: Settings = Depends(get_settings)) -> HTMLResponse:
    if await _current_user(request):
        return RedirectResponse("/dashboard", status_code=303)
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
          <td class="py-3.5 pl-5 sm:pl-8 pr-4"><span class="badge {cls}">{m}</span></td>
          <td class="py-3.5 pr-4 whitespace-nowrap"><code>{ep}</code></td>
          <td class="py-3.5 pr-5 sm:pr-8 text-slate-600">{d}</td>
        </tr>"""
        for m, ep, d, cls in endpoints
    )

    services = [
        (ICON["lock"], "Vérification OTP",
         "Codes à 6 chiffres cryptographiques, hachés, usage unique, TTL 5 min, 5 tentatives max.",
         "border-brand-200"),
        (ICON["mail"], "Emails transactionnels",
         "Bienvenue, notifications, confirmations de transaction ou email 100 % libre — FR & EN.",
         "border-violet-200"),
        (ICON["layers"], "Multi-tenant",
         "Chaque application cliente : clés propres, branding, quotas et données isolées dans Redis.",
         "border-blue-200"),
        (ICON["shield"], "Sécurité HMAC",
         "Clé API + signature HMAC-SHA256 + timestamp anti-rejeu sur chaque requête.",
         "border-emerald-200"),
        (ICON["globe"], "Routage géographique",
         "Pays à risque → fallback SMS automatique. Rollout progressif par pourcentage d'utilisateurs.",
         "border-amber-200"),
        (ICON["chart"], "Rate limiting & stats",
         "3 envois / 10 min par user et IP, cooldown 60 s, statistiques par tenant dans le dashboard.",
         "border-rose-200"),
    ]
    services_html = "".join(
        f"""<div class="card card-lift p-6 transition-all border-t-4 {bc}">
          <div class="w-11 h-11 rounded-xl bg-gradient-to-br from-brand-50 to-violet-100 border border-brand-100 flex items-center justify-center mb-4">{icon}</div>
          <h3 class="font-bold mb-1.5">{t}</h3>
          <p class="text-sm text-slate-500 leading-relaxed">{d}</p>
        </div>"""
        for icon, t, d, bc in services
    )

    plans = [
        ("Starter", "0€", "Pour tester et prototyper",
         ["1 tenant", "Clés API + HMAC instantanées", "Provider console (logs)", "Rate limiting inclus"],
         False),
        ("Pro", "Sur mesure", "Pour les apps en production",
         ["Tenants illimités", "SMTP / Brevo / Resend", "Branding par tenant", "Fallback SMS par pays", "Support prioritaire"],
         True),
        ("Enterprise", "Sur mesure", "Volume élevé & conformité",
         ["Instance dédiée", "SLA & quotas personnalisés", "Audit & logs avancés", "Accompagnement intégration"],
         False),
    ]
    plans_html = "".join(
        f"""<div class="card {'ring-2 ring-brand-500 shadow-xl scale-[1.03]' if hot else 'card-lift'} p-7 transition-all relative">
          {'<span class="badge bg-brand-600 text-white absolute -top-3 left-1/2 -translate-x-1/2">POPULAIRE</span>' if hot else ''}
          <h3 class="font-bold text-lg">{name}</h3>
          <p class="text-3xl font-black mt-2">{price}</p>
          <p class="text-sm text-slate-500 mt-1 mb-5">{desc}</p>
          <ul class="space-y-2.5 text-sm text-slate-600">
            {''.join(f'<li class="flex gap-2"><span class="text-emerald-500 font-bold">✓</span>{f_}</li>' for f_ in feats)}
          </ul>
          <a href="/register" class="{'btn-primary' if hot else 'btn-outline'} w-full text-center mt-6 block">Choisir</a>
        </div>"""
        for name, price, desc, feats, hot in plans
    )

    content = f"""
<section class="hero-bg text-white relative overflow-hidden">
  <div class="absolute inset-0 grid-pattern"></div>
  <div class="orb w-96 h-96 bg-brand-500 -top-20 -left-20"></div>
  <div class="orb w-80 h-80 bg-violet-500 top-40 -right-16"></div>
  <div class="max-w-6xl mx-auto px-6 py-16 md:py-28 relative">
    <div class="grid lg:grid-cols-2 gap-14 items-center">
      <div class="text-left">
        <span class="badge bg-white/10 text-indigo-200 border border-white/20 backdrop-blur fade-up">
          API OTP & Emails transactionnels
        </span>
        <h1 class="mt-7 text-4xl sm:text-5xl md:text-[3.6rem] font-black tracking-tight leading-[1.06] fade-up fade-up-1">
          Vos codes de vérification et emails,
          <span class="gradient-text">en un appel API.</span>
        </h1>
        <p class="mt-6 text-base sm:text-lg text-slate-300 leading-relaxed fade-up fade-up-2">
          Une seule API multi-tenant pour vos OTP, emails de bienvenue,
          notifications et confirmations. Créez un compte, récupérez vos clés,
          intégrez — c'est parti.
        </p>
        <div class="mt-9 flex flex-col sm:flex-row gap-4 fade-up fade-up-3">
          <a href="/register" class="btn-primary text-base px-8 py-3.5 text-center">Créer un compte gratuit</a>
          {'<a href="/demo" class="btn-ghost text-base text-center">Essayer la console</a>' if settings.demo_enabled else '<a href="/docs" class="btn-ghost text-base text-center">Documentation API</a>'}
        </div>
        <div class="mt-10 flex flex-wrap gap-x-7 gap-y-2 text-xs text-slate-400 fade-up fade-up-3">
          <span class="inline-flex items-center gap-1.5"><span class="text-emerald-400">✓</span> Sans carte bancaire</span>
          <span class="inline-flex items-center gap-1.5"><span class="text-emerald-400">✓</span> Self-hostable</span>
          <span class="inline-flex items-center gap-1.5"><span class="text-emerald-400">✓</span> Sécurité HMAC-SHA256</span>
        </div>
      </div>
      <div class="glass rounded-2xl p-1.5 shadow-2xl fade-up fade-up-2 hidden lg:block">
        <div class="bg-slate-950/80 rounded-xl overflow-hidden">
          <div class="flex items-center gap-2 px-4 py-3 border-b border-white/10">
            <span class="w-3 h-3 rounded-full bg-rose-400"></span>
            <span class="w-3 h-3 rounded-full bg-amber-400"></span>
            <span class="w-3 h-3 rounded-full bg-emerald-400"></span>
            <span class="text-xs text-slate-400 ml-2 font-mono">api.sh</span>
          </div>
          <pre class="p-5 text-[13px] leading-relaxed font-mono text-slate-300"><span class="text-slate-500"># Envoyer un OTP</span>
<span class="text-violet-400">curl</span> -X POST {str('/v1/otp/send')} \\
  -H <span class="text-emerald-300">"X-API-Key: ••••"</span> \\
  -d <span class="text-amber-300">'{{"email":"client@ex.com","purpose":"login_2fa"}}'</span>

<span class="text-slate-500"># Réponse</span>
<span class="text-emerald-300">{{"channel":"email","request_id":"req_…","expires_in":300}}</span></pre>
        </div>
      </div>
    </div>
  </div>
  <div class="absolute -bottom-px left-0 right-0 h-24 bg-gradient-to-t from-slate-50 to-transparent"></div>
</section>

<section class="max-w-6xl mx-auto px-6 -mt-8 relative z-10 pb-4">
  <div class="grid grid-cols-2 md:grid-cols-4 gap-4">
    <div class="card p-5 text-center"><p class="text-2xl font-black text-brand-600">6</p><p class="text-xs text-slate-500 mt-1">Endpoints sécurisés</p></div>
    <div class="card p-5 text-center"><p class="text-2xl font-black text-brand-600">&lt;30ms</p><p class="text-xs text-slate-500 mt-1">Latence API</p></div>
    <div class="card p-5 text-center"><p class="text-2xl font-black text-brand-600">∞</p><p class="text-xs text-slate-500 mt-1">Tenants</p></div>
    <div class="card p-5 text-center"><p class="text-2xl font-black text-brand-600">HMAC</p><p class="text-xs text-slate-500 mt-1">SHA-256 signé</p></div>
  </div>
</section>

<section id="services" class="max-w-6xl mx-auto px-6 py-14 md:py-20">
  <div class="text-center mb-12">
    <span class="badge bg-brand-50 text-brand-700 border border-brand-100">NOS SERVICES</span>
    <h2 class="text-3xl md:text-4xl font-extrabold mt-4">Tout pour vos emails & OTP</h2>
    <p class="text-slate-500 mt-3 max-w-xl mx-auto">Une plateforme complète, pensée pour les développeurs et les produits qui grandissent.</p>
  </div>
  <div class="grid md:grid-cols-3 gap-5">{services_html}</div>
</section>

<section class="max-w-6xl mx-auto px-6 py-16">
  <div class="text-center mb-10">
    <h2 class="text-3xl font-extrabold">Comment ça marche</h2>
    <p class="text-slate-500 mt-2">Trois étapes et votre application envoie des emails.</p>
  </div>
  <div class="grid md:grid-cols-3 gap-6">
    <div class="card card-lift p-7 text-center transition-all">
      <div class="w-11 h-11 mx-auto rounded-full bg-gradient-to-br from-brand-500 to-violet-600 text-white font-bold flex items-center justify-center mb-4 shadow-lg shadow-brand-500/30">1</div>
      <h3 class="font-bold mb-2">Créez votre compte</h3>
      <p class="text-sm text-slate-500">Un tenant + une clé API + un secret HMAC sont générés instantanément.</p>
    </div>
    <div class="card card-lift p-7 text-center transition-all">
      <div class="w-11 h-11 mx-auto rounded-full bg-gradient-to-br from-brand-500 to-violet-600 text-white font-bold flex items-center justify-center mb-4 shadow-lg shadow-brand-500/30">2</div>
      <h3 class="font-bold mb-2">Configurez votre expéditeur</h3>
      <p class="text-sm text-slate-500">Votre adresse email et votre SMTP dans le dashboard — test intégré.</p>
    </div>
    <div class="card card-lift p-7 text-center transition-all">
      <div class="w-11 h-11 mx-auto rounded-full bg-gradient-to-br from-brand-500 to-violet-600 text-white font-bold flex items-center justify-center mb-4 shadow-lg shadow-brand-500/30">3</div>
      <h3 class="font-bold mb-2">Intégrez en un appel</h3>
      <p class="text-sm text-slate-500">POST signé HMAC vers <code>/v1/otp/send</code> ou <code>/v1/email/send</code>.</p>
    </div>
  </div>
</section>

<section class="max-w-6xl mx-auto px-6 py-14">
  <div class="card overflow-hidden">
    <div class="px-5 sm:px-8 pt-7 pb-5 border-b border-slate-100 flex flex-col sm:flex-row sm:items-center justify-between gap-3">
      <div>
        <h2 class="font-bold text-xl">Endpoints API</h2>
        <p class="text-sm text-slate-500 mt-1">Toutes les routes sous <code>/v1/*</code> requièrent une signature HMAC.</p>
      </div>
    </div>
    <div class="overflow-x-auto">
    <table class="w-full text-sm min-w-[620px]">
      <thead><tr class="text-left text-slate-400 text-xs uppercase tracking-wider border-b bg-slate-50">
        <th class="py-3 pl-5 sm:pl-8 pr-4 font-semibold">Méthode</th>
        <th class="py-3 pr-4 font-semibold">Endpoint</th>
        <th class="py-3 pr-5 sm:pr-8 font-semibold">Description</th>
      </tr></thead>
      <tbody class="divide-y divide-slate-100">{rows}</tbody>
    </table>
    </div>
  </div>
</section>

<section id="pricing" class="max-w-6xl mx-auto px-6 py-14 md:py-20">
  <div class="text-center mb-12">
    <span class="badge bg-brand-50 text-brand-700 border border-brand-100">TARIFS</span>
    <h2 class="text-3xl md:text-4xl font-extrabold mt-4">Simple et transparent</h2>
    <p class="text-slate-500 mt-3">Commencez gratuitement, passez à l'échelle quand vous êtes prêt.</p>
  </div>
  <div class="grid md:grid-cols-3 gap-6 items-stretch pt-3">{plans_html}</div>
</section>

<section class="max-w-6xl mx-auto px-6 pb-24">
  <div class="hero-bg rounded-3xl p-8 sm:p-12 md:p-16 text-center text-white relative overflow-hidden">
    <div class="orb w-72 h-72 bg-violet-500 -top-10 -right-10"></div>
    <h2 class="text-3xl md:text-4xl font-extrabold relative">Prêt à envoyer votre premier email ?</h2>
    <p class="text-slate-300 mt-4 relative">Compte gratuit, clés instantanées, aucune carte requise.</p>
    <a href="/register" class="btn-primary inline-block mt-8 relative px-8 py-3.5">Commencer maintenant →</a>
  </div>
</section>
"""
    return _landing_page(content)


def _auth_form(title: str, action: str, fields: str, link: str) -> HTMLResponse:
    return _landing_page(f"""
<div class="min-h-[80vh] flex items-center justify-center px-4 sm:px-6 py-10 sm:py-16">
  <div class="w-full max-w-md fade-up">
    <div class="text-center mb-6">
      <a href="/" class="inline-flex items-center gap-2 font-extrabold text-lg">{LOGO_SVG} MailHub</a>
    </div>
    <div class="card p-6 sm:p-8 shadow-lg">
      <h1 class="text-2xl font-extrabold mb-1">{title}</h1>
      <p class="text-sm text-slate-500 mb-6">Accédez à votre espace MailHub</p>
      <form method="post" action="{action}" class="space-y-4">
        {fields}
        <button type="submit" class="btn-primary w-full">{title}</button>
      </form>
      <p class="text-xs text-slate-400 mt-6 text-center">{link}</p>
    </div>
    <p class="text-center text-xs text-slate-400 mt-5">
      Sécurisé par clé API + signature HMAC-SHA256
    </p>
  </div>
</div>
""")


@router.get("/register", response_class=HTMLResponse)
async def register_page(
    request: Request, error: str = "", settings: Settings = Depends(get_settings)
) -> HTMLResponse:
    if await _current_user(request):
        return RedirectResponse("/dashboard", status_code=303)
    if not settings.web_signup_enabled:
        raise HTTPException(status_code=404)
    err = f'<p class="text-sm text-red-600">{_e(error)}</p>' if error else ""
    invite_field = ""
    if settings.signup_invite_code:
        invite_field = """
      <div><label class="text-xs font-semibold text-slate-600">Code d'invitation</label>
      <input name="invite_code" class="input mt-1" required></div>"""
    fields = err + """
      <div><label class="text-xs font-semibold text-slate-600">Nom de votre application</label>
      <input name="app_name" class="input mt-1" placeholder="Ma Boutique" required></div>
      <div><label class="text-xs font-semibold text-slate-600">Marque (dans vos emails)</label>
      <input name="brand_name" class="input mt-1" placeholder="MaBoutique" required></div>
      <div><label class="text-xs font-semibold text-slate-600">Email</label>
      <input name="email" type="email" class="input mt-1" placeholder="vous@exemple.com" required></div>
      <div><label class="text-xs font-semibold text-slate-600">Mot de passe</label>
      <input name="password" type="password" class="input mt-1" minlength="8" required></div>
    """ + invite_field
    link = 'Déjà un compte ? <a href="/login" class="text-indigo-600 font-semibold">Se connecter</a>'
    return _auth_form("Créer un compte", "/register", fields, link)




@router.post("/register")
async def register(
    request: Request,
    app_name: str = Form(...),
    brand_name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    invite_code: str = Form(""),
    settings: Settings = Depends(get_settings),
    _: None = Depends(require_same_origin),
):
    if not settings.web_signup_enabled:
        raise HTTPException(status_code=404)
    if settings.signup_invite_code and not hmac.compare_digest(
        invite_code.encode("utf-8", "ignore"),
        settings.signup_invite_code.encode(),
    ):
        return _auth_form(
            "Créer un compte", "/register",
            '<p class="text-sm text-red-600">Code d\'invitation invalide.</p>',
            '<a href="/login" class="text-indigo-600">Se connecter</a>',
        )
    if len(password) < 8:
        return _auth_form(
            "Créer un compte", "/register",
            '<p class="text-sm text-red-600">Le mot de passe doit contenir au moins 8 caractères.</p>',
            '<a href="/login" class="text-indigo-600">Se connecter</a>',
        )
    if "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        return _auth_form(
            "Créer un compte", "/register",
            '<p class="text-sm text-red-600">Adresse email invalide.</p>',
            '<a href="/login" class="text-indigo-600">Se connecter</a>',
        )

    redis = request.app.state.redis
    users = WebUserStore(redis)
    if await users.get_by_email(email):
        return _auth_form(
            "Créer un compte", "/register",
            '<p class="text-sm text-red-600">Cet email est déjà utilisé.</p>',
            '<a href="/login" class="text-indigo-600">Se connecter</a>',
        )

    # Si une inscription est déjà en attente pour cet email, on renvoie
    # vers la page de saisie existante au lieu de consommer un nouvel
    # envoi OTP (quota 3/10min + cooldown — sinon l'utilisateur reste
    # bloqué sur le formulaire sans pouvoir saisir le code reçu).
    existing_tok = await redis.get(f"regidx:{email.strip().lower()}")
    if existing_tok and await redis.get(f"reg:{existing_tok}"):
        return RedirectResponse(f"/register/verify?t={existing_tok}", status_code=303)

    # Vérification de l'adresse email AVANT création du compte : on envoie
    # un OTP via le tenant "default" (config .env), puis on stocke la
    # demande en attente dans Redis (15 min). Le compte n'est créé qu'une
    # fois le code saisi correctement — les adresses fictives échouent ici.
    default_tenant = await TenantStore(redis).get("default")
    if not default_tenant:
        raise HTTPException(status_code=500, detail="Service indisponible")
    service: OTPService = request.app.state.otp_service
    try:
        otp_resp = await service.send_otp(
            tenant=_reg_sender_tenant(default_tenant),
            user_id=email.strip().lower(),
            email=email.strip(),
            country_code="FR",
            purpose="login_2fa",
            language=Language.fr,
            client_ip=get_client_ip(request),
            include_debug_code=True,
        )
    except HTTPException as exc:
        return RedirectResponse(
            f"/register?error={quote(str(exc.detail)[:120])}", status_code=303
        )
    if otp_resp.channel.value != "email":
        return RedirectResponse(
            "/register?error="
            + quote("Impossible d'envoyer l'email de vérification pour le moment."),
            status_code=303,
        )

    reg_token = uuid.uuid4().hex
    # Jamais le mot de passe en clair dans Redis : on ne stocke que le
    # hash PBKDF2, réutilisé tel quel à la création du compte.
    pw_salt, pw_hash = hash_password_pair(password)
    await redis.set(
        f"reg:{reg_token}",
        json.dumps({
            "app_name": app_name.strip(), "brand_name": brand_name.strip(),
            "email": email.strip(),
            "password_salt": pw_salt, "password_hash": pw_hash,
            "request_id": otp_resp.request_id,
            "debug_code": otp_resp.debug_code,
        }),
        ex=900,
    )
    await redis.set(f"regidx:{email.strip().lower()}", reg_token, ex=900)
    return RedirectResponse(f"/register/verify?t={reg_token}", status_code=303)


def _reg_sender_tenant(tenant: Tenant) -> Tenant:
    """Tenant 'default' rebrandé « MailHub » pour l'email de vérification
    d'inscription — l'inscription concerne la plateforme, pas le tenant."""
    return tenant.model_copy(update={"brand_name": "MailHub", "from_name": "MailHub"})


def _verify_email_page(
    t: str,
    masked: str,
    error: str = "",
    attempts: int | None = None,
    debug_code: str | None = None,
) -> HTMLResponse:
    err = f'<p class="text-sm text-red-600">{_e(error)}</p>' if error else ""
    att = (
        f'<p class="text-xs text-slate-500 mt-1">Tentatives restantes : {attempts}</p>'
        if attempts is not None else ""
    )
    dbg = ""
    if debug_code:
        dbg = (
            '<p class="text-xs text-amber-700 bg-amber-50 border border-amber-200 '
            f'rounded-lg p-2.5">Mode debug — code : <b>{_e(debug_code)}</b></p>'
        )
    fields = f"""
      <input type="hidden" name="t" value="{_e(t)}">
      {err}
      <div><label class="text-xs font-semibold text-slate-600">Code à 6 chiffres envoyé à {_e(masked)}</label>
      <input name="code" class="input mt-1 text-center text-2xl tracking-[.4em]" maxlength="6"
             inputmode="numeric" autocomplete="one-time-code" placeholder="••••••" required autofocus
             oninput="this.value=this.value.replace(/[^0-9]/g,'');if(this.value.length===6)this.form.requestSubmit()"></div>
      {att}
      {dbg}
    """
    link = (
        '<form method="post" action="/register/resend" class="inline">'
        f'<input type="hidden" name="t" value="{_e(t)}">'
        '<button type="submit" class="text-indigo-600 font-semibold bg-transparent border-0 '
        'p-0 cursor-pointer text-xs">Renvoyer le code</button></form>'
        ' · <a href="/register" class="text-indigo-600 font-semibold">Recommencer</a>'
    )
    return _auth_form("Vérifier mon email", "/register/verify", fields, link)


@router.get("/register/verify", response_class=HTMLResponse)
async def register_verify_page(request: Request, t: str = "") -> HTMLResponse:
    if await _current_user(request):
        return RedirectResponse("/dashboard", status_code=303)
    redis = request.app.state.redis
    raw = await redis.get(f"reg:{t}") if t else None
    if not raw:
        return RedirectResponse("/register", status_code=303)
    pending = json.loads(raw)
    return _verify_email_page(
        t, mask_email(pending["email"]), debug_code=pending.get("debug_code")
    )


@router.post("/register/verify")
async def register_verify(
    request: Request,
    t: str = Form(...),
    code: str = Form(...),
    settings: Settings = Depends(get_settings),
    _: None = Depends(require_same_origin),
):
    redis = request.app.state.redis
    raw = await redis.get(f"reg:{t}")
    if not raw:
        return RedirectResponse("/register", status_code=303)
    pending = json.loads(raw)
    masked = mask_email(pending["email"])

    default_tenant = await TenantStore(redis).get("default")
    if not default_tenant:
        raise HTTPException(status_code=500, detail="Service indisponible")
    service: OTPService = request.app.state.otp_service
    result = await service.verify(
        default_tenant, pending["request_id"], code.strip()
    )
    if not result.verified:
        return _verify_email_page(
            t, masked,
            error="Code invalide ou expiré." if result.attempts_remaining == 0
                  else "Code invalide.",
            attempts=result.attempts_remaining or None,
            debug_code=pending.get("debug_code"),
        )

    # Email vérifiée : dernier contrôle avant provisioning
    users = WebUserStore(redis)
    regidx_key = f"regidx:{pending['email'].strip().lower()}"
    if await users.get_by_email(pending["email"]):
        await redis.delete(f"reg:{t}", regidx_key)
        return _auth_form(
            "Créer un compte", "/register",
            '<p class="text-sm text-red-600">Cet email est déjà utilisé.</p>',
            '<a href="/login" class="text-indigo-600">Se connecter</a>',
        )
    await redis.delete(f"reg:{t}", regidx_key)

    # Auto-provisioning : créer le tenant + credentials
    tenant_id = f"t_{uuid.uuid4().hex[:10]}"
    api_key = generate_api_key()
    hmac_secret = generate_hmac_secret()
    tenant = Tenant(
        tenant_id=tenant_id,
        name=pending["app_name"],
        api_key_hash=hash_api_key(api_key),
        hmac_secret=hmac_secret,
        brand_name=pending["brand_name"],
        allowed_purposes=["login_2fa", "order_confirmation", "password_reset"],
        is_active=True,
    )
    await TenantStore(redis).create(tenant)

    # Stocker les credentials en clair temporairement pour les afficher
    # une fois dans le dashboard (puis régénérables)
    await redis.set(f"webuser:creds:{tenant_id}", f"{api_key}|{hmac_secret}", ex=86400)

    try:
        if "password_hash" in pending:
            user = await users.create_hashed(
                pending["email"], pending["password_hash"],
                pending["password_salt"], tenant_id, pending["app_name"],
            )
        else:
            # Rétrocompat : inscriptions en attente créées avant ce déploiement
            user = await users.create(
                pending["email"], pending["password"],
                tenant_id, pending["app_name"],
            )
    except Exception:
        # Rollback : ne pas laisser un tenant orphelin sans compte associé
        pipe = redis.pipeline()
        pipe.delete(f"tenant:{tenant_id}", f"tenant:bykey:{hash_api_key(api_key)}",
                    f"webuser:creds:{tenant_id}")
        pipe.srem("tenant:index", tenant_id)
        await pipe.execute()
        raise
    token = await SessionStore(redis).create(user.user_id)

    resp = RedirectResponse("/dashboard", status_code=303)
    resp.set_cookie(
        "session_id", token, httponly=True, samesite="lax",
        secure=str(settings.public_base_url).startswith("https://"),
    )
    return resp


@router.post("/register/resend")
async def register_resend(
    request: Request,
    t: str = Form(...),
    _: None = Depends(require_same_origin),
):
    redis = request.app.state.redis
    raw = await redis.get(f"reg:{t}")
    if not raw:
        return RedirectResponse("/register", status_code=303)
    pending = json.loads(raw)

    default_tenant = await TenantStore(redis).get("default")
    if not default_tenant:
        raise HTTPException(status_code=500, detail="Service indisponible")
    service: OTPService = request.app.state.otp_service
    try:
        otp_resp = await service.send_otp(
            tenant=_reg_sender_tenant(default_tenant),
            user_id=pending["email"].lower(),
            email=pending["email"],
            country_code="FR",
            purpose="login_2fa",
            language=Language.fr,
            client_ip=get_client_ip(request),
            include_debug_code=True,
        )
    except HTTPException as exc:
        return _verify_email_page(
            t, mask_email(pending["email"]), error=str(exc.detail),
            debug_code=pending.get("debug_code"),
        )
    if otp_resp.channel.value != "email":
        return _verify_email_page(
            t, mask_email(pending["email"]),
            error="Impossible d'envoyer l'email pour le moment.",
            debug_code=pending.get("debug_code"),
        )
    pending["request_id"] = otp_resp.request_id
    pending["debug_code"] = otp_resp.debug_code
    await redis.set(f"reg:{t}", json.dumps(pending), ex=900)
    return RedirectResponse(f"/register/verify?t={t}", status_code=303)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    if await _current_user(request):
        return RedirectResponse("/dashboard", status_code=303)
    fields = """
      <div><label class="text-xs font-semibold text-slate-600">Email</label>
      <input name="email" type="email" class="input mt-1" required></div>
      <div><label class="text-xs font-semibold text-slate-600">Mot de passe</label>
      <input name="password" type="password" class="input mt-1" required></div>
    """
    link = 'Pas de compte ? <a href="/register" class="text-indigo-600 font-semibold">Créer un compte</a>'
    return _auth_form("Connexion", "/login", fields, link)


@router.post("/login")
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    settings: Settings = Depends(get_settings),
    _: None = Depends(require_same_origin),
):
    # Anti brute-force : 5 tentatives / IP / 10 min
    redis = request.app.state.redis
    ip = get_client_ip(request)
    rl_key = f"rl:login:{ip}"
    attempts = await redis.incr(rl_key)
    if attempts == 1:
        await redis.expire(rl_key, 600)
    if attempts > settings.web_login_max_attempts:
        raise HTTPException(status_code=429, detail="Too many attempts, retry later")

    user = await WebUserStore(redis).authenticate(email, password)
    if not user:
        return _auth_form(
            "Connexion", "/login",
            '<p class="text-sm text-red-600">Email ou mot de passe incorrect.</p>',
            '<a href="/register" class="text-indigo-600">Créer un compte</a>',
        )
    # Succès : reset du compteur anti brute-force pour cette IP
    await redis.delete(rl_key)
    token = await SessionStore(request.app.state.redis).create(user.user_id)
    resp = RedirectResponse("/dashboard", status_code=303)
    resp.set_cookie(
        "session_id", token, httponly=True, samesite="lax",
        secure=str(settings.public_base_url).startswith("https://"),
    )
    return resp


@router.post("/logout")
async def logout(request: Request, _: None = Depends(require_same_origin)):
    token = request.cookies.get("session_id")
    if token:
        await SessionStore(request.app.state.redis).destroy(token)
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie("session_id")
    return resp


@router.get("/logout")
async def logout_get():
    # La déconnexion exige un POST (anti-CSRF : un lien/image tiers ne doit
    # pas pouvoir déconnecter l'utilisateur). Un GET redirige simplement.
    return RedirectResponse("/", status_code=303)


# =====================================================================
# Dashboard
# =====================================================================

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    test: str = "",
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    user = await _require_user(request)
    redis = request.app.state.redis
    tenant = await TenantStore(redis).get(user.tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant introuvable")

    banner = ""
    if test == "ok":
        banner = """<div class="mb-4 text-sm font-semibold text-emerald-700 bg-emerald-50 border border-emerald-200 rounded-lg p-3">
          ✅ Email de test envoyé — vérifiez la boîte de réception (et les spams).</div>"""
    elif test:
        banner = f"""<div class="mb-4 text-sm font-semibold text-red-700 bg-red-50 border border-red-200 rounded-lg p-3">
          ❌ Échec de l'envoi test : {_e(test)}<br>
          <span class="font-normal text-red-600">Vérifiez SMTP Host / Port / User / Password. Rappel : smtp.gmail.com n'accepte
          que les comptes Google — pour une adresse pro, utilisez le serveur SMTP de son hébergeur
          (ex. IONOS : smtp.ionos.fr).</span></div>"""

    # Credentials (affichés si encore en cache — sinon régénérables)
    creds = await redis.get(f"webuser:creds:{tenant.tenant_id}")
    if creds:
        api_key, hmac_secret = (_e(v) for v in creds.split("|"))
        creds_html = f"""
        <div class="mt-4 space-y-3">
          <div>
            <p class="text-xs font-semibold text-slate-500 mb-1">Clé API (header <code>X-API-Key</code>)</p>
            <div class="flex flex-col sm:flex-row gap-2"><code class="flex-1 min-w-0 text-xs break-all bg-slate-50 p-2 rounded">{api_key}</code>
            <button onclick="navigator.clipboard.writeText('{api_key}')" class="text-xs btn-primary">Copier</button></div>
          </div>
          <div>
            <p class="text-xs font-semibold text-slate-500 mb-1">Secret HMAC (signature des requêtes)</p>
            <div class="flex flex-col sm:flex-row gap-2"><code class="flex-1 min-w-0 text-xs break-all bg-slate-50 p-2 rounded">{hmac_secret}</code>
            <button onclick="navigator.clipboard.writeText('{hmac_secret}')" class="text-xs btn-primary">Copier</button></div>
          </div>
        </div>"""
    else:
        creds_html = """
        <p class="mt-4 text-sm text-amber-700 bg-amber-50 border border-amber-200 rounded-lg p-3">
          Vos clés sont masquées pour sécurité (affichées 24 h après génération).
          Pour obtenir une nouvelle clé API + secret, cliquez sur <b>« Régénérer mes clés »</b>
          ci-dessous — elles s'afficheront immédiatement ici.
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
          <p class="text-3xl font-extrabold mt-1">{_e(val)}</p>
        </div>"""

    # Graphique 7 derniers jours (OTP envoyés/vérifiés + emails transactionnels)
    from datetime import date, timedelta
    days = [date.today() - timedelta(days=i) for i in range(6, -1, -1)]
    series = {"otps_sent": [], "otps_verified": [], "emails_sent": []}
    for d in days:
        for metric in series:
            v = await redis.get(f"{tenant.key_prefix()}stats_daily:{metric}:{d.isoformat()}")
            series[metric].append(int(v or 0))
    max_v = max(*(max(v, default=0) for v in series.values()), 1)
    bars = ""
    for i, d in enumerate(days):
        h_sent = int(series["otps_sent"][i] / max_v * 100)
        h_ver = int(series["otps_verified"][i] / max_v * 100)
        h_em = int(series["emails_sent"][i] / max_v * 100)
        bars += f"""
        <div class="flex-1 flex flex-col items-center gap-1.5">
          <div class="w-full flex items-end justify-center gap-1 h-28">
            <div class="w-4 rounded-t bg-gradient-to-t from-brand-500 to-violet-500" style="height:{max(h_sent,4) if series['otps_sent'][i] else 2}%" title="{series['otps_sent'][i]} OTP envoyés"></div>
            <div class="w-4 rounded-t bg-emerald-400" style="height:{max(h_ver,4) if series['otps_verified'][i] else 2}%" title="{series['otps_verified'][i]} OTP vérifiés"></div>
            <div class="w-4 rounded-t bg-amber-400" style="height:{max(h_em,4) if series['emails_sent'][i] else 2}%" title="{series['emails_sent'][i]} emails envoyés"></div>
          </div>
          <span class="text-[10px] text-slate-400">{d.strftime('%d/%m')}</span>
        </div>"""
    chart_html = f"""
<div id="sec-stats" class="card p-4 sm:p-6 mb-6 scroll-mt-24">
  <div class="flex flex-wrap items-center justify-between gap-2 mb-4">
    <h2 class="font-bold">Envois — 7 derniers jours</h2>
    <div class="flex flex-wrap gap-4 text-xs text-slate-500">
      <span class="flex items-center gap-1.5"><span class="w-2.5 h-2.5 rounded-sm bg-brand-500"></span>OTP envoyés</span>
      <span class="flex items-center gap-1.5"><span class="w-2.5 h-2.5 rounded-sm bg-emerald-400"></span>OTP vérifiés</span>
      <span class="flex items-center gap-1.5"><span class="w-2.5 h-2.5 rounded-sm bg-amber-400"></span>Emails</span>
    </div>
  </div>
  <div class="flex gap-2">{bars}</div>
</div>"""

    # Activité récente (20 derniers événements du tenant)
    event_labels = {
        "otp_sent": ("OTP envoyé", "bg-brand-50 text-brand-700"),
        "otp_verified": ("OTP vérifié", "bg-emerald-100 text-emerald-700"),
        "otp_verify_failed": ("Vérif. échouée", "bg-rose-100 text-rose-700"),
        "otp_send_failed": ("Envoi OTP échoué", "bg-rose-100 text-rose-700"),
        "otp_rate_limited": ("Rate limit", "bg-amber-100 text-amber-700"),
        "otp_sms_fallback": ("Fallback SMS", "bg-violet-100 text-violet-700"),
        "otp_purpose_denied": ("Purpose refusé", "bg-slate-200 text-slate-600"),
        "email_sent": ("Email envoyé", "bg-emerald-100 text-emerald-700"),
        "email_failed": ("Email échoué", "bg-rose-100 text-rose-700"),
        "email_rate_limited": ("Rate limit", "bg-amber-100 text-amber-700"),
    }
    events = await get_events(redis, tenant, limit=20)
    if events:
        rows = "".join(
            f"""<tr class="hover:bg-slate-50 transition">
              <td class="py-2.5 pr-3 whitespace-nowrap"><span class="badge {cls}">{lbl}</span></td>
              <td class="py-2.5 pr-3 text-slate-500 text-xs whitespace-nowrap">{_e(e.get('ts','')[11:19])} {_e(e.get('ts','')[:10])}</td>
              <td class="py-2.5 text-slate-600 text-xs truncate max-w-[220px]">{_e(e.get('email') or e.get('request_id') or e.get('email_type') or '—')}</td>
            </tr>"""
            for e in events
            for lbl, cls in [event_labels.get(e.get("type", ""), (e.get("type", "?"), "bg-slate-100 text-slate-600"))]
        )
        activity_html = f"""
<div id="sec-activity" class="card p-4 sm:p-6 mb-6 scroll-mt-24">
  <h2 class="font-bold mb-4">Activité récente</h2>
  <div class="overflow-x-auto"><table class="w-full text-sm">
    <tbody class="divide-y divide-slate-100">{rows}</tbody>
  </table></div>
</div>"""
    else:
        activity_html = ""

    base_url = _e(str(request.base_url).rstrip("/"))

    cur_plan = tenant.plan if tenant and tenant.plan in PLANS else "free"
    pending_plan = tenant.plan_requested if tenant else None
    plan_cards = "".join(
        f'<div class="border rounded-xl p-4 flex flex-col '
        f'{"border-indigo-500 ring-1 ring-indigo-500" if pid == cur_plan else "border-slate-200"}">'
        f'<div class="flex items-center justify-between">'
        f"<p class=\"font-bold\">{_e(p['label'])}</p>"
        + ('<span class="badge bg-indigo-100 text-indigo-700">ACTUEL</span>' if pid == cur_plan else "")
        + "</div>"
        f"<p class=\"text-2xl font-black mt-1\">{_e(p['price'])}</p>"
        f"<p class=\"text-xs text-slate-500 mt-1\">{_e(p['desc'])}</p>"
        f'<ul class="text-[11px] text-slate-500 mt-3 space-y-1">'
        f"<li>{p['max_sends_per_user_per_10min']} envois/user/10min</li>"
        f"<li>{p['max_sends_per_ip_per_10min']} envois/IP/10min</li>"
        f"<li>Cooldown {p['cooldown_seconds']}s · {p['max_verify_attempts']} essais · OTP {p['otp_ttl_seconds']//60} min</li>"
        f"</ul>"
        f'<form method="post" action="/dashboard/plan" class="mt-auto pt-4">'
        f'<input type="hidden" name="plan" value="{_e(pid)}">'
        + (
            '<button disabled class="w-full text-xs font-bold text-slate-400 border border-slate-200 rounded-lg px-4 py-2 cursor-default">Plan actuel</button>'
            if pid == cur_plan
            else (
                '<button disabled class="w-full text-xs font-bold text-amber-600 border border-amber-200 bg-amber-50 rounded-lg px-4 py-2 cursor-default">⏳ Demande en attente</button>'
                if pid == pending_plan
                else '<button class="w-full text-xs font-bold text-indigo-600 border border-indigo-200 rounded-lg px-4 py-2 hover:bg-indigo-50">Choisir ce plan</button>'
            )
        )
        + "</form></div>"
        for pid, p in PLANS.items()
    )
    pending_banner = ""
    if pending_plan and pending_plan in PLANS:
        pending_banner = f"""
  <div class="mb-4 text-sm font-semibold text-amber-700 bg-amber-50 border border-amber-200 rounded-lg p-3">
    ⏳ Demande <b>{_e(PLANS[pending_plan]['label'])}</b> en attente — votre plan sera
    activé dès validation du paiement (Stripe, PayPal, mobile money ou virement).
    Questions ? Utilisez le support ci-dessous.</div>"""
    plan_card_html = f"""
<div id="sec-plan" class="card p-4 sm:p-6 mb-6 scroll-mt-24">
  <h2 class="font-bold mb-1">Votre abonnement</h2>
  <p class="text-sm text-slate-500 mb-4">Plan actuel : <b>{_e(PLANS[cur_plan]['label'])}</b> —
  les plans payants sont activés après validation du paiement.</p>
  {pending_banner}
  <div class="grid sm:grid-cols-3 gap-3">{plan_cards}</div>
</div>"""

    content = f"""
<h1 class="text-2xl font-extrabold mb-1">Dashboard</h1>
<p class="text-sm text-slate-500 mb-8">Bienvenue {_e(user.app_name)} — voici votre espace API.</p>

<div class="grid md:grid-cols-3 gap-5 mb-8">{stats_cards}</div>

{chart_html}
{activity_html}

<div id="sec-creds" class="card p-4 sm:p-6 mb-6 scroll-mt-24">
  <div class="flex items-center justify-between mb-2">
    <h2 class="font-bold">Vos credentials API</h2>
    <span class="badge bg-emerald-100 text-emerald-700">ACTIF</span>
  </div>
  <p class="text-sm text-slate-500">Tenant ID : <code>{_e(tenant.tenant_id)}</code> — URL de base : <code>{base_url}</code></p>
  {creds_html}
  <form method="post" action="/dashboard/regenerate-keys" class="mt-4">
    <button class="text-xs font-semibold text-rose-600 border border-rose-200 rounded-lg px-4 py-2 hover:bg-rose-50">
      Régénérer mes clés (invalide les anciennes)
    </button>
  </form>
</div>

{plan_card_html}

<div id="sec-smtp" class="card p-4 sm:p-6 mb-6 scroll-mt-24">
  <h2 class="font-bold mb-1">Configuration de votre expéditeur email</h2>
  <p class="text-sm text-slate-500 mb-4">Configurez l'adresse email utilisée pour envoyer VOS emails
  (Gmail : utilisez un <a class="text-indigo-600 underline" href="https://myaccount.google.com/apppasswords" target="_blank">mot de passe d'application</a>).</p>
  <form method="post" action="/dashboard/config" class="grid md:grid-cols-2 gap-4">
    <div><label class="text-xs font-semibold text-slate-600">Marque affichée</label>
    <input name="brand_name" class="input mt-1" value="{_e(tenant.brand_name)}"></div>
    <div><label class="text-xs font-semibold text-slate-600">Adresse expéditrice</label>
    <input name="from_email" type="email" class="input mt-1" value="{_e(tenant.from_email or '')}" placeholder="no-reply@votredomaine.com">
    <p class="text-[11px] text-amber-600 mt-1">Avec Gmail, doit correspondre au SMTP User (ou à un alias « Envoyer en tant que » vérifié) — sinon Google réécrit l'expéditeur.</p></div>
    <div><label class="text-xs font-semibold text-slate-600">Nom expéditeur</label>
    <input name="from_name" class="input mt-1" value="{_e(tenant.from_name or '')}" placeholder="{_e(tenant.brand_name)}"></div>
    <div><label class="text-xs font-semibold text-slate-600">SMTP Host</label>
    <input name="smtp_host" class="input mt-1" value="{_e(_cfg(tenant,'smtp_host') or '')}" placeholder="{_e(settings.smtp_host)}">
    <p class="text-[11px] text-slate-400 mt-1">Vide = config globale (.env) · Gmail : smtp.gmail.com · IONOS : smtp.ionos.fr (ou .com selon région) · OVH : ssl0.ovh.net · Zoho : smtp.zoho.com</p></div>
    <div><label class="text-xs font-semibold text-slate-600">SMTP Port</label>
    <input name="smtp_port" type="number" min="1" max="65535" class="input mt-1" value="{_e(str(_cfg(tenant,'smtp_port') or ''))}" placeholder="{settings.smtp_port}">
    <p class="text-[11px] text-slate-400 mt-1">587 = STARTTLS (Gmail, IONOS) · 465 = SSL (OVH, Zoho, cPanel) — détection automatique.</p></div>
    <div><label class="text-xs font-semibold text-slate-600">SMTP User</label>
    <input name="smtp_user" class="input mt-1" value="{_e(tenant.smtp_user or '')}" placeholder="votre.email@gmail.com"></div>
    <div><label class="text-xs font-semibold text-slate-600">SMTP Password (app password)</label>
    <input name="smtp_password" type="password" class="input mt-1" placeholder="16 caractères">
    <p class="text-[11px] text-slate-400 mt-1">Laisser vide pour conserver le mot de passe actuel.</p></div>
    <div class="md:col-span-2 flex gap-3">
      <button class="btn-primary">Sauvegarder</button>
    </div>
  </form>
  <form method="post" action="/dashboard/test-email" class="mt-3 flex flex-col sm:flex-row gap-2 sm:items-center">
    <input name="test_to" type="email" class="input sm:max-w-xs" placeholder="envoyer un email de test à..." required>
    <button class="text-xs font-semibold text-indigo-600 border border-blue-200 rounded-lg px-4 py-2.5 hover:bg-blue-50 whitespace-nowrap">Tester l'envoi</button>
  </form>
</div>

<div id="sec-branding" class="card p-4 sm:p-6 mb-6 scroll-mt-24">
  <h2 class="font-bold mb-1">Personnalisation des emails</h2>
  <p class="text-sm text-slate-500 mb-4">Couleur, logo et pied de page appliqués à tous vos emails (OTP et transactionnels).</p>
  <form method="post" action="/dashboard/branding" class="grid md:grid-cols-2 gap-4 mb-4">
    <div><label class="text-xs font-semibold text-slate-600">Couleur de marque</label>
    <div class="flex gap-2 mt-1">
      <input name="brand_color" type="color" class="h-10 w-14 rounded-lg border border-slate-200 cursor-pointer" value="{_e(tenant.brand_color)}">
      <input class="input flex-1" value="{_e(tenant.brand_color)}" readonly>
    </div></div>
    <div><label class="text-xs font-semibold text-slate-600">URL du logo (optionnel)</label>
    <input name="email_logo_url" type="url" class="input mt-1" value="{_e(tenant.email_logo_url or '')}" placeholder="https://votresite.com/logo.png">
    <p class="text-[11px] text-slate-400 mt-1">PNG/JPG, fond transparent conseillé, ~180×36 px.</p></div>
    <div class="md:col-span-2"><label class="text-xs font-semibold text-slate-600">Pied de page personnalisé (optionnel)</label>
    <input name="email_footer" class="input mt-1" value="{_e(tenant.email_footer or '')}" placeholder="Ex : © 2026 MaSociété — contact@maboite.com">
    <p class="text-[11px] text-slate-400 mt-1">Vide = « Message automatique de VotreMarque — merci de ne pas répondre. »</p></div>
    <div class="md:col-span-2"><button class="btn-primary">Sauvegarder le branding</button></div>
  </form>
  <p class="text-xs font-semibold text-slate-600 mb-2">Aperçu de votre email OTP :</p>
  <iframe src="/dashboard/email-preview" class="w-full h-96 rounded-xl border border-slate-200 bg-white" title="Aperçu email"></iframe>
</div>

<div id="sec-integration" class="card p-4 sm:p-6 mb-6 scroll-mt-24">
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
  <p class="text-xs text-slate-500 mt-3">Node.js / PHP / cURL : voir <a href="/docs" class="text-indigo-600 underline">la documentation Swagger</a>.</p>
</div>

<div id="sec-support" class="card p-4 sm:p-6 mb-6 scroll-mt-24">
  <h2 class="font-bold mb-1">Contacter le support</h2>
  <p class="text-sm text-slate-500 mb-4">Un problème, une question ? L'équipe admin vous répond.</p>
  <form method="post" action="/dashboard/support" class="grid gap-3">
    <input name="subject" class="input" placeholder="Sujet" maxlength="120" required>
    <textarea name="message" class="input" rows="3" maxlength="2000" placeholder="Décrivez votre problème..." required></textarea>
    <div><button class="btn-outline text-sm">Envoyer la demande</button></div>
  </form>
</div>

<div id="sec-config" class="card p-4 sm:p-6 scroll-mt-24">
  <h2 class="font-bold mb-4">Votre configuration</h2>
  <div class="grid md:grid-cols-2 gap-4 text-sm">
    <div><p class="text-xs font-semibold text-slate-500">Purposes autorisés</p><p class="mt-1">{', '.join(f'<code>{_e(p)}</code>' for p in tenant.allowed_purposes)}</p></div>
    <div><p class="text-xs font-semibold text-slate-500">Rollout OTP</p><p class="font-semibold mt-1">{tenant.rollout_percentage} %</p></div>
    <div><p class="text-xs font-semibold text-slate-500">Pays en fallback SMS</p><p class="mt-1">{_e(', '.join(tenant.risky_countries)) or '—'}</p></div>
    <div><p class="text-xs font-semibold text-slate-500">Expiration OTP</p><p class="font-semibold mt-1">{tenant.otp_ttl_seconds} s</p></div>
  </div>
</div>
"""
    return _dashboard_page(user, tenant, banner + content, debug=settings.demo_enabled)


def _cfg(tenant: Tenant, key: str):
    return getattr(tenant, key, None)


@router.post("/dashboard/config")
async def dashboard_config(
    request: Request,
    brand_name: str = Form(...),
    from_email: str = Form(""),
    from_name: str = Form(""),
    smtp_host: str = Form(""),
    smtp_port: str = Form(""),
    smtp_user: str = Form(""),
    smtp_password: str = Form(""),
    _: None = Depends(require_same_origin),
):
    user = await _require_user(request)
    if from_email.strip() and (
        "@" not in from_email or "." not in from_email.rsplit("@", 1)[-1]
    ):
        raise HTTPException(status_code=422, detail="Email expéditeur invalide")
    port: int | None = None
    if smtp_port.strip():
        try:
            port = int(smtp_port.strip())
        except ValueError:
            raise HTTPException(status_code=422, detail="Port SMTP invalide") from None
        if not 1 <= port <= 65535:
            raise HTTPException(status_code=422, detail="Port SMTP invalide")
    store = TenantStore(request.app.state.redis)
    updates = {
        "brand_name": brand_name.strip() or "SendBID",
        "from_email": from_email.strip() or None,
        "from_name": from_name.strip() or None,
        "smtp_host": smtp_host.strip() or None,
        "smtp_port": port,
        "smtp_user": smtp_user.strip() or None,
    }
    if smtp_password.strip():
        updates["smtp_password"] = smtp_password.strip()
    await store.update(user.tenant_id, updates)
    return RedirectResponse("/dashboard", status_code=303)


@router.post("/dashboard/branding")
async def dashboard_branding(
    request: Request,
    brand_color: str = Form("#6366f1"),
    email_logo_url: str = Form(""),
    email_footer: str = Form(""),
    _: None = Depends(require_same_origin),
):
    user = await _require_user(request)
    color = brand_color.strip()
    if not (
        len(color) == 7
        and color.startswith("#")
        and all(c in "0123456789abcdefABCDEF" for c in color[1:])
    ):
        raise HTTPException(status_code=422, detail="Couleur invalide (format #rrggbb)")
    logo = email_logo_url.strip()
    if logo and not logo.startswith(("https://", "http://")):
        raise HTTPException(status_code=422, detail="URL de logo invalide")
    await TenantStore(request.app.state.redis).update(
        user.tenant_id,
        {
            "brand_color": color,
            "email_logo_url": logo or None,
            "email_footer": email_footer.strip() or None,
        },
    )
    return RedirectResponse("/dashboard", status_code=303)


@router.post("/dashboard/support")
async def dashboard_support(
    request: Request,
    subject: str = Form(...),
    message: str = Form(...),
    _: None = Depends(require_same_origin),
):
    user = await _require_user(request)
    redis = request.app.state.redis
    from datetime import datetime, timezone
    req_id = f"sup_{uuid.uuid4().hex[:12]}"
    await redis.set(
        f"support:{req_id}",
        json.dumps(
            {
                "id": req_id,
                "tenant_id": user.tenant_id,
                "email": user.email,
                "app_name": user.app_name,
                "subject": subject.strip()[:120],
                "message": message.strip()[:2000],
                "status": "open",
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
        ),
        ex=2592000,
    )
    await redis.lpush("support:open", req_id)
    return RedirectResponse("/dashboard", status_code=303)


@router.get("/dashboard/email-preview", response_class=HTMLResponse)
async def dashboard_email_preview(request: Request):
    user = await _require_user(request)
    tenant = await TenantStore(request.app.state.redis).get(user.tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant introuvable")
    return HTMLResponse(
        otp_html(
            "482915",
            tenant.brand_name,
            "fr",
            ttl_minutes=max(1, round(tenant.otp_ttl_seconds / 60)),
            color=tenant.brand_color,
            logo_url=tenant.email_logo_url,
            footer=tenant.email_footer,
        )
    )


@router.post("/dashboard/regenerate-keys")
async def dashboard_regenerate(
    request: Request, _: None = Depends(require_same_origin)
):
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
    await redis.set(
        f"webuser:creds:{tenant.tenant_id}", f"{new_key}|{new_secret}", ex=86400
    )
    return RedirectResponse("/dashboard", status_code=303)


@router.post("/dashboard/plan")
async def dashboard_plan(
    request: Request,
    plan: str = Form(...),
    _: None = Depends(require_same_origin),
):
    user = await _require_user(request)
    if plan not in PLANS:
        raise HTTPException(status_code=422, detail="Plan invalide")
    redis = request.app.state.redis
    if plan == "free":
        # Le plan gratuit est appliqué immédiatement (rien à payer)
        await TenantStore(redis).update(
            user.tenant_id,
            {"plan": plan, "plan_requested": None, **plan_quota_updates(plan)},
        )
        return RedirectResponse("/dashboard", status_code=303)
    # Plan payant → demande en attente : l'admin valide le paiement
    # (manuel ou webhook Stripe/PayPal/mobile money) puis applique le plan.
    await TenantStore(redis).update(user.tenant_id, {"plan_requested": plan})
    return RedirectResponse("/dashboard?plan_requested=" + plan, status_code=303)


@router.post("/dashboard/test-email")
async def dashboard_test_email(
    request: Request,
    test_to: str = Form(...),
    settings: Settings = Depends(get_settings),
    _: None = Depends(require_same_origin),
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
    try:
        await service.send(tenant, req)
    except HTTPException as exc:
        return RedirectResponse(
            f"/dashboard?test={quote(str(exc.detail)[:120])}", status_code=303
        )
    except Exception:
        return RedirectResponse("/dashboard?test=erreur%20d%27envoi", status_code=303)
    return RedirectResponse("/dashboard?test=ok", status_code=303)


# =====================================================================
# Démo interactive (accessible sans compte en DEBUG)
# =====================================================================

def _demo_page(default_tenant: str = "default") -> str:
    return """
<div class="max-w-4xl mx-auto py-10 px-6">
  <h1 class="text-2xl font-extrabold mb-1">Console de test</h1>
  <p class="text-sm text-slate-500 mb-8">Testez les endpoints en direct. Les emails sont loggés côté serveur (provider console).</p>

  <div class="card p-4 sm:p-6 mb-6">
    <h2 class="font-bold mb-4">1. Envoyer un OTP</h2>
    <div class="grid md:grid-cols-2 gap-4">
      <div><label class="text-xs font-semibold text-slate-600">Tenant <span class="font-normal text-slate-400">(id, nom ou marque)</span></label>
      <input id="otpTenant" class="input mt-1" value="__TENANT__"></div>
      <div><label class="text-xs font-semibold text-slate-600">Email</label>
      <input id="email" type="email" class="input mt-1" value="test@example.com"></div>
      <div><label class="text-xs font-semibold text-slate-600">Code pays</label>
      <input id="country" class="input mt-1" value="FR" maxlength="2"></div>
      <div><label class="text-xs font-semibold text-slate-600">Langue</label>
      <select id="language" class="input mt-1"><option value="fr">Français</option><option value="en">English</option></select></div>
    </div>
    <button onclick="sendOtp()" class="btn-primary mt-4">Envoyer l'OTP</button>
    <pre id="sendResult" class="mt-4 text-xs bg-slate-900 text-emerald-300 rounded-lg p-4 hidden overflow-auto"></pre>
    <div id="otpCode" class="mt-3 text-center text-3xl font-extrabold tracking-[.4em] text-indigo-600 hidden"></div>
  </div>

  <div class="card p-4 sm:p-6 mb-6">
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
      <input id="emailTenant" class="input mt-1" value="__TENANT__"></div>
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
  const data = await res.json();
  show("verifyResult", data, res.ok && data.verified);
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
""".replace("__TENANT__", _e(default_tenant))


@router.get("/demo", response_class=HTMLResponse)
async def demo(request: Request, settings: Settings = Depends(get_settings)) -> HTMLResponse:
    user = await _current_user(request)
    # Démo publique désactivée ? La console reste ouverte aux clients connectés.
    if not settings.demo_enabled and not user:
        raise HTTPException(status_code=404)
    # Connecté : pré-remplir le champ Tenant avec SON tenant pour que les
    # envois de test alimentent ses stats du dashboard (pas "default").
    default_tenant = user.tenant_id if user else "default"
    return _landing_page(
        f'<div class="bg-slate-50">{_demo_page(default_tenant)}</div>',
        logged_in=bool(user),
    )


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
    """Tenant ciblé par la démo — restreint pour éviter l'abus inter-tenants :
    anonyme → 'default' uniquement ; connecté → son propre tenant ou 'default'.
    Un paramètre tenant_id libre laissait n'importe qui consommer le quota et
    le SMTP d'un autre client."""
    allowed = {"default"}
    user = await _current_user(request)
    if user:
        allowed.add(user.tenant_id)
    if tenant_id not in allowed:
        tenant_id = user.tenant_id if user else "default"
    store = TenantStore(request.app.state.redis)
    tenant = await store.get(tenant_id)
    if tenant is None or not tenant.is_active:
        raise HTTPException(status_code=404, detail=f"Tenant '{tenant_id}' not found")
    return tenant


@router.post("/demo/send", include_in_schema=False)
async def demo_send(
    payload: DemoSendRequest,
    request: Request,
    service: OTPService = Depends(get_otp_service),
    settings: Settings = Depends(get_settings),
    _: None = Depends(require_same_origin),
):
    if not settings.demo_enabled and not await _current_user(request):
        raise HTTPException(status_code=404)
    tenant = await _demo_tenant(request, payload.tenant_id)
    response = await service.send_otp(
        tenant=tenant,
        user_id=str(payload.email),
        email=str(payload.email),
        country_code=payload.country_code,
        purpose=payload.purpose,
        language=payload.language,
        client_ip=get_client_ip(request),
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
    _: None = Depends(require_same_origin),
):
    if not settings.demo_enabled and not await _current_user(request):
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
    _: None = Depends(require_same_origin),
):
    if not settings.demo_enabled and not await _current_user(request):
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
    return await service.send(tenant, req, client_ip=get_client_ip(request))
