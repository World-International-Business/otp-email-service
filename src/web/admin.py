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
import json
import re
import secrets

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.api.dependencies import get_client_ip, require_same_origin
from src.config import Settings, get_settings
from src.core.tenants import (
    PLANS,
    Tenant,
    TenantStore,
    generate_api_key,
    generate_hmac_secret,
    hash_api_key,
    plan_quota_updates,
)
from src.services.events import get_events
from src.web.routes import LOGO_SVG, _head
from src.web.users import WebUserStore

router = APIRouter(prefix="/admin", tags=["admin-web"], include_in_schema=False)

ADMIN_COOKIE = "admin_session"
ADMIN_SESSION_TTL = 12 * 3600  # 12 h
ADMIN_SESSION_VALUE = "admin"  # marqueur stocké dans Redis (indépendant des credentials)
BILLING_STATUSES = ("trialing", "active", "past_due", "canceled")
BILLING_STATUS_LABELS = {
    "trialing": "Essai",
    "active": "Actif (payé)",
    "past_due": "Paiement en retard",
    "canceled": "Résilié",
}
PAYMENT_PROVIDERS = ("", "stripe", "paypal", "mobile_wallet", "manual")

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
    "email_rate_limited": ("Rate limit", "bg-amber-100 text-amber-700"),
}


def _e(value) -> str:
    return html.escape(str(value), quote=True)


def _admin_page(title: str, content: str) -> HTMLResponse:
    return HTMLResponse(f"""{_head(title)}
<body class="bg-slate-100 text-slate-800 min-h-screen">
<nav class="bg-slate-900 text-white sticky top-0 z-20">
  <div class="max-w-7xl mx-auto px-4 sm:px-6 min-h-14 py-2 flex flex-wrap items-center justify-between gap-x-4 gap-y-1">
    <a href="/admin" class="flex items-center gap-2.5 font-extrabold">{LOGO_SVG}
      MailHub <span class="badge bg-rose-500 text-white">ADMIN</span>
    </a>
    <div class="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
      <a href="/admin" class="text-slate-300 hover:text-white font-medium">Tenants</a>
      <a href="/admin/tenants/new" class="text-slate-300 hover:text-white font-medium">Nouveau tenant</a>
      <a href="/admin/support" class="text-slate-300 hover:text-white font-medium">Support</a>
      <a href="/" class="text-slate-300 hover:text-white font-medium">Site</a>
      <form method="post" action="/admin/logout" class="inline">
        <button type="submit" class="text-rose-300 hover:text-rose-200 font-medium bg-transparent border-0 p-0 cursor-pointer text-sm">Déconnexion</button>
      </form>
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
    # Marqueur fixe : la session ne dépend ni de ADMIN_API_KEY ni du mot de passe
    if not stored or not hmac.compare_digest(stored.encode(), ADMIN_SESSION_VALUE.encode()):
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
        <div><label class="text-xs font-semibold text-slate-600">Clé admin ou mot de passe</label>
        <input name="admin_key" type="password" class="input mt-1" required autofocus
               placeholder="ADMIN_API_KEY ou ADMIN_WEB_PASSWORD"></div>
        <button class="btn-primary w-full">Se connecter</button>
      </form>
    </div>
    <p class="text-center text-xs text-slate-500 mt-5">
      ADMIN_API_KEY ou ADMIN_WEB_PASSWORD dans les variables d'environnement.
    </p>
  </div>
</body></html>""")


@router.post("/login")
async def admin_login(
    request: Request,
    admin_key: str = Form(...),
    settings: Settings = Depends(get_settings),
    _: None = Depends(require_same_origin),
):
    # Anti brute-force : 5 tentatives / IP / 10 min
    redis = request.app.state.redis
    rl_key = f"rl:adminlogin:{get_client_ip(request)}"
    attempts = await redis.incr(rl_key)
    if attempts == 1:
        await redis.expire(rl_key, 600)
    if attempts > settings.web_login_max_attempts:
        raise HTTPException(status_code=429, detail="Too many attempts, retry later")

    candidates = [
        c for c in (settings.admin_api_key, settings.admin_web_password) if c
    ]
    if not any(
        hmac.compare_digest(admin_key.encode("utf-8", "ignore"), c.encode())
        for c in candidates
    ):
        return RedirectResponse("/admin/login?error=1", status_code=303)

    token = secrets.token_urlsafe(32)
    await redis.set(
        f"adminsession:{token}", ADMIN_SESSION_VALUE,
        ex=ADMIN_SESSION_TTL,
    )
    resp = RedirectResponse("/admin", status_code=303)
    resp.set_cookie(
        ADMIN_COOKIE, token, max_age=ADMIN_SESSION_TTL,
        httponly=True, samesite="lax", path="/admin",
        secure=str(settings.public_base_url).startswith("https://"),
    )
    return resp


@router.post("/logout")
async def admin_logout(request: Request, _: None = Depends(require_same_origin)):
    token = request.cookies.get(ADMIN_COOKIE)
    if token:
        await request.app.state.redis.delete(f"adminsession:{token}")
    resp = RedirectResponse("/admin/login", status_code=303)
    resp.delete_cookie(ADMIN_COOKIE, path="/admin")
    return resp


@router.get("/logout")
async def admin_logout_get():
    # Anti-CSRF : la déconnexion exige un POST.
    return RedirectResponse("/admin", status_code=303)


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

    from datetime import date
    today = date.today().isoformat()
    rows = ""
    all_events: list[dict] = []
    today_otps = today_emails = 0
    active_count = 0
    for t in tenants:
        active_count += 1 if t.is_active else 0
        stats = await _tenant_stats(redis, t)
        pipe = redis.pipeline()
        pipe.get(f"{t.key_prefix()}stats_daily:otps_sent:{today}")
        pipe.get(f"{t.key_prefix()}stats_daily:emails_sent:{today}")
        d_otps, d_emails = await pipe.execute()
        today_otps += int(d_otps or 0)
        today_emails += int(d_emails or 0)
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
        partner_badge = (
            ' <span class="badge bg-amber-100 text-amber-700">PARTENAIRE ∞</span>'
            if t.unlimited else ""
        )
        if t.plan_requested:
            partner_badge += (
                f' <span class="badge bg-amber-50 text-amber-600 border border-amber-200">'
                f"⏳ {_e(t.plan_requested.upper())} demandé</span>"
            )
        rows += f"""<tr class="hover:bg-slate-50 transition align-top">
  <td class="py-4 pl-5 pr-4">
    <p class="font-bold">{_e(t.name)}</p>
    <p class="text-xs text-slate-400"><code>{_e(t.tenant_id)}</code> · {_e(t.brand_name)}
    · <span class="badge bg-indigo-50 text-indigo-700">{_e(PLANS.get(t.plan, PLANS["free"])["label"])}</span>{partner_badge}</p>
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

    open_support = int(await redis.llen("support:open") or 0)

    content = f"""
<div class="flex items-center justify-between mb-6">
  <div>
    <h1 class="text-2xl font-extrabold">Administration</h1>
    <p class="text-sm text-slate-500">{len(tenants)} tenant(s) — supervision et gestion des accès API.</p>
  </div>
  <a href="/admin/tenants/new" class="btn-primary text-sm">+ Nouveau tenant</a>
</div>

<div class="grid grid-cols-2 md:grid-cols-4 gap-4 mb-8">

  <div class="card p-4">
    <p class="text-xs font-semibold text-slate-500 uppercase tracking-wide">Tenants</p>
    <p class="text-2xl font-extrabold mt-1">{len(tenants)}
      <span class="text-xs font-semibold text-emerald-600">{active_count} actifs</span></p>
  </div>
  <div class="card p-4">
    <p class="text-xs font-semibold text-slate-500 uppercase tracking-wide">OTP aujourd'hui</p>
    <p class="text-2xl font-extrabold mt-1">{today_otps}</p>
  </div>
  <div class="card p-4">
    <p class="text-xs font-semibold text-slate-500 uppercase tracking-wide">Emails aujourd'hui</p>
    <p class="text-2xl font-extrabold mt-1">{today_emails}</p>
  </div>
  <div class="card p-4">
    <p class="text-xs font-semibold text-slate-500 uppercase tracking-wide">Support</p>
    <p class="text-2xl font-extrabold mt-1">{open_support}
      <a href="/admin/support" class="text-xs font-semibold text-indigo-600">voir →</a></p>
  </div>
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
    def g(k, d=""):
        # getattr retourne la valeur même quand elle est None (champ non
        # renseigné) — sans garde, le formulaire afficherait "None" et le
        # save écrirait la chaîne "None" (voire int("None") -> 500).
        if not t:
            return _e(d)
        v = getattr(t, k, None)
        return _e(v if v is not None else d)
    risks = _e(",".join(t.risky_countries) if t else "")
    purposes = _e(",".join(t.allowed_purposes) if t else "login_2fa")
    smtp_pass = ""  # jamais prérempli
    cur_plan = t.plan if t else "free"
    plan_options = "".join(
        f'<option value="{_e(pid)}"{" selected" if cur_plan == pid else ""}>'
        f"{_e(p['label'])} — {_e(p['price'])}</option>"
        for pid, p in PLANS.items()
    )

    cur_billing = t.billing_status if t else "trialing"
    billing_options = "".join(
        f'<option value="{_e(s)}"{" selected" if cur_billing == s else ""}>'
        f"{_e(BILLING_STATUS_LABELS.get(s, s))}</option>"
        for s in BILLING_STATUSES
    )
    cur_provider = (t.payment_provider or "") if t else ""
    provider_options = "".join(
        f'<option value="{_e(p)}"{" selected" if cur_provider == p else ""}>'
        f"{_e(p) if p else '—'}</option>"
        for p in PAYMENT_PROVIDERS
    )
    unlimited_checked = " checked" if t and t.unlimited else ""

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
  <input name="user_password" type="password" class="input mt-1" minlength="8" placeholder="8 caractères min"></div>"""

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
  <div><label class="text-xs font-semibold text-slate-600">Couleur emails</label>
  <input name="brand_color" type="color" class="mt-1 h-10 w-full rounded-lg border border-slate-200 cursor-pointer" value="{g('brand_color', '#6366f1')}"></div>
  <div><label class="text-xs font-semibold text-slate-600">Logo emails (URL)</label>
  <input name="email_logo_url" type="url" class="input mt-1" value="{g('email_logo_url')}" placeholder="https://…"></div>
  <div class="md:col-span-2"><label class="text-xs font-semibold text-slate-600">Pied de page emails</label>
  <input name="email_footer" class="input mt-1" value="{g('email_footer')}" placeholder="Vide = texte par défaut"></div>

  <h3 class="font-bold text-sm text-slate-700 md:col-span-2 mt-2">SMTP du client <span class="font-normal text-slate-400">(vide = config globale du serveur ; IONOS : smtp.ionos.fr:587)</span></h3>
  <div><label class="text-xs font-semibold text-slate-600">SMTP Host</label>
  <input name="smtp_host" class="input mt-1" value="{g('smtp_host')}" placeholder="smtp.ionos.fr"></div>
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

  <h3 class="font-bold text-sm text-slate-700 md:col-span-2 mt-2">Plan & quotas</h3>
  {unlimited_hint}
  <div><label class="text-xs font-semibold text-slate-600">Plan d'abonnement</label>
  <select name="plan" class="input mt-1">{plan_options}</select>
  <p class="text-[11px] text-slate-400 mt-1">Tag affiché au client — les champs quota ci-dessous font foi.</p></div>
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

  <h3 class="font-bold text-sm text-slate-700 md:col-span-2 mt-2">Partenaire & facturation</h3>
  <div class="md:col-span-2 flex items-center gap-2 bg-indigo-50 rounded-lg p-3">
    <input name="unlimited" type="checkbox" id="unlimited" class="h-4 w-4"{unlimited_checked}>
    <label for="unlimited" class="text-sm font-semibold text-indigo-700">
      Partenaire — quotas et cooldown illimités (bypass total du rate-limit)</label>
  </div>
  <div><label class="text-xs font-semibold text-slate-600">Statut de facturation</label>
  <select name="billing_status" class="input mt-1">{billing_options}</select></div>
  <div><label class="text-xs font-semibold text-slate-600">Payé jusqu'au</label>
  <input name="paid_until" type="date" class="input mt-1" value="{g('paid_until')}"></div>
  <div><label class="text-xs font-semibold text-slate-600">Prestataire de paiement</label>
  <select name="payment_provider" class="input mt-1">{provider_options}</select></div>
  <div><label class="text-xs font-semibold text-slate-600">Référence paiement</label>
  <input name="payment_ref" class="input mt-1" value="{g('payment_ref')}"
         placeholder="cus_… / sub_… / transaction id"></div>

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
    if not value:
        return None
    try:
        port = int(value)
    except ValueError:
        raise HTTPException(status_code=422, detail="Port SMTP invalide") from None
    if not 1 <= port <= 65535:
        raise HTTPException(status_code=422, detail="Port SMTP invalide")
    return port


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
    brand_color: str = Form("#6366f1"),
    email_logo_url: str = Form(""),
    email_footer: str = Form(""),
    smtp_host: str = Form(""),
    smtp_port: str = Form(""),
    smtp_user: str = Form(""),
    smtp_password: str = Form(""),
    allowed_purposes: str = Form("login_2fa"),
    risky_countries: str = Form(""),
    rollout_percentage: int = Form(100),
    plan: str = Form("free"),
    max_sends_per_user_per_10min: int = Form(1000000),
    max_sends_per_ip_per_10min: int = Form(1000000),
    cooldown_seconds: int = Form(1),
    max_verify_attempts: int = Form(1000000),
    otp_ttl_seconds: int = Form(600),
    user_email: str = Form(""),
    user_password: str = Form(""),
    _: None = Depends(require_same_origin),
) -> HTMLResponse:
    await _require_admin(request, settings)
    redis = request.app.state.redis
    store = TenantStore(redis)
    tenant_id = tenant_id.strip().lower()
    if not re.fullmatch(r"[a-z0-9_-]{2,64}", tenant_id):
        raise HTTPException(
            status_code=422,
            detail="tenant_id invalide (2-64 caractères : a-z, 0-9, -, _)",
        )
    if from_email.strip() and (
        "@" not in from_email or "." not in from_email.rsplit("@", 1)[-1]
    ):
        raise HTTPException(status_code=422, detail="Email expéditeur invalide")

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
        brand_color=brand_color.strip() or "#6366f1",
        email_logo_url=email_logo_url.strip() or None,
        email_footer=email_footer.strip() or None,
        smtp_host=smtp_host.strip() or None,
        smtp_port=_int_or_none(smtp_port),
        smtp_user=smtp_user.strip() or None,
        smtp_password=smtp_password.strip() or None,
        risky_countries=[c.upper() for c in _csv(risky_countries)],
        rollout_percentage=max(0, min(100, rollout_percentage)),
        allowed_purposes=_csv(allowed_purposes) or ["login_2fa"],
        plan=plan if plan in PLANS else "free",
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
            await redis.set(
                f"webuser:creds:{tenant_id}", f"{api_key}|{hmac_secret}", ex=86400
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

    flash = ""
    if saved:
        flash = ('<p class="text-sm text-emerald-700 bg-emerald-50 rounded-lg p-3 mb-4">'
                 "Configuration enregistrée.</p>")
    elif request.query_params.get("account"):
        flash = ('<p class="text-sm text-emerald-700 bg-emerald-50 rounded-lg p-3 mb-4">'
                 "Compte dashboard créé — le client peut se connecter sur /login.</p>")
    elif request.query_params.get("error") == "email":
        flash = ('<p class="text-sm text-red-600 bg-red-50 rounded-lg p-3 mb-4">'
                 "Cet email possède déjà un compte dashboard.</p>")
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
    users = await WebUserStore(request.app.state.redis).list_by_tenant(tenant_id)
    user_rows = ""
    for u in users:
        user_rows += f"""<tr class="border-t border-slate-100">
  <td class="py-2.5 pl-5 pr-4 text-sm">{html.escape(u.email)}
    <span class="text-xs text-slate-400 ml-2">{html.escape(u.user_id)}</span></td>
  <td class="py-2.5 pr-5 text-right whitespace-nowrap">
    <form method="post" action="/admin/tenants/{_e(tenant_id)}/users/{_e(u.user_id)}/reset-password" class="inline">
      <button class="text-xs font-semibold text-indigo-600 border border-indigo-200 rounded-lg px-3 py-1.5 hover:bg-indigo-50">Reset MDP</button>
    </form>
    <form method="post" action="/admin/tenants/{_e(tenant_id)}/users/{_e(u.user_id)}/delete" class="inline ml-1"
          onsubmit="return confirm('Supprimer ce compte utilisateur ?')">
      <button class="text-xs font-semibold text-rose-600 border border-rose-200 rounded-lg px-3 py-1.5 hover:bg-rose-50">Supprimer</button>
    </form>
  </td>
</tr>"""
    if not user_rows:
        user_rows = ('<tr><td colspan="2" class="py-6 text-center text-sm text-slate-400">'
                     "Aucun compte dashboard.</td></tr>")
    delete_zone = "" if tenant_id == "default" else f"""
<div class="card p-4 sm:p-6 border-rose-200 mt-6">
  <h2 class="font-bold mb-1 text-rose-700">Zone danger</h2>
  <p class="text-sm text-slate-500 mb-4">Supprime le tenant, ses clés API, ses statistiques
  et ses comptes utilisateurs. <b>Action irréversible.</b></p>
  <form method="post" action="/admin/tenants/{_e(tenant_id)}/delete"
        onsubmit="return confirm('Supprimer définitivement ce tenant et ses comptes ?')">
    <button class="text-sm font-bold text-white bg-rose-600 rounded-lg px-5 py-2.5 hover:bg-rose-700">Supprimer le tenant</button>
  </form>
</div>"""
    paid_badge = {
        "trialing": '<span class="badge bg-slate-100 text-slate-600">ESSAI</span>',
        "active": '<span class="badge bg-emerald-100 text-emerald-700">ACTIF (PAYÉ)</span>',
        "past_due": '<span class="badge bg-amber-100 text-amber-700">EN RETARD</span>',
        "canceled": '<span class="badge bg-rose-100 text-rose-700">RÉSILIÉ</span>',
    }.get(tenant.billing_status, _e(tenant.billing_status))
    plan_request_html = ""
    reject_plan_form = ""
    if tenant.plan_requested:
        plan_request_html = f"""
  <p class="text-sm font-semibold text-amber-700 bg-amber-50 border border-amber-200 rounded-lg p-3 mb-4">
    ⏳ Ce client demande le plan <b>{_e(tenant.plan_requested.upper())}</b> —
    « Valider un paiement » appliquera ce plan et ses quotas.</p>"""
        reject_plan_form = f"""
    <form method="post" action="/admin/tenants/{_e(tenant_id)}/billing"
          onsubmit="return confirm('Refuser cette demande de plan ?')">
      <input type="hidden" name="action" value="reject_plan">
      <button class="text-xs font-semibold text-slate-600 border border-slate-200 rounded-lg px-4 py-2 hover:bg-slate-50">Refuser la demande</button>
    </form>"""
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
<div class="card p-4 sm:p-6 mb-6">
  {_tenant_form(f'/admin/tenants/{_e(tenant_id)}', tenant, include_account=False)}
</div>

<div class="card p-4 sm:p-6 mb-6">
  <h2 class="font-bold mb-1">Facturation — validation manuelle</h2>
  <p class="text-sm text-slate-500 mb-4">Statut : {paid_badge}
    {f"· payé jusqu'au <b>{_e(tenant.paid_until)}</b>" if tenant.paid_until else ""}
    {f"· via {_e(tenant.payment_provider)}" if tenant.payment_provider else ""}
    {f"· réf <code>{_e(tenant.payment_ref)}</code>" if tenant.payment_ref else ""}</p>
  {plan_request_html}
  <div class="flex flex-wrap gap-2">
    <form method="post" action="/admin/tenants/{_e(tenant_id)}/billing">
      <input type="hidden" name="action" value="paid">
      <button class="text-xs font-semibold text-emerald-700 border border-emerald-200 rounded-lg px-4 py-2 hover:bg-emerald-50">✓ Valider un paiement (+30 j)</button>
    </form>
    <form method="post" action="/admin/tenants/{_e(tenant_id)}/billing">
      <input type="hidden" name="action" value="past_due">
      <button class="text-xs font-semibold text-amber-700 border border-amber-200 rounded-lg px-4 py-2 hover:bg-amber-50">Marquer en retard</button>
    </form>
    <form method="post" action="/admin/tenants/{_e(tenant_id)}/billing"
          onsubmit="return confirm('Résilier ce client ?')">
      <input type="hidden" name="action" value="canceled">
      <button class="text-xs font-semibold text-rose-600 border border-rose-200 rounded-lg px-4 py-2 hover:bg-rose-50">Résilier</button>
    </form>
    {reject_plan_form}
  </div>
  <p class="text-[11px] text-slate-400 mt-3">À utiliser si Stripe / PayPal / mobile money est
  indisponible ou pour un paiement reçu hors ligne — la trace reste via le prestataire « manual ».</p>
</div>

<div class="card p-4 sm:p-6">
  <h2 class="font-bold mb-1">Compte dashboard du client</h2>
  <p class="text-sm text-slate-500 mb-4">Crée un accès à <code>/dashboard</code> pour ce tenant —
  le client y verra ses stats, configurera son SMTP et pourra régénérer ses clés.</p>
  <form method="post" action="/admin/tenants/{_e(tenant_id)}/account" class="grid md:grid-cols-3 gap-4">
    <div><label class="text-xs font-semibold text-slate-600">Email du client</label>
    <input name="user_email" type="email" class="input mt-1" required placeholder="client@exemple.com"></div>
    <div><label class="text-xs font-semibold text-slate-600">Mot de passe</label>
    <input name="user_password" type="password" class="input mt-1" required minlength="8" placeholder="8 caractères min"></div>
    <div class="flex items-end"><button class="btn-primary text-sm">Créer le compte</button></div>
  </form>
</div>

<div class="card p-4 sm:p-6 mt-6">
  <h2 class="font-bold mb-3">Comptes dashboard existants</h2>
  <div class="overflow-x-auto"><table class="w-full text-sm"><tbody>{user_rows}</tbody></table></div>
</div>
{delete_zone}"""
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
    brand_color: str = Form("#6366f1"),
    email_logo_url: str = Form(""),
    email_footer: str = Form(""),
    smtp_host: str = Form(""),
    smtp_port: str = Form(""),
    smtp_user: str = Form(""),
    smtp_password: str = Form(""),
    allowed_purposes: str = Form("login_2fa"),
    risky_countries: str = Form(""),
    rollout_percentage: int = Form(100),
    plan: str = Form("free"),
    unlimited: str = Form(""),
    billing_status: str = Form("trialing"),
    paid_until: str = Form(""),
    payment_provider: str = Form(""),
    payment_ref: str = Form(""),
    max_sends_per_user_per_10min: int = Form(1000000),
    max_sends_per_ip_per_10min: int = Form(1000000),
    cooldown_seconds: int = Form(1),
    max_verify_attempts: int = Form(1000000),
    otp_ttl_seconds: int = Form(600),
    _: None = Depends(require_same_origin),
):
    await _require_admin(request, settings)
    if plan not in PLANS:
        raise HTTPException(status_code=422, detail="Plan invalide")
    if from_email.strip() and (
        "@" not in from_email or "." not in from_email.rsplit("@", 1)[-1]
    ):
        raise HTTPException(status_code=422, detail="Email expéditeur invalide")
    updates = {
        "name": name.strip(),
        "brand_name": brand_name.strip() or "SendBID",
        "from_email": _opt(from_email),
        "from_name": _opt(from_name),
        "brand_color": brand_color.strip() or "#6366f1",
        "email_logo_url": _opt(email_logo_url),
        "email_footer": _opt(email_footer),
        "smtp_host": _opt(smtp_host),
        "smtp_port": _int_or_none(smtp_port),
        "smtp_user": _opt(smtp_user),
        "risky_countries": [c.upper() for c in _csv(risky_countries)],
        "rollout_percentage": max(0, min(100, rollout_percentage)),
        "allowed_purposes": _csv(allowed_purposes) or ["login_2fa"],
        "plan": plan,
        "unlimited": unlimited == "on",
        "billing_status": (
            billing_status if billing_status in BILLING_STATUSES else "trialing"
        ),
        "paid_until": _opt(paid_until),
        "payment_provider": _opt(payment_provider),
        "payment_ref": _opt(payment_ref),
        "max_sends_per_user_per_10min": max(1, max_sends_per_user_per_10min),
        "max_sends_per_ip_per_10min": max(1, max_sends_per_ip_per_10min),
        "cooldown_seconds": max(1, cooldown_seconds),
        "max_verify_attempts": max(1, max_verify_attempts),
        "otp_ttl_seconds": max(30, otp_ttl_seconds),
    }
    if smtp_password.strip():
        updates["smtp_password"] = smtp_password.strip()

    # Les champs optionnels vides repassent à None (= fallback config globale)
    tenant = await TenantStore(request.app.state.redis).update(tenant_id, updates)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant introuvable")
    return RedirectResponse(f"/admin/tenants/{tenant_id}?saved=1", status_code=303)


@router.post("/tenants/{tenant_id}/billing")
async def admin_billing_action(
    tenant_id: str,
    request: Request,
    action: str = Form(...),
    settings: Settings = Depends(get_settings),
    _: None = Depends(require_same_origin),
):
    """Validation manuelle des paiements — utile quand Stripe / PayPal /
    mobile money est indisponible ou pour un règlement reçu hors ligne."""
    await _require_admin(request, settings)
    store = TenantStore(request.app.state.redis)
    tenant = await store.get(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant introuvable")
    if action == "paid":
        from datetime import date, timedelta
        updates = {
            "billing_status": "active",
            "paid_until": (date.today() + timedelta(days=30)).isoformat(),
            "payment_provider": tenant.payment_provider or "manual",
        }
        # Une demande de plan en attente est appliquée avec le paiement.
        if tenant.plan_requested and tenant.plan_requested in PLANS:
            updates["plan"] = tenant.plan_requested
            updates["plan_requested"] = None
            updates.update(plan_quota_updates(tenant.plan_requested))
        await store.update(tenant_id, updates)
    elif action == "reject_plan":
        await store.update(tenant_id, {"plan_requested": None})
    elif action in ("past_due", "canceled"):
        await store.update(tenant_id, {"billing_status": action})
    else:
        raise HTTPException(status_code=422, detail="Action inconnue")
    return RedirectResponse(f"/admin/tenants/{tenant_id}?saved=1", status_code=303)


@router.post("/tenants/{tenant_id}/account")
async def admin_create_account(
    tenant_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
    user_email: str = Form(...),
    user_password: str = Form(...),
    _: None = Depends(require_same_origin),
):
    """Crée un compte dashboard (webuser) lié à un tenant existant."""
    await _require_admin(request, settings)
    redis = request.app.state.redis
    tenant = await TenantStore(redis).get(tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant introuvable")
    if len(user_password) < 8:
        raise HTTPException(status_code=422, detail="Mot de passe trop court")

    users = WebUserStore(redis)
    if await users.get_by_email(user_email.strip()):
        return RedirectResponse(
            f"/admin/tenants/{tenant_id}?error=email", status_code=303
        )
    await users.create(user_email.strip(), user_password, tenant_id, tenant.name)
    return RedirectResponse(f"/admin/tenants/{tenant_id}?account=1", status_code=303)


@router.post("/tenants/{tenant_id}/toggle")
async def admin_toggle_tenant(
    tenant_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
    _: None = Depends(require_same_origin),
):
    await _require_admin(request, settings)
    store = TenantStore(request.app.state.redis)
    tenant = await store.get(tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant introuvable")
    await store.update(tenant_id, {"is_active": not tenant.is_active})
    return RedirectResponse("/admin", status_code=303)


# ---------------------------------------------------------------------
# Boîte support — demandes envoyées par les clients depuis leur dashboard
# ---------------------------------------------------------------------

@router.get("/support", response_class=HTMLResponse)
async def admin_support(
    request: Request, settings: Settings = Depends(get_settings)
) -> HTMLResponse:
    await _require_admin(request, settings)
    redis = request.app.state.redis
    ids = await redis.lrange("support:open", 0, 99)
    rows = ""
    for rid in ids:
        raw = await redis.get(f"support:{rid}")
        if not raw:
            continue
        req = json.loads(raw)
        rows += f"""<tr class="hover:bg-slate-50 transition align-top">
  <td class="py-4 pl-5 pr-4">
    <p class="font-bold">{html.escape(req['subject'])}</p>
    <p class="text-xs text-slate-400 mt-0.5">{html.escape(req['app_name'])}
    · {html.escape(req['email'])} · <code>{html.escape(req['tenant_id'])}</code></p>
    <p class="text-sm text-slate-600 mt-2 whitespace-pre-line">{html.escape(req['message'])}</p>
  </td>
  <td class="py-4 pr-4 text-xs text-slate-500 whitespace-nowrap">
    {html.escape(req['created_at'][:16].replace('T', ' '))} UTC
  </td>
  <td class="py-4 pr-5 text-right whitespace-nowrap">
    <a href="/admin/tenants/{html.escape(req['tenant_id'])}" class="btn-outline text-xs mr-2">Voir le compte</a>
    <form method="post" action="/admin/support/{html.escape(req['id'])}/resolve" class="inline">
      <button class="text-xs font-semibold text-emerald-600 border border-emerald-200 rounded-lg px-3 py-1.5 hover:bg-emerald-50">Résolu</button>
    </form>
  </td>
</tr>"""
    empty = (
        '<tr><td colspan="3" class="py-10 text-center text-slate-400 text-sm">'
        "Aucune demande en cours.</td></tr>"
        if not rows
        else ""
    )
    content = f"""
<div class="flex items-center justify-between mb-6">
  <h1 class="text-2xl font-extrabold">Demandes support</h1>
  <span class="badge bg-indigo-100 text-indigo-700">{len(ids)} en attente</span>
</div>
<div class="card overflow-hidden">
  <table class="w-full text-sm"><tbody class="divide-y divide-slate-100">
  {rows}{empty}</tbody></table>
</div>"""
    return _admin_page("Support", content)


@router.post("/support/{req_id}/resolve")
async def admin_support_resolve(
    req_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
    _: None = Depends(require_same_origin),
):
    await _require_admin(request, settings)
    if not re.fullmatch(r"sup_[0-9a-f]{12}", req_id):
        raise HTTPException(status_code=404, detail="Demande introuvable")
    redis = request.app.state.redis
    raw = await redis.get(f"support:{req_id}")
    if raw:
        req = json.loads(raw)
        req["status"] = "resolved"
        await redis.set(f"support:{req_id}", json.dumps(req), keepttl=True)
    await redis.lrem("support:open", 0, req_id)
    return RedirectResponse("/admin/support", status_code=303)


# ---------------------------------------------------------------------
# Gestion complète du compte client — utilisateurs + suppression tenant
# ---------------------------------------------------------------------

@router.post("/tenants/{tenant_id}/users/{user_id}/delete")
async def admin_delete_user(
    tenant_id: str,
    user_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
    _: None = Depends(require_same_origin),
):
    await _require_admin(request, settings)
    if not re.fullmatch(r"usr_[A-Za-z0-9_-]+", user_id):
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")
    user = await WebUserStore(request.app.state.redis).get(user_id)
    if not user or user.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")
    await WebUserStore(request.app.state.redis).delete(user_id)
    return RedirectResponse(f"/admin/tenants/{tenant_id}", status_code=303)


@router.post("/tenants/{tenant_id}/users/{user_id}/reset-password")
async def admin_reset_user_password(
    tenant_id: str,
    user_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
    _: None = Depends(require_same_origin),
):
    await _require_admin(request, settings)
    if not re.fullmatch(r"usr_[A-Za-z0-9_-]+", user_id):
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")
    user = await WebUserStore(request.app.state.redis).get(user_id)
    if not user or user.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")
    new_password = secrets.token_urlsafe(12)
    await WebUserStore(request.app.state.redis).set_password(user_id, new_password)
    content = f"""
<div class="card p-8 max-w-lg">
  <h1 class="text-xl font-bold mb-2">Mot de passe réinitialisé</h1>
  <p class="text-sm text-slate-500 mb-4">Nouveau mot de passe pour
  <b>{html.escape(user.email)}</b> — communiquez-le au client, il ne sera plus affiché :</p>
  <div class="bg-slate-900 text-emerald-300 font-mono rounded-lg p-4 select-all break-all mb-6">{html.escape(new_password)}</div>
  <a href="/admin/tenants/{_e(tenant_id)}" class="btn-primary text-sm">Retour au tenant</a>
</div>"""
    return _admin_page("Mot de passe réinitialisé", content)


@router.post("/tenants/{tenant_id}/delete")
async def admin_delete_tenant(
    tenant_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
    _: None = Depends(require_same_origin),
):
    await _require_admin(request, settings)
    redis = request.app.state.redis
    store = TenantStore(redis)
    tenant = await store.get(tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant introuvable")
    if tenant_id == "default":
        raise HTTPException(
            status_code=422,
            detail="Le tenant 'default' ne peut pas être supprimé",
        )
    users = WebUserStore(redis)
    for user in await users.list_by_tenant(tenant_id):
        await users.delete(user.user_id)
    keys = [f"tenant:{tenant_id}", f"tenant:bykey:{tenant.api_key_hash}",
            f"webuser:creds:{tenant_id}"]
    async for key in redis.scan_iter(f"t:{tenant_id}:*"):
        keys.append(key)
    if keys:
        await redis.delete(*keys)
    await redis.srem("tenant:index", tenant_id)
    return RedirectResponse("/admin", status_code=303)
