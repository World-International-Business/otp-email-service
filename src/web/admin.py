"""Espace administrateur web — gestion des tenants et supervision.

Authentification : la clé maître `ADMIN_API_KEY` saisie sur /admin/login
ouvre une session (token Redis, cookie HttpOnly, TTL 12 h).

Pages :
  /admin                    -> dashboard : tenants, stats, journal d'événements
  /admin/tenants/new        -> créer un tenant (clés affichées une seule fois)
  /admin/tenants/{id}       -> éditer la config / les quotas d'un tenant
  /admin/tenants/{id}/toggle -> activer / désactiver un tenant
"""

import hmac
import html
import secrets

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.config import Settings, get_settings
from src.core.tenants import (
    Tenant,
    TenantStore,
    generate_api_key,
    generate_hmac_secret,
    hash_api_key,
)
from src.services.events import get_events
from src.web.routes import LOGO_SVG, _head
from src.web.users import WebUserStore

router = APIRouter(prefix="/admin", tags=["admin-web"], include_in_schema=False)

ADMIN_COOKIE = "admin_session"
ADMIN_SESSION_TTL = 12 * 3600  # 12 h

_EVENT_LABELS = {
    "otp_sent": ("OTP envoyé", "bg-emerald-100 text-emerald-700"),
    "otp_verified": ("OTP vérifié", "bg-green-100 text-green-700"),
    "otp_verify_failed": ("Vérif. échouée", "bg-rose-100 text-rose-700"),
    "otp_rate_limited": ("Rate limit", "bg-amber-100 text-amber-700"),
    "otp_purpose_denied": ("Purpose refusé", "bg-rose-100 text-rose-700"),
    "otp_sms_fallback": ("Fallback SMS", "bg-orange-100 text-orange-700"),
    "otp_send_failed": ("Échec envoi OTP", "bg-rose-100 text-rose-700"),
    "email_sent": ("Email envoyé", "bg-blue-100 text-blue-700"),
    "email_failed": ("Échec email", "bg-rose-100 text-rose-700"),
}


def _e(value) -> str:
    return html.escape(str(value), quote=True)


def _admin_page(title: str, content: str) -> HTMLResponse:
    return HTMLResponse(f"""{_head(title)}
<body class="bg-slate-100 text-slate-800 min-h-screen">
<nav class="bg-slate-900 text-white sticky top-0 z-20">
  <div class="max-w-7xl mx-auto px-4 sm:px-6 h-14 flex items-center justify-between">
    <a href="/admin" class="flex items-center gap-2.5 font-extrabold">{LOGO_SVG}
      MailHub <span class="badge bg-rose-500 text-white">ADMIN</span>
    </a>
    <div class="flex items-center gap-4 text-sm">
      <a href="/admin" class="text-slate-300 hover:text-white font-medium">Tenants</a>
      <a href="/admin/tenants/new" class="text-slate-300 hover:text-white font-medium">Nouveau tenant</a>
      <a href="/" class="text-slate-300 hover:text-white font-medium">Site</a>
      <a href="/admin/logout" class="text-rose-300 hover:text-rose-200 font-medium">Déconnexion</a>
    </div>
  </div>
</nav>
<main class="max-w-7xl mx-auto px-4 sm:px-6 py-8">{content}</main>
</body></html>""")


# ---------------------------------------------------------------------
# Authentification admin
# ---------------------------------------------------------------------

async def _require_admin(request: Request, settings: Settings) -> None:
    token = request.cookies.get(ADMIN_COOKIE)
    if not token:
        raise HTTPException(status_code=303, headers={"Location": "/admin/login"})
    stored = await request.app.state.redis.get(f"adminsession:{token}")
    if not stored or not hmac.compare_digest(stored, settings.admin_api_key):
        raise HTTPException(status_code=303, headers={"Location": "/admin/login"})


@router.get("/login", response_class=HTMLResponse)
async def admin_login_page(request: Request, error: str = "") -> HTMLResponse:
    err = (
        '<p class="text-sm text-red-600 bg-red-50 rounded-lg p-3 mb-4">'
        "Clé administrateur invalide.</p>"
        if error else ""
    )
    return HTMLResponse(f"""{_head("Administration")}
<body class="bg-slate-900 min-h-screen flex items-center justify-center px-4">
  <div class="w-full max-w-sm">
    <div class="text-center mb-6">
      <a href="/" class="inline-flex items-center gap-2 font-extrabold text-lg text-white">
        {LOGO_SVG} MailHub
      </a>
    </div>
    <div class="card p-6 sm:p-8 shadow-xl">
      <h1 class="text-xl font-extrabold mb-1">Espace administrateur</h1>
      <p class="text-sm text-slate-500 mb-6">Réservé aux administrateurs de la plateforme.</p>
      {err}
      <form method="post" action="/admin/login" class="space-y-4">
        <div><label class="text-xs font-semibold text-slate-600">Clé administrateur</label>
        <input name="admin_key" type="password" class="input mt-1" required autofocus
               placeholder="ADMIN_API_KEY"></div>
        <button class="btn-primary w-full">Se connecter</button>
      </form>
    </div>
    <p class="text-center text-xs text-slate-500 mt-5">
      La clé se trouve dans les variables d'environnement du serveur (ADMIN_API_KEY).
    </p>
  </div>
</body></html>""")


@router.post("/login")
async def admin_login(
    request: Request,
    admin_key: str = Form(...),
    settings: Settings = Depends(get_settings),
):
    if not hmac.compare_digest(admin_key, settings.admin_api_key):
        return RedirectResponse("/admin/login?error=1", status_code=303)

    token = secrets.token_urlsafe(32)
    await request.app.state.redis.setex(
        f"adminsession:{token}", ADMIN_SESSION_TTL, settings.admin_api_key
    )
    resp = RedirectResponse("/admin", status_code=303)
    resp.set_cookie(
        ADMIN_COOKIE, token, max_age=ADMIN_SESSION_TTL,
        httponly=True, samesite="lax",
    )
    return resp


@router.get("/logout")
async def admin_logout(request: Request):
    token = request.cookies.get(ADMIN_COOKIE)
    if token:
        await request.app.state.redis.delete(f"adminsession:{token}")
    resp = RedirectResponse("/admin/login", status_code=303)
    resp.delete_cookie(ADMIN_COOKIE)
    return resp


# ---------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------

async def _tenant_stats(redis, tenant: Tenant) -> dict[str, str]:
    pipe = redis.pipeline()
    for key in ("stats:emails_sent", "stats:otps_sent", "stats:otps_verified"):
        pipe.get(f"{tenant.key_prefix()}{key}")
    emails, sent, verified = await pipe.execute()
    return {
        "emails": emails or "0", "otps": sent or "0", "verified": verified or "0",
    }


@router.get("", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request, settings: Settings = Depends(get_settings)
) -> HTMLResponse:
    await _require_admin(request, settings)
    redis = request.app.state.redis
    tenants = await TenantStore(redis).list()

    rows = ""
    all_events: list[dict] = []
    for t in tenants:
        stats = await _tenant_stats(redis, t)
        all_events.extend(
            {**ev, "tenant_id": t.tenant_id} for ev in await get_events(redis, t, 30)
        )
        status_badge = (
            '<span class="badge bg-emerald-100 text-emerald-700">ACTIF</span>'
            if t.is_active else
            '<span class="badge bg-rose-100 text-rose-700">DÉSACTIVÉ</span>'
        )
        toggle_label = "Désactiver" if t.is_active else "Réactiver"
        toggle_cls = (
            "text-rose-600 border-rose-200 hover:bg-rose-50" if t.is_active
            else "text-emerald-600 border-emerald-200 hover:bg-emerald-50"
        )
        rows += f"""<tr class="hover:bg-slate-50 transition align-top">
  <td class="py-4 pl-5 pr-4">
    <p class="font-bold">{_e(t.name)}</p>
    <p class="text-xs text-slate-400"><code>{_e(t.tenant_id)}</code> · {_e(t.brand_name)}</p>
  </td>
  <td class="py-4 pr-4">{status_badge}</td>
  <td class="py-4 pr-4 text-sm">
    <span class="font-semibold">{stats['otps']}</span> OTP ·
    <span class="font-semibold">{stats['verified']}</span> vérifiés ·
    <span class="font-semibold">{stats['emails']}</span> emails
  </td>
  <td class="py-4 pr-4 text-xs text-slate-500">
    {t.max_sends_per_user_per_10min}/user · {t.max_sends_per_ip_per_10min}/ip ·
    cooldown {t.cooldown_seconds}s · {t.max_verify_attempts} essais · TTL {t.otp_ttl_seconds}s<br>
    rollout {t.rollout_percentage}% · {len(t.allowed_purposes)} purpose(s)
  </td>
  <td class="py-4 pr-5 whitespace-nowrap text-right">
    <a href="/admin/tenants/{_e(t.tenant_id)}"
       class="text-xs font-semibold text-indigo-600 border border-indigo-200 rounded-lg px-3 py-1.5 hover:bg-indigo-50">Gérer</a>
    <form method="post" action="/admin/tenants/{_e(t.tenant_id)}/toggle" class="inline"
          onsubmit="return confirm('Confirmer ?')">
      <button class="text-xs font-semibold {toggle_cls} border rounded-lg px-3 py-1.5 ml-1">{toggle_label}</button>
    </form>
  </td>
</tr>"""

    if not rows:
        rows = ('<tr><td colspan="5" class="py-10 text-center text-slate-400">'
                "Aucun tenant — créez le premier.</td></tr>")

    all_events.sort(key=lambda e: e.get("ts", ""), reverse=True)
    event_rows = ""
    for ev in all_events[:50]:
        label, cls = _EVENT_LABELS.get(
            ev.get("type", ""), (ev.get("type", "?"), "bg-slate-100 text-slate-600")
        )
        details = " · ".join(
            f"{k}={_e(v)}" for k, v in ev.items()
            if k not in ("ts", "type", "tenant_id") and v not in (None, "")
        )
        event_rows += f"""<tr class="border-b border-slate-50 last:border-0">
  <td class="py-2.5 pl-5 pr-4 text-xs text-slate-400 whitespace-nowrap">{_e(ev.get('ts',''))}</td>
  <td class="py-2.5 pr-4"><code class="text-xs">{_e(ev.get('tenant_id',''))}</code></td>
  <td class="py-2.5 pr-4"><span class="badge {cls}">{_e(label)}</span></td>
  <td class="py-2.5 pr-5 text-xs text-slate-500">{details}</td>
</tr>"""
    if not event_rows:
        event_rows = ('<tr><td colspan="4" class="py-8 text-center text-slate-400">'
                      "Aucune interaction enregistrée.</td></tr>")

    content = f"""
<div class="flex items-center justify-between mb-6">
  <div>
    <h1 class="text-2xl font-extrabold">Administration</h1>
    <p class="text-sm text-slate-500">{len(tenants)} tenant(s) — supervision et gestion des accès API.</p>
  </div>
  <a href="/admin/tenants/new" class="btn-primary text-sm">+ Nouveau tenant</a>
</div>

<div class="card overflow-hidden mb-8">
  <div class="px-5 py-4 border-b border-slate-100"><h2 class="font-bold">Tenants</h2></div>
  <div class="overflow-x-auto">
  <table class="w-full text-sm min-w-[860px]">
    <thead><tr class="text-left text-slate-400 text-xs uppercase tracking-wider border-b bg-slate-50">
      <th class="py-3 pl-5 pr-4 font-semibold">Tenant</th>
      <th class="py-3 pr-4 font-semibold">Statut</th>
      <th class="py-3 pr-4 font-semibold">Statistiques</th>
      <th class="py-3 pr-4 font-semibold">Limites</th>
      <th class="py-3 pr-5 font-semibold"></th>
    </tr></thead>
    <tbody class="divide-y divide-slate-100">{rows}</tbody>
  </table>
  </div>
</div>

<div class="card overflow-hidden">
  <div class="px-5 py-4 border-b border-slate-100">
    <h2 class="font-bold">Journal des interactions</h2>
    <p class="text-xs text-slate-500 mt-0.5">50 derniers événements, tous tenants confondus.</p>
  </div>
  <div class="overflow-x-auto">
  <table class="w-full min-w-[720px]">
    <tbody>{event_rows}</tbody>
  </table>
  </div>
</div>"""
    return _admin_page("Administration", content)


# ---------------------------------------------------------------------
# Création / édition de tenant
# ---------------------------------------------------------------------

def _tenant_form(action: str, t: Tenant | None, include_account: bool) -> str:
    """Formulaire de création/édition — valeurs préremplies si `t` fourni."""
    g = lambda k, d="": _e(getattr(t, k, d) if t else d)
    risks = _e(",".join(t.risky_countries) if t else "")
    purposes = _e(",".join(t.allowed_purposes) if t else "login_2fa")
    smtp_pass = ""  # jamais prérempli

    id_field = ""
    if t is None:
        id_field = """
    <div><label class="text-xs font-semibold text-slate-600">Tenant ID *</label>
    <input name="tenant_id" class="input mt-1" required pattern="[a-z0-9_-]+"
           placeholder="sendbid" title="minuscules, chiffres, - et _"></div>"""

    account = ""
    if include_account:
        account = """
  <h3 class="font-bold text-sm text-slate-700 md:col-span-2 mt-2">Compte dashboard du client <span class="font-normal text-slate-400">(optionnel — lui donne accès à /dashboard)</span></h3>
  <div><label class="text-xs font-semibold text-slate-600">Email du client</label>
  <input name="user_email" type="email" class="input mt-1" placeholder="client@exemple.com"></div>
  <div><label class="text-xs font-semibold text-slate-600">Mot de passe du client</label>
  <input name="user_password" type="text" class="input mt-1" minlength="8" placeholder="8 caractères min"></div>"""

    unlimited_hint = "" if t else """
  <p class="md:col-span-2 text-xs text-indigo-600 bg-indigo-50 rounded-lg p-3">
    Valeurs par défaut = <strong>accès quasi illimité</strong>. Réduisez les
    champs pour un client limité (ex. 3 envois/10 min, cooldown 60 s).</p>"""

    return f"""
<form method="post" action="{action}" class="grid md:grid-cols-2 gap-4">
  {id_field}
  <div><label class="text-xs font-semibold text-slate-600">Nom affiché *</label>
  <input name="name" class="input mt-1" required value="{g('name')}"></div>
  <div><label class="text-xs font-semibold text-slate-600">Marque (dans les emails)</label>
  <input name="brand_name" class="input mt-1" value="{g('brand_name', 'SendBID')}"></div>
  <div><label class="text-xs font-semibold text-slate-600">Email expéditeur</label>
  <input name="from_email" type="email" class="input mt-1" value="{g('from_email')}" placeholder="no-reply@client.com"></div>
  <div><label class="text-xs font-semibold text-slate-600">Nom expéditeur</label>
  <input name="from_name" class="input mt-1" value="{g('from_name')}"></div>

  <h3 class="font-bold text-sm text-slate-700 md:col-span-2 mt-2">SMTP du client <span class="font-normal text-slate-400">(vide = config globale du serveur ; IONOS : smtp.ionos.com:587)</span></h3>
  <div><label class="text-xs font-semibold text-slate-600">SMTP Host</label>
  <input name="smtp_host" class="input mt-1" value="{g('smtp_host')}" placeholder="smtp.ionos.com"></div>
  <div><label class="text-xs font-semibold text-slate-600">SMTP Port</label>
  <input name="smtp_port" type="number" class="input mt-1" value="{g('smtp_port')}" placeholder="587"></div>
  <div><label class="text-xs font-semibold text-slate-600">SMTP User</label>
  <input name="smtp_user" class="input mt-1" value="{g('smtp_user')}" placeholder="email@domaine.com"></div>
  <div><label class="text-xs font-semibold text-slate-600">SMTP Password{'(vide = inchangé)' if t else ''}</label>
  <input name="smtp_password" type="password" class="input mt-1" value="{smtp_pass}"></div>

  <h3 class="font-bold text-sm text-slate-700 md:col-span-2 mt-2">Règles métier</h3>
  <div><label class="text-xs font-semibold text-slate-600">Purposes autorisés (virgules)</label>
  <input name="allowed_purposes" class="input mt-1" value="{purposes}"></div>
  <div><label class="text-xs font-semibold text-slate-600">Pays → fallback SMS (virgules, vide = aucun)</label>
  <input name="risky_countries" class="input mt-1" value="{risks}" placeholder="CN,RU"></div>
  <div><label class="text-xs font-semibold text-slate-600">Rollout %</label>
  <input name="rollout_percentage" type="number" min="0" max="100" class="input mt-1"
         value="{g('rollout_percentage', 100)}"></div>

  <h3 class="font-bold text-sm text-slate-700 md:col-span-2 mt-2">Quotas</h3>
  {unlimited_hint}
  <div><label class="text-xs font-semibold text-slate-600">Envois max / user / 10 min</label>
  <input name="max_sends_per_user_per_10min" type="number" min="1" class="input mt-1"
         value="{g('max_sends_per_user_per_10min', 1000000)}"></div>
  <div><label class="text-xs font-semibold text-slate-600">Envois max / IP / 10 min</label>
  <input name="max_sends_per_ip_per_10min" type="number" min="1" class="input mt-1"
         value="{g('max_sends_per_ip_per_10min', 1000000)}"></div>
  <div><label class="text-xs font-semibold text-slate-600">Cooldown entre envois (s, min 1)</label>
  <input name="cooldown_seconds" type="number" min="1" class="input mt-1"
         value="{g('cooldown_seconds', 1)}"></div>
  <div><label class="text-xs font-semibold text-slate-600">Tentatives de vérification max</label>
  <input name="max_verify_attempts" type="number" min="1" class="input mt-1"
         value="{g('max_verify_attempts', 1000000)}"></div>
  <div><label class="text-xs font-semibold text-slate-600">Validité du code OTP (s)</label>
  <input name="otp_ttl_seconds" type="number" min="30" class="input mt-1"
         value="{g('otp_ttl_seconds', 600)}"></div>

  {account}
  <div class="md:col-span-2 flex gap-3 mt-2">
    <button class="btn-primary">{'Créer le tenant' if t is None else 'Sauvegarder'}</button>
    <a href="/admin" class="btn-outline">Annuler</a>
  </div>
</form>"""


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def _int_or_none(value: str) -> int | None:
    value = (value or "").strip()
    return int(value) if value else None


@router.get("/tenants/new", response_class=HTMLResponse)
async def admin_new_tenant(
    request: Request, settings: Settings = Depends(get_settings)
) -> HTMLResponse:
    await _require_admin(request, settings)
    content = f"""
<h1 class="text-2xl font-extrabold mb-1">Nouveau tenant</h1>
<p class="text-sm text-slate-500 mb-8">La clé API et le secret HMAC seront affichés
<strong>une seule fois</strong> après création.</p>
<div class="card p-4 sm:p-6">{_tenant_form('/admin/tenants', None, include_account=True)}</div>"""
    return _admin_page("Nouveau tenant", content)


@router.post("/tenants", response_class=HTMLResponse)
async def admin_create_tenant(
    request: Request,
    settings: Settings = Depends(get_settings),
    tenant_id: str = Form(...),
    name: str = Form(...),
    brand_name: str = Form("SendBID"),
    from_email: str = Form(""),
    from_name: str = Form(""),
    smtp_host: str = Form(""),
    smtp_port: str = Form(""),
    smtp_user: str = Form(""),
    smtp_password: str = Form(""),
    allowed_purposes: str = Form("login_2fa"),
    risky_countries: str = Form(""),
    rollout_percentage: int = Form(100),
    max_sends_per_user_per_10min: int = Form(1000000),
    max_sends_per_ip_per_10min: int = Form(1000000),
    cooldown_seconds: int = Form(1),
    max_verify_attempts: int = Form(1000000),
    otp_ttl_seconds: int = Form(600),
    user_email: str = Form(""),
    user_password: str = Form(""),
) -> HTMLResponse:
    await _require_admin(request, settings)
    redis = request.app.state.redis
    store = TenantStore(redis)
    tenant_id = tenant_id.strip().lower()

    if await store.get(tenant_id):
        raise HTTPException(status_code=409, detail=f"Tenant '{tenant_id}' existe déjà")

    api_key = generate_api_key()
    hmac_secret = generate_hmac_secret()
    tenant = Tenant(
        tenant_id=tenant_id,
        name=name.strip(),
        api_key_hash=hash_api_key(api_key),
        hmac_secret=hmac_secret,
        brand_name=brand_name.strip() or "SendBID",
        from_email=from_email.strip() or None,
        from_name=from_name.strip() or None,
        smtp_host=smtp_host.strip() or None,
        smtp_port=_int_or_none(smtp_port),
        smtp_user=smtp_user.strip() or None,
        smtp_password=smtp_password.strip() or None,
        risky_countries=[c.upper() for c in _csv(risky_countries)],
        rollout_percentage=max(0, min(100, rollout_percentage)),
        allowed_purposes=_csv(allowed_purposes) or ["login_2fa"],
        max_sends_per_user_per_10min=max(1, max_sends_per_user_per_10min),
        max_sends_per_ip_per_10min=max(1, max_sends_per_ip_per_10min),
        cooldown_seconds=max(1, cooldown_seconds),
        max_verify_attempts=max(1, max_verify_attempts),
        otp_ttl_seconds=max(30, otp_ttl_seconds),
        is_active=True,
    )
    await store.create(tenant)

    account_msg = ""
    if user_email.strip() and user_password.strip():
        users = WebUserStore(redis)
        if await users.get_by_email(user_email.strip()):
            account_msg = (
                '<p class="mt-3 text-sm text-amber-600 bg-amber-50 rounded-lg p-3">'
                f"L'email {_e(user_email)} a déjà un compte dashboard — "
                "tenant non lié.</p>"
            )
        else:
            await users.create(user_email.strip(), user_password, tenant_id, name.strip())
            await redis.setex(
                f"webuser:creds:{tenant_id}", 86400, f"{api_key}|{hmac_secret}"
            )
            account_msg = (
                '<p class="mt-3 text-sm text-emerald-700 bg-emerald-50 rounded-lg p-3">'
                f"Compte dashboard créé : <code>{_e(user_email)}</code> — "
                "il peut se connecter sur /login.</p>"
            )

    content = f"""
<div class="max-w-2xl mx-auto">
  <div class="card p-6 sm:p-8">
    <div class="w-12 h-12 rounded-full bg-emerald-100 text-emerald-600 flex items-center justify-center mb-4">
      <svg class="w-6 h-6" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24">
        <path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/></svg>
    </div>
    <h1 class="text-xl font-extrabold">Tenant <code>{_e(tenant_id)}</code> créé</h1>
    <p class="text-sm text-rose-600 bg-rose-50 rounded-lg p-3 mt-3 font-semibold">
      Copiez ces clés maintenant — elles ne seront plus affichées ensuite.</p>
    <div class="mt-4 space-y-3">
      <div><p class="text-xs font-semibold text-slate-500 mb-1">Clé API (X-API-Key)</p>
      <code class="block text-xs break-all bg-slate-50 p-3 rounded">{_e(api_key)}</code></div>
      <div><p class="text-xs font-semibold text-slate-500 mb-1">Secret HMAC (signature des requêtes)</p>
      <code class="block text-xs break-all bg-slate-50 p-3 rounded">{_e(hmac_secret)}</code></div>
    </div>
    {account_msg}
    <div class="mt-6 flex gap-3">
      <a href="/admin" class="btn-primary text-sm">Retour au dashboard</a>
      <a href="/admin/tenants/{_e(tenant_id)}" class="btn-outline text-sm">Modifier</a>
    </div>
  </div>
</div>"""
    return _admin_page("Tenant créé", content)


# ---------------------------------------------------------------------
# Édition + activation
# ---------------------------------------------------------------------

@router.get("/tenants/{tenant_id}", response_class=HTMLResponse)
async def admin_edit_tenant(
    tenant_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
    saved: str = "",
) -> HTMLResponse:
    await _require_admin(request, settings)
    tenant = await TenantStore(request.app.state.redis).get(tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant introuvable")

    flash = (
        '<p class="text-sm text-emerald-700 bg-emerald-50 rounded-lg p-3 mb-4">'
        "Configuration enregistrée.</p>" if saved else ""
    )
    info = f"""
<div class="grid md:grid-cols-4 gap-4 mb-6 text-sm">
  <div class="card p-4"><p class="text-xs font-semibold text-slate-500">Statut</p>
    <p class="font-bold mt-1">{'<span class="text-emerald-600">Actif</span>' if tenant.is_active else '<span class="text-rose-600">Désactivé</span>'}</p></div>
  <div class="card p-4"><p class="text-xs font-semibold text-slate-500">Rollout</p>
    <p class="font-bold mt-1">{tenant.rollout_percentage} %</p></div>
  <div class="card p-4"><p class="text-xs font-semibold text-slate-500">TTL OTP</p>
    <p class="font-bold mt-1">{tenant.otp_ttl_seconds} s</p></div>
  <div class="card p-4"><p class="text-xs font-semibold text-slate-500">SMTP</p>
    <p class="font-bold mt-1 truncate">{_e(tenant.smtp_host or 'global')}</p></div>
</div>"""
    content = f"""
<div class="flex items-center justify-between mb-6">
  <div>
    <h1 class="text-2xl font-extrabold">Tenant <code>{_e(tenant_id)}</code></h1>
    <p class="text-sm text-slate-500">Configuration, quotas et SMTP.</p>
  </div>
  <form method="post" action="/admin/tenants/{_e(tenant_id)}/toggle"
        onsubmit="return confirm('Confirmer ?')">
    <button class="text-xs font-semibold {'text-rose-600 border-rose-200 hover:bg-rose-50' if tenant.is_active else 'text-emerald-600 border-emerald-200 hover:bg-emerald-50'} border rounded-lg px-4 py-2">
      {'Désactiver' if tenant.is_active else 'Réactiver'}</button>
  </form>
</div>
{flash}
{info}
<div class="card p-4 sm:p-6">
  {_tenant_form(f'/admin/tenants/{_e(tenant_id)}', tenant, include_account=False)}
</div>"""
    return _admin_page(f"Tenant {tenant_id}", content)


def _opt(value: str) -> str | None:
    value = (value or "").strip()
    return value or None


@router.post("/tenants/{tenant_id}")
async def admin_update_tenant(
    tenant_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
    name: str = Form(...),
    brand_name: str = Form("SendBID"),
    from_email: str = Form(""),
    from_name: str = Form(""),
    smtp_host: str = Form(""),
    smtp_port: str = Form(""),
    smtp_user: str = Form(""),
    smtp_password: str = Form(""),
    allowed_purposes: str = Form("login_2fa"),
    risky_countries: str = Form(""),
    rollout_percentage: int = Form(100),
    max_sends_per_user_per_10min: int = Form(1000000),
    max_sends_per_ip_per_10min: int = Form(1000000),
    cooldown_seconds: int = Form(1),
    max_verify_attempts: int = Form(1000000),
    otp_ttl_seconds: int = Form(600),
):
    await _require_admin(request, settings)
    updates = {
        "name": name.strip(),
        "brand_name": brand_name.strip() or "SendBID",
        "from_email": _opt(from_email),
        "from_name": _opt(from_name),
        "smtp_host": _opt(smtp_host),
        "smtp_user": _opt(smtp_user),
        "risky_countries": [c.upper() for c in _csv(risky_countries)],
        "rollout_percentage": max(0, min(100, rollout_percentage)),
        "allowed_purposes": _csv(allowed_purposes) or ["login_2fa"],
        "max_sends_per_user_per_10min": max(1, max_sends_per_user_per_10min),
        "max_sends_per_ip_per_10min": max(1, max_sends_per_ip_per_10min),
        "cooldown_seconds": max(1, cooldown_seconds),
        "max_verify_attempts": max(1, max_verify_attempts),
        "otp_ttl_seconds": max(30, otp_ttl_seconds),
    }
    port = _int_or_none(smtp_port)
    if port is not None:
        updates["smtp_port"] = port
    if smtp_password.strip():
        updates["smtp_password"] = smtp_password.strip()

    # Les champs optionnels vides restent inchangés (store.update ignore None)
    tenant = await TenantStore(request.app.state.redis).update(tenant_id, updates)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant introuvable")
    return RedirectResponse(f"/admin/tenants/{tenant_id}?saved=1", status_code=303)


@router.post("/tenants/{tenant_id}/toggle")
async def admin_toggle_tenant(
    tenant_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
):
    await _require_admin(request, settings)
    store = TenantStore(request.app.state.redis)
    tenant = await store.get(tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant introuvable")
    await store.update(tenant_id, {"is_active": not tenant.is_active})
    return RedirectResponse("/admin", status_code=303)
