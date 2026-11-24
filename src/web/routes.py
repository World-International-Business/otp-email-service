"""Interface web de la plateforme — landing, auth, dashboard, démo."""

import base64
import hmac
import html
import json
import logging
import re
import time
import uuid
from datetime import date
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, EmailStr
from starlette.datastructures import UploadFile

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
from src.services.payments import (
    MANUAL_METHODS,
    PERIODS,
    apply_paid_plan,
    available_providers,
    capture_paypal_order,
    create_checkout,
    get_tx,
    list_payments,
    manual_method_details,
    new_tx_id,
    plan_price,
    record_tx,
    update_tx,
)
from src.web.i18n import (
    _FLAGS,
    DEFAULT_LANG,
    LANG_COOKIE,
    LANG_COOKIE_MAX_AGE,
    LANGUAGES,
    I18nRoute,
    safe_next,
)
from src.web.tailwind_css import TAILWIND_CSS
from src.web.users import SessionStore, WebUserStore, hash_password_pair

router = APIRouter(tags=["web"], include_in_schema=False, route_class=I18nRoute)
logger = logging.getLogger(__name__)


def _pager(
    base: str,
    page: int,
    total: int,
    per_page: int,
    page_param: str,
    params: dict | None = None,
    anchor: str = "",
) -> str:
    """Nav « ‹ Préc | Page x/y | Suiv › » — conserve les paramètres d'URL."""
    pages = max(1, -(-total // per_page))
    if pages <= 1:
        return ""
    page = max(1, min(page, pages))

    def _url(p_: int) -> str:
        q = dict(params or {})
        q[page_param] = p_
        qs = "&".join(
            f"{k}={quote(str(v))}" for k, v in q.items()
            if v not in (None, "", 1)
        )
        return f"{base}?{qs}{anchor}" if qs else f"{base}{anchor}"

    prev = (
        f'<a href="{_url(page - 1)}" class="btn-soft text-xs">‹ Préc.</a>'
        if page > 1
        else '<span class="btn-soft text-xs opacity-40 cursor-default">‹ Préc.</span>'
    )
    nxt = (
        f'<a href="{_url(page + 1)}" class="btn-soft text-xs">Suiv ›</a>'
        if page < pages
        else '<span class="btn-soft text-xs opacity-40 cursor-default">Suiv ›</span>'
    )
    return (
        '<div class="flex items-center gap-3 justify-end px-5 py-3 '
        'border-t border-slate-100 text-xs text-slate-500">'
        f"{prev}<span>Page {page} / {pages}</span>{nxt}</div>"
    )


def _e(value) -> str:
    """Échappe une valeur avant injection dans le HTML (anti-XSS)."""
    return html.escape(str(value), quote=True)


# =====================================================================
# Templates HTML — design moderne (Tailwind)
# =====================================================================

LOGO_MARK = (
    '<img src="/static/mailhub-mark.png?v=3" alt="MailHub" class="h-10 w-auto">'
)
LOGO_FULL = (
    '<img src="/static/mailhub-logo.png?v=3" alt="MailHub" class="h-16 w-auto">'
)
LOGO_SVG = LOGO_MARK

FAVICON_URI = "/static/mailhub-mark.png?v=3"

# Micro-bibliothèque UI partagée par toutes les pages : toasts, modale de
# confirmation, copie presse-papiers, états de chargement des formulaires.
# Aucune dépendance — vanilla JS, < 3 Ko. Les chaînes FR sont traduites via
# le mécanisme RAW d'i18n.
UI_JS = """<script>
(function () {
  var T = {
    confirm: 'Confirmer', cancel: 'Annuler', title: 'Confirmation',
    copied: 'Copié !', copyfail: 'Copie impossible', neterr: 'Erreur réseau'
  };
  var ICONS = {
    ok: '<svg fill="none" stroke="currentColor" stroke-width="2.2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/></svg>',
    err: '<svg fill="none" stroke="currentColor" stroke-width="2.2" viewBox="0 0 24 24"><path stroke-linecap="round" d="M6 6l12 12M18 6L6 18"/></svg>',
    warn: '<svg fill="none" stroke="currentColor" stroke-width="2.2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M12 9v4m0 4h.01M10.3 3.9L1.8 18a2 2 0 001.7 3h17a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z"/></svg>',
    info: '<svg fill="none" stroke="currentColor" stroke-width="2.2" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path stroke-linecap="round" d="M12 8h.01M12 11v5"/></svg>',
    shield: '<svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linejoin="round" d="M12 3l7 3v5c0 4.4-3 8.4-7 10-4-1.6-7-5.6-7-10V6z"/></svg>'
  };

  function root() {
    var r = document.querySelector('.toast-root');
    if (!r) { r = document.createElement('div'); r.className = 'toast-root'; document.body.appendChild(r); }
    return r;
  }

  window.toast = function (msg, type, ms) {
    type = type || 'info'; ms = ms || 3800;
    var t = document.createElement('div');
    t.className = 'toast t-' + type;
    t.innerHTML = ICONS[type === 'err' ? 'err' : type] || ICONS.info;
    var span = document.createElement('span');
    span.textContent = msg;
    t.appendChild(span);
    root().appendChild(t);
    setTimeout(function () { t.classList.add('out'); setTimeout(function () { t.remove(); }, 240); }, ms);
  };

  window.uiConfirm = function (opts) {
    return new Promise(function (resolve) {
      var ov = document.createElement('div');
      ov.className = 'modal-ov';
      var danger = opts && opts.danger;
      ov.innerHTML =
        '<div class="modal-card" role="dialog" aria-modal="true">' +
          '<div class="modal-ic' + (danger ? ' danger' : '') + '">' + (danger ? ICONS.warn : ICONS.shield) + '</div>' +
          '<p class="font-bold text-base mb-1.5">' + (opts.title || T.title) + '</p>' +
          '<p class="text-sm text-slate-500 leading-relaxed mb-6">' + opts.msg + '</p>' +
          '<div class="flex gap-3 justify-end">' +
            '<button type="button" class="btn-soft" data-x="0">' + T.cancel + '</button>' +
            '<button type="button" class="' + (danger ? 'btn-danger' : 'btn-soft') + '" data-x="1" autofocus>' +
              (opts.ok || T.confirm) + '</button>' +
          '</div>' +
        '</div>';
      function done(v) { ov.remove(); document.removeEventListener('keydown', onKey); resolve(v); }
      function onKey(e) { if (e.key === 'Escape') done(false); }
      ov.addEventListener('click', function (e) {
        if (e.target === ov) return done(false);
        var b = e.target.closest('[data-x]');
        if (b) done(b.getAttribute('data-x') === '1');
      });
      document.addEventListener('keydown', onKey);
      document.body.appendChild(ov);
      var b = ov.querySelector('[data-x="1"]'); if (b) b.focus();
    });
  };

  window.copyText = function (text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(
        function () { toast(T.copied, 'ok', 1800); },
        function () { toast(T.copyfail, 'err'); }
      );
    } else {
      var ta = document.createElement('textarea');
      ta.value = text; document.body.appendChild(ta); ta.select();
      try { document.execCommand('copy'); toast(T.copied, 'ok', 1800); }
      catch (e) { toast(T.copyfail, 'err'); }
      ta.remove();
    }
  };

  // data-confirm="message" sur un <form> -> modale avant soumission réelle.
  // data-confirm-danger -> variante rouge pour les actions destructives.
  document.addEventListener('submit', function (e) {
    var f = e.target;
    if (!(f instanceof HTMLFormElement)) return;
    var msg = f.getAttribute('data-confirm');
    if (msg && !f.dataset.confirmOk) {
      e.preventDefault();
      uiConfirm({
        msg: msg,
        danger: f.hasAttribute('data-confirm-danger'),
        ok: f.getAttribute('data-confirm-ok') || undefined,
        title: f.getAttribute('data-confirm-title') || undefined
      }).then(function (ok) {
        if (!ok) return;
        f.dataset.confirmOk = '1';
        if (f.requestSubmit) f.requestSubmit(); else f.submit();
      });
      return;
    }
    var btn = f.querySelector('button[type="submit"]:not([data-noload]), button:not([type]):not([data-noload])');
    if (btn) { btn.classList.add('is-loading'); btn.disabled = true; }
  }, true);

  // data-copy="texte" -> copie + toast
  document.addEventListener('click', function (e) {
    var c = e.target.closest('[data-copy]');
    if (c) { e.preventDefault(); copyText(c.getAttribute('data-copy')); }
  });

  // Effets au DOM prêt (ce script est dans <head>, avant le body)
  function initFx() {
    // Toggle mensuel/annuel : [data-bill] dans [data-billing-toggle], scope = section parente
    document.querySelectorAll('[data-bill]').forEach(function (b) {
    b.addEventListener('click', function () {
      var p = b.getAttribute('data-bill');
      b.closest('[data-billing-toggle]').querySelectorAll('[data-bill]').forEach(function (x) {
        x.classList.toggle('active', x === b);
      });
      var scope = b.closest('section') || document;
      scope.querySelectorAll('[data-m]').forEach(function (el) { el.classList.toggle('hidden', p === 'y'); });
      scope.querySelectorAll('[data-y]').forEach(function (el) { el.classList.toggle('hidden', p === 'm'); });
    });
  });

  // Révélation au scroll + compteurs animés
  var reduced = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
  var revs = document.querySelectorAll('.reveal');
  function countUp(el) {
    var target = parseFloat(el.getAttribute('data-count'));
    if (isNaN(target) || reduced) { return; }
    var dur = 1100, t0 = null, suffix = el.getAttribute('data-count-suffix') || '';
    function tick(t) {
      if (!t0) { t0 = t; }
      var k = Math.min(1, (t - t0) / dur);
      var eased = 1 - Math.pow(1 - k, 3);
      el.textContent = Math.round(target * eased) + suffix;
      if (k < 1) { requestAnimationFrame(tick); }
    }
    requestAnimationFrame(tick);
  }
  if (!reduced && 'IntersectionObserver' in window) {
    var io = new IntersectionObserver(function (es) {
      es.forEach(function (e) {
        if (!e.isIntersecting) { return; }
        e.target.classList.add('in');
        if (e.target.hasAttribute('data-count')) { countUp(e.target); }
        io.unobserve(e.target);
      });
    }, { threshold: .12 });
    revs.forEach(function (el) { io.observe(el); });
    document.querySelectorAll('[data-count]').forEach(function (el) { io.observe(el); });
    } else {
      revs.forEach(function (el) { el.classList.add('in'); });
    }

    // Scroll-spy sidebar dashboard : surligne l'item de la section visible.
    // Les sections sans item (sec-payments, sec-activity, sec-config) gardent
    // le dernier item valide ; tout en haut -> "top" (Vue d'ensemble).
    var spyLinks = document.querySelectorAll('.sb-link[data-sec]');
    // Uniquement sur la page dashboard principale : les sous-pages (checkout,
    // règlement) n'ont pas de sections et gardent l'état actif du serveur.
    if (spyLinks.length && document.querySelector('[id^="sec-"]')) {
      var secMap = {};
      spyLinks.forEach(function (a) { secMap[a.getAttribute('data-sec')] = a; });
      var spyCur = 'top';
      var spy = function () {
        var y = window.scrollY + 140;
        var cur = 'top';
        document.querySelectorAll('[id^="sec-"]').forEach(function (s) {
          if (secMap[s.id] && s.offsetTop <= y) { cur = s.id; }
        });
        spyCur = cur;
        spyLinks.forEach(function (a) {
          a.classList.toggle('on', a.getAttribute('data-sec') === cur);
        });
      };
      window.addEventListener('scroll', spy, { passive: true });
      window.addEventListener('hashchange', spy);
      // Clic : activer immédiatement l'item, avant la fin du scroll animé.
      spyLinks.forEach(function (a) {
        a.addEventListener('click', function () {
          spyLinks.forEach(function (x) { x.classList.toggle('on', x === a); });
        });
      });
      spy();
    }
  }

  function initFxSafe() {
    try {
      initFx();
    } catch (e) {
      // En cas d'erreur : tout afficher plutôt qu'une page invisible.
      document.querySelectorAll('.reveal').forEach(function (el) { el.classList.add('in'); });
    }
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initFxSafe);
  } else {
    initFxSafe();
  }
})();
</script>"""


def _head(title: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="fr" class="scroll-smooth">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="description" content="MailHub — API d'emails transactionnels et de codes OTP. Multi-tenant, sécurisée HMAC, intégration en 5 minutes.">
<link rel="icon" type="image/png" href="{FAVICON_URI}">
<title>{title} — MailHub</title>
<style>{TAILWIND_CSS}</style>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Space+Grotesk:wght@500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<noscript><style>.reveal{{opacity:1!important;transform:none!important}}</style></noscript>
{UI_JS}
</head>"""


_STATIC_DIR = Path(__file__).resolve().parent / "static"
_STATIC_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".svg": "image/svg+xml",
    ".ico": "image/x-icon", ".woff": "font/woff", ".woff2": "font/woff2",
}


@router.get("/static/{path:path}")
async def static_asset(path: str):
    """Assets locaux (logo, polices) — whitelist extensions + anti-traversal."""
    target = (_STATIC_DIR / path).resolve()
    if (
        not str(target).startswith(str(_STATIC_DIR))
        or target.suffix.lower() not in _STATIC_TYPES
        or not target.is_file()
    ):
        raise HTTPException(status_code=404, detail="Asset introuvable")
    return Response(
        target.read_bytes(),
        media_type=_STATIC_TYPES[target.suffix.lower()],
        headers={"Cache-Control": "public, max-age=3600"},
    )


def _footer(logged_in: bool = False) -> str:
    account = (
        '<a href="/dashboard" class="hover:text-white transition">Mon espace</a>'
        if logged_in else
        '<a href="/register" class="hover:text-white transition">Créer un compte</a>\n'
        '        <a href="/login" class="hover:text-white transition">Connexion</a>'
    )
    year = date.today().year
    return f"""
<footer class="footer bg-[#0a0b14] text-slate-400">
  <div class="max-w-6xl mx-auto px-6 py-12 md:py-16 grid gap-10 sm:grid-cols-2 lg:grid-cols-[1.5fr_1fr_1fr_1fr]">
    <div>
      <div class="flex items-center gap-2.5 text-white font-extrabold mb-3">{LOGO_MARK}</div>
      <p class="text-sm leading-relaxed mb-5">API d'emails transactionnels et de codes OTP.
      Sécurisée, multi-tenant, self-hostable.</p>
      <div class="flex items-center gap-2 opacity-80">
        {''.join(_FLAGS.values())}
      </div>
      <p class="text-[11px] text-slate-500 mt-2">Interface en 5 langues</p>
    </div>
    <div>
      <p class="text-white font-semibold text-sm mb-4">Produit</p>
      <div class="flex flex-col gap-2.5 text-sm">
        <a href="/#services" class="hover:text-white transition">Services</a>
        <a href="/#pricing" class="hover:text-white transition">Tarifs</a>
        <a href="/demo" class="hover:text-white transition">Console de démo</a>
        <a href="/#pricing" class="hover:text-white transition">Passer au Pro</a>
      </div>
    </div>
    <div>
      <p class="text-white font-semibold text-sm mb-4">Développeurs</p>
      <div class="flex flex-col gap-2.5 text-sm">
        <a href="/documentation" class="hover:text-white transition">Documentation</a>
        <a href="/docs" class="hover:text-white transition">Référence API (Swagger)</a>
        <a href="/health" class="hover:text-white transition">Healthcheck</a>
      </div>
    </div>
    <div>
      <p class="text-white font-semibold text-sm mb-4">Compte</p>
      <div class="flex flex-col gap-2.5 text-sm">
        {account}
        <a href="/dashboard#sec-support" class="hover:text-white transition">Support</a>
      </div>
    </div>
  </div>
  <div class="border-t border-white/10">
    <div class="max-w-6xl mx-auto px-6 py-5 text-xs text-slate-500 flex flex-col sm:flex-row items-center justify-between gap-2 text-center">
      <span>© {year} MailHub — World International Business</span>
      <span>Emails & OTP en un appel API.</span>
    </div>
  </div>
</footer>"""


def _landing_page(content: str, logged_in: bool = False, footer: bool = True) -> HTMLResponse:
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
<nav class="bg-white/85 backdrop-blur-md border-b border-slate-200/80 sticky top-0 z-20 shadow-[0_1px_12px_rgba(13,15,30,.05)]">
  <div class="max-w-6xl mx-auto px-4 sm:px-6 h-16 flex items-center justify-between">
    <a href="/" class="flex items-center gap-2.5 font-extrabold text-lg text-slate-900">
      {LOGO_SVG}
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
{_footer(logged_in) if footer else ""}
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
        f'<a href="{url}" data-sec="{"top" if key == "dashboard" else "sec-" + key}" '
        f'class="sb-link{" on" if key == active else ""}">'
        f'{icons[key]}<span>{label}</span></a>'
        for key, label, url in items
    )
    return HTMLResponse(f"""{_head("Dashboard")}
<body class="bg-slate-100 text-slate-800">
<header class="md:hidden fixed top-0 inset-x-0 h-14 bg-[#0d0f1e] text-white flex items-center justify-between px-4 z-20">
  <a href="/dashboard" class="flex items-center gap-2 font-extrabold">{LOGO_SVG}</a>
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
  <aside id="sidebar" class="w-64 bg-[#0d0f1e] text-white flex flex-col fixed inset-y-0 z-30 overflow-y-auto transform -translate-x-full md:translate-x-0 transition-transform duration-200">
    <div class="h-16 flex items-center gap-2.5 px-6 border-b border-white/10 shrink-0">
      {LOGO_SVG}
      <button type="button" aria-label="Fermer" class="ml-auto md:hidden p-1.5 rounded-lg hover:bg-white/10"
              onclick="document.getElementById('sidebar').classList.add('-translate-x-full');document.getElementById('sbOverlay').classList.add('hidden')">
        <svg class="w-5 h-5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" d="M6 6l12 12M18 6L6 18"/></svg>
      </button>
    </div>
    <nav class="flex-1 px-3 py-5 space-y-1">
      <p class="sb-cap">Menu</p>{nav}
      <p class="sb-cap mt-6">Compte</p>
      <a href="/documentation" class="sb-link">{icons['docs']}<span>Documentation</span></a>
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
    "lock": """<svg class="w-6 h-6" fill="none" stroke="#5060a8" stroke-width="2" viewBox="0 0 24 24"><rect x="4" y="10" width="16" height="11" rx="2"/><path d="M8 10V7a4 4 0 118 0v3"/></svg>""",
    "mail": """<svg class="w-6 h-6" fill="none" stroke="#5060a8" stroke-width="2" viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7l9 6 9-6"/></svg>""",
    "layers": """<svg class="w-6 h-6" fill="none" stroke="#5060a8" stroke-width="2" viewBox="0 0 24 24"><path d="M12 2l9 5-9 5-9-5 9-5z"/><path d="M3 12l9 5 9-5"/><path d="M3 17l9 5 9-5"/></svg>""",
    "shield": """<svg class="w-6 h-6" fill="none" stroke="#5060a8" stroke-width="2" viewBox="0 0 24 24"><path d="M12 2l8 4v6c0 5-3.5 8.5-8 10-4.5-1.5-8-5-8-10V6l8-4z"/><path d="M9 12l2 2 4-4"/></svg>""",
    "gauge": """<svg class="w-6 h-6" fill="none" stroke="#5060a8" stroke-width="2" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/></svg>""",
    "code": """<svg class="w-6 h-6" fill="none" stroke="#5060a8" stroke-width="2" viewBox="0 0 24 24"><path d="M8 6l-6 6 6 6M16 6l6 6-6 6"/></svg>""",
    "globe": """<svg class="w-6 h-6" fill="none" stroke="#8b5cf6" stroke-width="2" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a15 15 0 010 18M12 3a15 15 0 000 18"/></svg>""",
    "chart": """<svg class="w-6 h-6" fill="none" stroke="#10b981" stroke-width="2" viewBox="0 0 24 24"><path d="M4 20V10M10 20V4M16 20v-8M22 20H2"/></svg>""",
}


UI_ICONS = {
    "check": '<svg class="w-4 h-4 inline-block -mt-0.5" fill="none" stroke="currentColor" stroke-width="2.2" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path stroke-linecap="round" stroke-linejoin="round" d="M8.5 12.5l2.5 2.5 4.5-5.5"/></svg>',
    "error": '<svg class="w-4 h-4 inline-block -mt-0.5" fill="none" stroke="currentColor" stroke-width="2.2" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path stroke-linecap="round" d="M9 9l6 6M15 9l-6 6"/></svg>',
    "clock": '<svg class="w-4 h-4 inline-block -mt-0.5" fill="none" stroke="currentColor" stroke-width="2.2" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path stroke-linecap="round" stroke-linejoin="round" d="M12 7v5l3.5 2"/></svg>',
    "warn": '<svg class="w-4 h-4 inline-block -mt-0.5" fill="none" stroke="currentColor" stroke-width="2.2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M12 8v5m0 3.5v.01M10.3 3.9L2.6 17a2 2 0 001.7 3h15.4a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z"/></svg>',
    "inbox": '<svg fill="none" stroke="currentColor" stroke-width="1.9" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M3 13l3-8h12l3 8v5a2 2 0 01-2 2H5a2 2 0 01-2-2v-5z"/><path stroke-linecap="round" d="M3 13h5l1.5 3h5L16 13h5"/></svg>',
    "users": '<svg fill="none" stroke="currentColor" stroke-width="1.9" viewBox="0 0 24 24"><circle cx="9" cy="8" r="3.5"/><path stroke-linecap="round" d="M3.5 19c.5-3 2.8-5 5.5-5s5 2 5.5 5M16 8.5a3 3 0 100-6M17.5 14.2c2 .6 3.4 2.3 3.9 4.8"/></svg>',
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
         "border-accent-200"),
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
        f"""<div class="card svc p-6 transition-all border-t-4 reveal reveal-d{si % 3 + 1} {bc}">
          <div class="svc-ic w-11 h-11 rounded-xl bg-gradient-to-br from-brand-50 to-accent-100 border border-brand-100 flex items-center justify-center mb-4">{icon}</div>
          <h3 class="font-bold mb-1.5">{t}</h3>
          <p class="text-sm text-slate-500 leading-relaxed">{d}</p>
        </div>"""
        for si, (icon, t, d, bc) in enumerate(services)
    )

    _CURR = {"EUR": "€", "USD": "$", "GBP": "£", "CHF": "CHF", "XOF": "F CFA", "XAF": "F CFA"}

    def _pp(plan: str) -> tuple[str | None, str | None]:
        pm, cm = plan_price(settings, plan, "stripe", "monthly")
        py, cy = plan_price(settings, plan, "stripe", "yearly")
        m = f"{pm:g} {_CURR.get(cm, cm)}" if pm else None
        y = f"{py:g} {_CURR.get(cy, cy)}" if py else None
        return m, y

    _pm, _py = _pp("pro")
    _em, _ey = _pp("enterprise")
    _pm_raw, _ = plan_price(settings, "pro", "stripe", "monthly")
    _py_raw, _ = plan_price(settings, "pro", "stripe", "yearly")
    _save = (
        f'-{round((1 - _py_raw / (_pm_raw * 12)) * 100)}%'
        if _pm_raw and _py_raw and _py_raw < _pm_raw * 12
        else ""
    )
    plans = [
        ("Starter", "0€", None, "Pour tester et prototyper",
         ["1 tenant", "Clés API + HMAC instantanées", "Provider console (logs)", "Rate limiting inclus"],
         False),
        ("Pro", _pm or "Sur mesure", _py, "Pour les apps en production",
         ["Tenants illimités", "SMTP / Brevo / Resend", "Branding par tenant", "Fallback SMS par pays", "Support prioritaire"],
         True),
        ("Enterprise", _em or "Sur mesure", _ey, "Volume élevé & conformité",
         ["Instance dédiée", "SLA & quotas personnalisés", "Audit & logs avancés", "Accompagnement intégration"],
         False),
    ]

    def _price_html(m: str, y: str | None) -> str:
        if not y:
            return f'<p class="text-3xl font-black mt-2">{m}</p>'
        return (
            '<p class="text-3xl font-black mt-2">'
            f'<span data-m>{m}<span class="text-base font-semibold text-slate-400">/mois</span></span>'
            f'<span data-y class="hidden">{y}<span class="text-base font-semibold text-slate-400">/an</span></span>'
            "</p>"
        )

    plans_html = "".join(
        f"""<div class="card {'plan-pop scale-[1.03]' if hot else 'card-lift'} p-7 transition-all relative reveal reveal-d{i + 1}">
          {'<span class="badge bg-brand-600 text-white absolute -top-3 left-1/2 -translate-x-1/2">POPULAIRE</span>' if hot else ''}
          <h3 class="font-bold text-lg">{name}</h3>
          {_price_html(m_price, y_price)}
          <p class="text-sm text-slate-500 mt-1 mb-5">{desc}</p>
          <ul class="space-y-2.5 text-sm text-slate-600">
            {''.join(f'<li class="flex gap-2"><span class="check"></span>{f_}</li>' for f_ in feats)}
          </ul>
          <a href="/register" class="{'btn-primary' if hot else 'btn-outline'} w-full text-center mt-6 block">Choisir</a>
        </div>"""
        for i, (name, m_price, y_price, desc, feats, hot) in enumerate(plans)
    )

    content = f"""
<section class="hero-bg text-white relative overflow-hidden">
  <div class="absolute inset-0 grid-pattern"></div>
  <div class="orb w-96 h-96 bg-brand-500 -top-20 -left-20"></div>
  <div class="orb w-80 h-80 bg-accent-500 top-40 -right-16"></div>
  <div class="max-w-6xl mx-auto px-6 py-16 md:py-28 relative">
    <div class="grid lg:grid-cols-2 gap-14 items-center">
      <div class="text-left">
        <h1 class="text-4xl sm:text-5xl md:text-[3.6rem] font-black tracking-tight leading-[1.06] fade-up fade-up-1">
          Vos codes de vérification et emails,
          <span class="gradient-text">en un appel API.</span>
        </h1>
        <p class="mt-6 text-base sm:text-lg text-slate-300 leading-relaxed fade-up fade-up-2">
          Une seule API multi-tenant pour vos OTP, emails de bienvenue,
          notifications et confirmations. Créez un compte, récupérez vos clés,
          intégrez — c'est parti.
        </p>
        <div class="mt-9 flex flex-col sm:flex-row gap-4 fade-up fade-up-3">
          <a href="/register" class="btn-primary btn-shine text-base px-8 py-3.5 text-center">Créer un compte gratuit</a>
          {'<a href="/demo" class="btn-ghost text-base text-center">Essayer la console</a>' if settings.demo_enabled else '<a href="/docs" class="btn-ghost text-base text-center">Documentation API</a>'}
        </div>
        <div class="mt-10 flex flex-wrap gap-x-7 gap-y-2 text-xs text-slate-400 fade-up fade-up-3">
          <span class="inline-flex items-center gap-1.5"><span class="check check-dark"></span> Sans carte bancaire</span>
          <span class="inline-flex items-center gap-1.5"><span class="check check-dark"></span> Self-hostable</span>
          <span class="inline-flex items-center gap-1.5"><span class="check check-dark"></span> Sécurité HMAC-SHA256</span>
        </div>
      </div>
      <div class="relative fade-up fade-up-2 hidden lg:block">
        <div class="glass rounded-2xl p-1.5 shadow-2xl">
          <div class="bg-[#0a0b14]/80 rounded-xl overflow-hidden">
            <div class="flex items-center gap-2 px-4 py-3 border-b border-white/10">
              <span class="w-3 h-3 rounded-full bg-rose-400"></span>
              <span class="w-3 h-3 rounded-full bg-amber-400"></span>
              <span class="w-3 h-3 rounded-full bg-emerald-400"></span>
              <span class="text-xs text-slate-400 ml-2 font-mono">api.sh</span>
            </div>
            <pre class="p-5 text-[13px] leading-relaxed font-mono text-slate-300"><span class="text-slate-500"># Envoyer un OTP</span>
<span class="text-accent-400">curl</span> -X POST {str('/v1/otp/send')} \\
  -H <span class="text-emerald-300">"X-API-Key: ••••"</span> \\
  -d <span class="text-amber-300">'{{"email":"client@ex.com","purpose":"login_2fa"}}'</span>

<span class="text-slate-500"># Réponse</span>
<span class="text-emerald-300">{{"channel":"email","request_id":"req_…","expires_in":300}}</span></pre>
          </div>
        </div>
        <div class="floaty absolute -top-6 -right-5 bg-white rounded-xl shadow-xl border border-slate-200 px-4 py-3 flex items-center gap-3">
          <span class="w-9 h-9 rounded-full bg-emerald-100 text-emerald-600 flex items-center justify-center">{UI_ICONS['check']}</span>
          <div>
            <p class="text-xs font-bold text-slate-800">Email délivré</p>
            <p class="text-[11px] text-slate-400">il y a 0,4 s — boîte de réception</p>
          </div>
        </div>
        <div class="floaty floaty-2 absolute -bottom-7 -left-5 bg-[#0d0f1e] rounded-xl shadow-xl border border-white/10 px-4 py-3 flex items-center gap-3">
          <span class="w-9 h-9 rounded-lg bg-brand-600/20 text-brand-300 flex items-center justify-center font-mono text-[10px] font-bold">OTP</span>
          <div>
            <p class="text-[11px] text-slate-400">Code de vérification</p>
            <p class="text-sm font-bold text-white font-mono tracking-[0.3em]">8 4 2 1 9 6</p>
          </div>
        </div>
      </div>
    </div>
  </div>
  <div class="absolute -bottom-px left-0 right-0 h-24 bg-gradient-to-t from-slate-50 to-transparent"></div>
</section>

<section class="max-w-6xl mx-auto px-6 -mt-8 relative z-10 pb-4">
  <div class="grid grid-cols-2 md:grid-cols-4 gap-4">
    <div class="card kpi p-5 text-center reveal"><p class="text-2xl font-black text-brand-600" data-count="6">0</p><p class="text-xs text-slate-500 mt-1">Endpoints sécurisés</p></div>
    <div class="card kpi p-5 text-center reveal reveal-d1"><p class="text-2xl font-black text-brand-600">&lt;30ms</p><p class="text-xs text-slate-500 mt-1">Latence API</p></div>
    <div class="card kpi p-5 text-center reveal reveal-d2"><p class="text-2xl font-black text-brand-600">∞</p><p class="text-xs text-slate-500 mt-1">Tenants</p></div>
    <div class="card kpi p-5 text-center reveal reveal-d3"><p class="text-2xl font-black text-brand-600">HMAC</p><p class="text-xs text-slate-500 mt-1">SHA-256 signé</p></div>
  </div>
</section>

<section id="services" class="max-w-6xl mx-auto px-6 py-14 md:py-20">
  <div class="text-center mb-12 reveal">
    <h2 class="text-3xl md:text-4xl font-extrabold">Tout pour vos emails & OTP</h2>
    <p class="text-slate-500 mt-3 max-w-xl mx-auto">Une plateforme complète, pensée pour les développeurs et les produits qui grandissent.</p>
  </div>
  <div class="grid md:grid-cols-3 gap-5">{services_html}</div>
</section>

<section class="max-w-6xl mx-auto px-6 py-16">
  <div class="text-center mb-10 reveal">
    <h2 class="text-3xl font-extrabold">Comment ça marche</h2>
    <p class="text-slate-500 mt-2">Trois étapes et votre application envoie des emails.</p>
  </div>
  <div class="grid md:grid-cols-3 gap-6">
    <div class="card step p-7 text-center transition-all reveal">
      <div class="step-ic w-11 h-11 mx-auto rounded-full bg-gradient-to-br from-brand-500 to-accent-600 text-white font-bold flex items-center justify-center mb-4 shadow-lg shadow-brand-500/30">1</div>
      <h3 class="font-bold mb-2">Créez votre compte</h3>
      <p class="text-sm text-slate-500">Un tenant + une clé API + un secret HMAC sont générés instantanément.</p>
    </div>
    <div class="card step p-7 text-center transition-all reveal reveal-d1">
      <div class="step-ic w-11 h-11 mx-auto rounded-full bg-gradient-to-br from-brand-500 to-accent-600 text-white font-bold flex items-center justify-center mb-4 shadow-lg shadow-brand-500/30">2</div>
      <h3 class="font-bold mb-2">Configurez votre expéditeur</h3>
      <p class="text-sm text-slate-500">Votre adresse email et votre SMTP dans le dashboard — test intégré.</p>
    </div>
    <div class="card step p-7 text-center transition-all reveal reveal-d2">
      <div class="step-ic w-11 h-11 mx-auto rounded-full bg-gradient-to-br from-brand-500 to-accent-600 text-white font-bold flex items-center justify-center mb-4 shadow-lg shadow-brand-500/30">3</div>
      <h3 class="font-bold mb-2">Intégrez en un appel</h3>
      <p class="text-sm text-slate-500">POST signé HMAC vers <code>/v1/otp/send</code> ou <code>/v1/email/send</code>.</p>
    </div>
  </div>
</section>

<section class="max-w-6xl mx-auto px-6 py-14 md:py-20">
  <div class="grid lg:grid-cols-2 gap-10 lg:gap-14 items-center">
    <div class="reveal">
      <p class="text-xs font-bold uppercase tracking-wider text-accent-500 mb-3">Délivrabilité</p>
      <h2 class="text-3xl md:text-4xl font-extrabold mb-4">Vos emails arrivent à destination</h2>
      <p class="text-slate-500 leading-relaxed mb-6">File de retry intelligente, fallback SMTP et fallback SMS
      géographique : un envoi qui échoue retente automatiquement, jamais perdu.</p>
      <ul class="space-y-3 text-sm text-slate-600">
        <li class="flex gap-2.5"><span class="check"></span>File d'attente avec retries programmés — aucun email perdu</li>
        <li class="flex gap-2.5"><span class="check"></span>Fallback SMS automatique pour les pays à risque</li>
        <li class="flex gap-2.5"><span class="check"></span>Statut temps réel : queued, retry, sent, dead-letter</li>
      </ul>
      <a href="/documentation" class="btn-outline text-sm mt-7 inline-flex items-center gap-2">Voir la documentation <span class="font-bold">→</span></a>
    </div>
    <div class="relative reveal reveal-d1 img-frame">
      <div class="frame-deco absolute -inset-3 bg-gradient-to-br from-brand-200/70 to-accent-200/50 rounded-[1.6rem] -rotate-2"></div>
      <div class="img-zoom relative rounded-2xl shadow-2xl">
      <img src="https://images.unsplash.com/photo-1596526131083-e8c633c948d2?w=1000&q=80&auto=format&fit=crop"
           alt="Application email sur smartphone" loading="lazy"
           class="rounded-2xl w-full object-cover aspect-[4/3]">
      </div>
      <div class="floaty absolute -bottom-5 left-5 card px-4 py-3 flex items-center gap-3">
        <span class="w-9 h-9 rounded-full bg-emerald-100 text-emerald-600 flex items-center justify-center">{UI_ICONS['check']}</span>
        <div>
          <p class="text-xs font-bold text-slate-800">Délivré en boîte de réception</p>
          <p class="text-[11px] text-slate-400">OTP reçu — pas de spam</p>
        </div>
      </div>
    </div>
  </div>
</section>

<section class="max-w-6xl mx-auto px-6 py-14 md:py-20">
  <div class="grid lg:grid-cols-2 gap-10 lg:gap-14 items-center">
    <div class="relative reveal img-frame">
      <div class="frame-deco absolute -inset-3 bg-gradient-to-br from-accent-200/50 to-brand-200/70 rounded-[1.6rem] rotate-2"></div>
      <div class="img-zoom relative rounded-2xl shadow-2xl">
      <img src="https://images.unsplash.com/photo-1607799279861-4dd421887fb3?w=1000&q=80&auto=format&fit=crop"
           alt="Code de l'API dans un éditeur" loading="lazy"
           class="rounded-2xl w-full object-cover aspect-[4/3]">
      </div>
      <div class="floaty floaty-2 absolute -top-5 right-5 bg-[#0d0f1e] rounded-xl shadow-xl border border-white/10 px-4 py-2.5">
        <p class="font-mono text-[11px] text-slate-400">POST <span class="text-accent-400">/v1/otp/send</span>
        <span class="text-emerald-400 ml-2">200 OK</span></p>
      </div>
    </div>
    <div class="reveal reveal-d1">
      <p class="text-xs font-bold uppercase tracking-wider text-brand-500 mb-3">API-first</p>
      <h2 class="text-3xl md:text-4xl font-extrabold mb-4">Pensée pour les développeurs</h2>
      <p class="text-slate-500 leading-relaxed mb-6">Documentation OpenAPI interactive, exemples clients prêts
      à l'emploi, console de test intégrée et réponses JSON propres.</p>
      <ul class="space-y-3 text-sm text-slate-600">
        <li class="flex gap-2.5"><span class="check"></span>Swagger /docs pour tester chaque endpoint en direct</li>
        <li class="flex gap-2.5"><span class="check"></span>Client Python d'exemple avec signature HMAC</li>
        <li class="flex gap-2.5"><span class="check"></span>Idempotency-Key intégrée — zéro envoi en double</li>
      </ul>
      <a href="/docs" class="btn-outline text-sm mt-7 inline-flex items-center gap-2">Explorer l'API <span class="font-bold">→</span></a>
    </div>
  </div>
</section>

<section class="max-w-6xl mx-auto px-6 py-14">
  <div class="card overflow-hidden reveal">
    <div class="px-5 sm:px-8 pt-7 pb-5 border-b border-slate-100 flex flex-col sm:flex-row sm:items-center justify-between gap-3">
      <div>
        <h2 class="font-bold text-xl">Endpoints API</h2>
        <p class="text-sm text-slate-500 mt-1">Toutes les routes sous <code>/v1/*</code> requièrent une signature HMAC.</p>
      </div>
    </div>
    <div class="overflow-x-auto">
    <table class="dt min-w-[620px]">
      <thead><tr>
        <th class="pl-5 sm:pl-8">Méthode</th>
        <th>Endpoint</th>
        <th class="pr-5 sm:pr-8">Description</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
    </div>
  </div>
</section>

<section id="pricing" class="max-w-6xl mx-auto px-6 py-14 md:py-20">
  <div class="text-center mb-10 reveal">
    <h2 class="text-3xl md:text-4xl font-extrabold">Simple et transparent</h2>
    <p class="text-slate-500 mt-3 mb-7">Commencez gratuitement, passez à l'échelle quand vous êtes prêt.</p>
    <div class="seg-wrap" data-billing-toggle>
      <button type="button" class="seg-btn active" data-bill="m" data-noload>Mensuel</button>
      <button type="button" class="seg-btn" data-bill="y" data-noload>Annuel{f'<span class="seg-save">{_save}</span>' if _save else ''}</button>
    </div>
  </div>
  <div class="grid md:grid-cols-3 gap-6 items-stretch pt-3">{plans_html}</div>
</section>

<section class="max-w-6xl mx-auto px-6 pb-24">
  <div class="hero-bg rounded-3xl p-8 sm:p-12 md:p-16 text-center text-white relative overflow-hidden reveal">
    <div class="orb w-72 h-72 bg-accent-500 -top-10 -right-10"></div>
    <h2 class="text-3xl md:text-4xl font-extrabold relative">Prêt à envoyer votre premier email ?</h2>
    <p class="text-slate-300 mt-4 relative">Compte gratuit, clés instantanées, aucune carte requise.</p>
    <a href="/register" class="btn-primary btn-shine inline-block mt-8 relative px-8 py-3.5">Commencer maintenant →</a>
  </div>
</section>
"""
    return _landing_page(content)


_AUTH_SIDES = {
    "login": (
        "https://images.unsplash.com/photo-1557200134-90327ee9fafa?w=1200&q=80&auto=format&fit=crop",
        "Boîte de réception sur un ordinateur portable",
        "Vos emails, votre console.",
        ["Statistiques d'envoi en temps réel", "Clés API et webhooks sortants", "Support intégré au dashboard"],
    ),
    "register": (
        "https://images.unsplash.com/photo-1522071820081-009f0129c71c?w=1200&q=80&auto=format&fit=crop",
        "Une équipe en train de développer",
        "Rejoignez les équipes qui envoient.",
        ["Compte gratuit — sans carte bancaire", "Clés API instantanées", "Premier envoi en 5 minutes"],
    ),
    "generic": (
        "https://images.unsplash.com/photo-1607799279861-4dd421887fb3?w=1200&q=80&auto=format&fit=crop",
        "Code d'API dans un éditeur",
        "Une API, tous vos emails.",
        ["OTP et emails transactionnels", "Multi-tenant & branding", "Sécurité HMAC-SHA256"],
    ),
}


def _auth_form(title: str, action: str, fields: str, link: str, side: str = "generic") -> HTMLResponse:
    img, img_alt, headline, points = _AUTH_SIDES.get(side, _AUTH_SIDES["generic"])
    bullets = "".join(
        f'<li class="flex gap-2.5"><span class="check check-dark"></span>{pt}</li>'
        for pt in points
    )
    return _landing_page(f"""
<div class="grid lg:grid-cols-2 min-h-[calc(100vh_-_4rem)]">
  <div class="flex items-center justify-center px-4 sm:px-6 py-10 sm:py-14 order-2 lg:order-1">
    <div class="w-full max-w-md fade-up">
      <div class="text-center mb-6 lg:hidden">
        <a href="/" class="inline-flex items-center gap-2 font-extrabold text-lg">{LOGO_FULL}</a>
      </div>
      <div class="card p-6 sm:p-8 shadow-lg">
        <h1 class="text-2xl font-extrabold mb-1">{title}</h1>
        <p class="text-sm text-slate-500 mb-6">Accédez à votre espace MailHub</p>
        <form method="post" action="{action}" class="space-y-4">
          {fields}
          <button type="submit" class="btn-primary btn-shine w-full">{title}</button>
        </form>
        <p class="text-xs text-slate-400 mt-6 text-center">{link}</p>
      </div>
      <p class="text-center text-xs text-slate-400 mt-5">
        Sécurisé par clé API + signature HMAC-SHA256
      </p>
    </div>
  </div>
  <div class="hidden lg:block relative overflow-hidden order-1 lg:order-2">
    <img src="{img}" alt="{img_alt}" class="absolute inset-0 w-full h-full object-cover">
    <div class="absolute inset-0 bg-gradient-to-t from-[#0a0b14] via-[#0a0b14]/70 to-[#0a0b14]/25"></div>
    <div class="relative z-10 flex flex-col justify-between h-full p-10 xl:p-14 text-white">
      <a href="/" class="inline-flex items-center gap-2.5 font-extrabold text-lg">{LOGO_FULL}</a>
      <div class="fade-up fade-up-2">
        <h2 class="text-3xl xl:text-4xl font-extrabold mb-5 leading-tight">{headline}</h2>
        <ul class="space-y-3 text-sm text-slate-200">{bullets}</ul>
      </div>
    </div>
    <div class="orb w-72 h-72 bg-brand-500 -bottom-16 -left-16"></div>
    <div class="orb w-60 h-60 bg-accent-500 -top-10 -right-10"></div>
  </div>
</div>
""", footer=False)


@router.get("/register", response_class=HTMLResponse)
async def register_page(
    request: Request, error: str = "", settings: Settings = Depends(get_settings)
) -> HTMLResponse:
    if await _current_user(request):
        return RedirectResponse("/dashboard", status_code=303)
    if not settings.web_signup_enabled:
        raise HTTPException(status_code=404)
    err = f'<div class="alert alert-err mb-4">{UI_ICONS["error"]}<span>{_e(error)}</span></div>' if error else ""
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
    link = 'Déjà un compte ? <a href="/login" class="text-brand-600 font-semibold">Se connecter</a>'
    return _auth_form("Créer un compte", "/register", fields, link, side="register")




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
            f'<div class="alert alert-err mb-4">{UI_ICONS["error"]}<span>Code d\'invitation invalide.</span></div>',
            '<a href="/login" class="text-brand-600">Se connecter</a>',
            side="register",
        )
    if len(password) < 8:
        return _auth_form(
            "Créer un compte", "/register",
            f'<div class="alert alert-err mb-4">{UI_ICONS["error"]}<span>Le mot de passe doit contenir au moins 8 caractères.</span></div>',
            '<a href="/login" class="text-brand-600">Se connecter</a>',
            side="register",
        )
    if "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        return _auth_form(
            "Créer un compte", "/register",
            f'<div class="alert alert-err mb-4">{UI_ICONS["error"]}<span>Adresse email invalide.</span></div>',
            '<a href="/login" class="text-brand-600">Se connecter</a>',
            side="register",
        )

    redis = request.app.state.redis
    users = WebUserStore(redis)
    if await users.get_by_email(email):
        return _auth_form(
            "Créer un compte", "/register",
            f'<div class="alert alert-err mb-4">{UI_ICONS["error"]}<span>Cet email est déjà utilisé.</span></div>',
            '<a href="/login" class="text-brand-600">Se connecter</a>',
            side="register",
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
    err = f'<div class="alert alert-err mb-4">{UI_ICONS["error"]}<span>{_e(error)}</span></div>' if error else ""
    att = (
        f'<p class="text-xs text-slate-500 mt-1">Tentatives restantes : {attempts}</p>'
        if attempts is not None else ""
    )
    dbg = ""
    if debug_code:
        dbg = (
            '<div class="alert alert-warn mt-3">'
            f"{UI_ICONS['warn']}<span>Mode debug — code : <b>{_e(debug_code)}</b></span></div>"
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
        '<button type="submit" class="text-brand-600 font-semibold bg-transparent border-0 '
        'p-0 cursor-pointer text-xs">Renvoyer le code</button></form>'
        ' · <a href="/register" class="text-brand-600 font-semibold">Recommencer</a>'
    )
    return _auth_form("Vérifier mon email", "/register/verify", fields, link, side="register")


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
            f'<div class="alert alert-err mb-4">{UI_ICONS["error"]}<span>Cet email est déjà utilisé.</span></div>',
            '<a href="/login" class="text-brand-600">Se connecter</a>',
            side="register",
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
    link = 'Pas de compte ? <a href="/register" class="text-brand-600 font-semibold">Créer un compte</a>'
    return _auth_form("Connexion", "/login", fields, link, side="login")


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
            f'<div class="alert alert-err mb-4">{UI_ICONS["error"]}<span>Email ou mot de passe incorrect.</span></div>',
            '<a href="/register" class="text-brand-600">Créer un compte</a>',
            side="login",
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
    billing: str = "",
    apage: int = 1,
    atype: str = "",
    ppage: int = 1,
    pstatus: str = "",
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    user = await _require_user(request)
    redis = request.app.state.redis
    tenant = await TenantStore(redis).get(user.tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant introuvable")

    banner = ""
    if billing == "ok":
        banner = f"""<div class="alert alert-ok mb-5">{UI_ICONS['check']}
          <span>Paiement confirmé — votre plan est actif.</span></div>"""
    elif billing == "pending":
        banner = f"""<div class="alert alert-info mb-5">{UI_ICONS['clock']}
          <span>Paiement reçu — activation en cours de confirmation, votre plan sera
          appliqué automatiquement dans quelques instants.</span></div>"""
    elif billing == "cancel":
        banner = f"""<div class="alert alert-warn mb-5">{UI_ICONS['warn']}
          <span>Paiement annulé — aucun montant n'a été débité.</span></div>"""
    elif billing.startswith("err"):
        banner = f"""<div class="alert alert-err mb-5">{UI_ICONS['error']}
          <span>Le paiement n'a pas pu être initialisé — réessayez ou contactez le support.</span></div>"""
    if test == "ok":
        banner = f"""<div class="alert alert-ok mb-5">{UI_ICONS['check']}
          <span>Email de test envoyé — vérifiez la boîte de réception (et les spams).</span></div>"""
    elif test:
        banner = f"""<div class="alert alert-err mb-5">{UI_ICONS['error']}
          <span>Échec de l'envoi test : {_e(test)}<br>
          <span class="font-normal opacity-80">Vérifiez SMTP Host / Port / User / Password. Rappel : smtp.gmail.com n'accepte
          que les comptes Google — pour une adresse pro, utilisez le serveur SMTP de son hébergeur
          (ex. IONOS : smtp.ionos.fr).</span></span></div>"""

    # Credentials (affichés si encore en cache — sinon régénérables)
    creds = await redis.get(f"webuser:creds:{tenant.tenant_id}")
    if creds:
        api_key, hmac_secret = (_e(v) for v in creds.split("|"))
        creds_html = f"""
        <div class="mt-4 space-y-3">
          <div>
            <p class="text-xs font-semibold text-slate-500 mb-1">Clé API (header <code>X-API-Key</code>)</p>
            <div class="flex flex-col sm:flex-row gap-2"><code class="flex-1 min-w-0 text-xs break-all bg-slate-50 p-2 rounded">{api_key}</code>
            <button data-copy="{api_key}" class="btn-soft">Copier</button></div>
          </div>
          <div>
            <p class="text-xs font-semibold text-slate-500 mb-1">Secret HMAC (signature des requêtes)</p>
            <div class="flex flex-col sm:flex-row gap-2"><code class="flex-1 min-w-0 text-xs break-all bg-slate-50 p-2 rounded">{hmac_secret}</code>
            <button data-copy="{hmac_secret}" class="btn-soft">Copier</button></div>
          </div>
        </div>"""
    else:
        creds_html = f"""
        <div class="alert alert-warn mt-4">{UI_ICONS['warn']}
          <span>Vos clés sont masquées pour sécurité (affichées 24 h après génération).
          Pour obtenir une nouvelle clé API + secret, cliquez sur <b>« Régénérer mes clés »</b>
          ci-dessous — elles s'afficheront immédiatement ici.</span></div>"""

    stats_keys = [
        ("Emails envoyés", "stats:emails_sent",
         '<svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linejoin="round" d="M3 8l9 6 9-6M4 6h16a1 1 0 011 1v10a1 1 0 01-1 1H4a1 1 0 01-1-1V7a1 1 0 011-1z"/></svg>',
         "bg-brand-50 text-brand-600"),
        ("OTP envoyés", "stats:otps_sent",
         '<svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" d="M12 19l7-7-7-7M5 12h14"/></svg>',
         "bg-amber-50 text-amber-600"),
        ("OTP vérifiés", "stats:otps_verified",
         '<svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M9 12l2 2 4-4m5 2a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>',
         "bg-emerald-50 text-emerald-600"),
    ]
    # Santé expéditeur : badge SMTP + résultat DNS en cache
    if tenant.smtp_status == "ok":
        smtp_status_badge = '<span class="badge bg-emerald-100 text-emerald-700">SMTP OK</span>'
    elif tenant.smtp_status == "failed":
        smtp_status_badge = '<span class="badge bg-rose-100 text-rose-700">SMTP EN PANNE</span>'
    else:
        smtp_status_badge = '<span class="badge bg-slate-100 text-slate-500">SMTP non testé</span>'
    # Warm-up : SMTP custom récent = volume journalier plafonné progressif
    from src.services.warmup import warmup_state
    wu_remaining, wu_cap, wu_sent = await warmup_state(redis, settings, tenant)
    if wu_cap is not None:
        days_left = ""
        if tenant.smtp_configured_at:
            d = int((time.time() - tenant.smtp_configured_at) / 86400) + 1
            days_left = f"· jour {min(d, settings.warmup_days)}/{settings.warmup_days}"
        smtp_status_badge += (
            f' <span class="badge bg-amber-100 text-amber-700" '
            f'title="Montée en charge : le volume autorisé augmente '
            f'automatiquement chaque jour">Warm-up {days_left} · '
            f'{wu_sent}/{wu_cap} aujourd\'hui</span>'
        )
    if tenant.smtp_status == "failed":
        smtp_health_html = (
            f'<p class="text-xs text-rose-600 font-semibold">Échec : '
            f'{_e(tenant.smtp_status_error)}</p>'
        )
    elif tenant.smtp_status == "ok":
        smtp_health_html = (
            '<p class="text-xs text-emerald-600">Connexion et identifiants '
            'vérifiés — les emails peuvent partir.</p>'
        )
    else:
        smtp_health_html = (
            '<p class="text-xs text-slate-400">Testez la connexion (EHLO, TLS, '
            'identifiants) sans envoyer de mail — contrôle auto toutes les heures.</p>'
        )

    dns_raw = await redis.get(f"{tenant.key_prefix()}dnscheck")
    dns = json.loads(dns_raw) if dns_raw else None
    if dns:
        def _b(ok: bool, label: str, detail: str = "") -> str:
            cls = "bg-emerald-100 text-emerald-700" if ok else "bg-rose-100 text-rose-700"
            tip = f' title="{_e(detail)}"' if detail else ""
            return (f'<span class="badge {cls} cursor-help"{tip}>'
                    f'{"✓" if ok else "✗"} {label}</span>')
        all_ok = all((dns["mx"], dns["spf_ok"], dns["dkim_ok"], dns["dmarc_ok"]))
        dns_check_html = (
            f'<p class="text-[11px] text-slate-400 mb-1.5">Domaine : '
            f'<code>{_e(dns["domain"])}</code></p>'
            f'<div class="flex flex-wrap gap-1.5">'
            f'{_b(dns["mx"], "MX")}'
            f'{_b(dns["spf_ok"], "SPF", dns["spf"])}'
            f'{_b(dns["dkim_ok"], "DKIM", dns["dkim_selector"])}'
            f'{_b(dns["dmarc_ok"], "DMARC", dns["dmarc"])}'
            f'</div>'
            + ('<p class="text-[11px] text-emerald-600 mt-1.5">Domaine vérifié — '
               'délivrabilité optimale.</p>' if all_ok
               else '<p class="text-[11px] text-rose-600 mt-1.5">Enregistrements '
                    'manquants : ajoutez-les dans la zone DNS du domaine (chez '
                    'votre hébergeur), sinon vos mails arriveront en spam.</p>')
        )
    else:
        dns_check_html = (
            '<p class="text-[11px] text-slate-400">Vérifiez MX / SPF / DKIM / DMARC '
            'du domaine expéditeur — les 4 enregistrements qui évitent les spams.</p>'
        )

    stats_cards = ""
    for label, key, ic, icc in stats_keys:
        val = await redis.get(f"{tenant.key_prefix()}{key}") or "0"
        stats_cards += f"""
        <div class="card kpi p-5 flex items-center gap-4">
          <span class="kpi-ic {icc}">{ic}</span>
          <div class="min-w-0">
            <p class="text-[11px] font-semibold text-slate-500 uppercase tracking-wider">{label}</p>
            <p class="kpi-num mt-0.5">{_e(val)}</p>
          </div>
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
            <div class="w-4 rounded-t bg-gradient-to-t from-brand-500 to-accent-500" style="height:{max(h_sent,4) if series['otps_sent'][i] else 2}%" title="{series['otps_sent'][i]} OTP envoyés"></div>
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
        "otp_sms_fallback": ("Fallback SMS", "bg-accent-100 text-accent-700"),
        "otp_purpose_denied": ("Purpose refusé", "bg-slate-200 text-slate-600"),
        "email_sent": ("Email envoyé", "bg-emerald-100 text-emerald-700"),
        "email_failed": ("Email échoué", "bg-rose-100 text-rose-700"),
        "email_rate_limited": ("Rate limit", "bg-amber-100 text-amber-700"),
        "smtp_failover": ("SMTP secours", "bg-amber-100 text-amber-700"),
        "smtp_health_failed": ("SMTP en panne", "bg-rose-100 text-rose-700"),
        "email_bounced": ("Bounce", "bg-rose-100 text-rose-700"),
        "email_softbounced": ("Bounce léger", "bg-amber-100 text-amber-700"),
        "email_complained": ("Plainte spam", "bg-rose-100 text-rose-700"),
        "email_suppressed": ("Destinataire supprimé", "bg-slate-200 text-slate-600"),
        "otp_suppressed": ("OTP supprimé", "bg-slate-200 text-slate-600"),
        "warmup_limited": ("Limite warm-up", "bg-amber-100 text-amber-700"),
    }
    events = await get_events(redis, tenant, limit=200)
    atypes = sorted({e.get("type", "") for e in events if e.get("type")})
    if atype:
        events = [e for e in events if e.get("type") == atype]
    A_PER = 15
    atotal = len(events)
    apage = max(1, apage)
    events_page = events[(apage - 1) * A_PER: apage * A_PER]
    type_opts = '<option value="">Tous les types</option>' + "".join(
        f'<option value="{_e(t)}"{" selected" if t == atype else ""}>{_e(t)}</option>'
        for t in atypes
    )
    apager = _pager("/dashboard", apage, atotal, A_PER, "apage",
                    {"atype": atype}, anchor="#sec-activity")
    if events_page:
        rows = "".join(
            f"""<tr class="hover:bg-slate-50 transition">
              <td class="py-2.5 pr-3 whitespace-nowrap"><span class="badge {cls}">{lbl}</span></td>
              <td class="py-2.5 pr-3 text-slate-500 text-xs whitespace-nowrap">{_e(e.get('ts','')[11:19])} {_e(e.get('ts','')[:10])}</td>
              <td class="py-2.5 text-slate-600 text-xs truncate max-w-[220px]">{_e(e.get('email') or e.get('request_id') or e.get('email_type') or '—')}</td>
            </tr>"""
            for e in events_page
            for lbl, cls in [event_labels.get(e.get("type", ""), (e.get("type", "?"), "bg-slate-100 text-slate-600"))]
        )
        activity_html = f"""
<div id="sec-activity" class="card p-4 sm:p-6 mb-6 scroll-mt-24">
  <div class="flex items-center justify-between mb-4">
    <h2 class="font-bold">Activité récente</h2>
    <form method="get" action="/dashboard#sec-activity">
      <select name="atype" onchange="this.form.submit()"
              class="input !py-1.5 !px-2 text-xs">{type_opts}</select>
    </form>
  </div>
  <div class="overflow-x-auto"><table class="w-full text-sm">
    <tbody class="divide-y divide-slate-100">{rows}</tbody>
  </table></div>
  {apager}
</div>"""
    else:
        activity_html = f"""
<div id="sec-activity" class="card p-4 sm:p-6 mb-6 scroll-mt-24">
  <h2 class="font-bold mb-4">Activité récente</h2>
  <div class="empty"><span class="empty-ic">{UI_ICONS['inbox']}</span>
    <p>Aucune activité pour le moment — vos envois apparaîtront ici.</p></div>
</div>"""

    base_url = _e(str(request.base_url).rstrip("/"))

    cur_plan = tenant.plan if tenant and tenant.plan in PLANS else "free"
    pending_plan = tenant.plan_requested if tenant else None
    scheduled_plan = tenant.plan_scheduled if tenant else None

    def _plan_price_label(pid: str) -> str:
        amt, cur = plan_price(settings, pid, "stripe", "monthly")
        return f"{amt:g} {cur}/mois" if amt else PLANS[pid]["price"]

    plan_cards = "".join(
        f'<div class="border rounded-xl p-4 flex flex-col '
        f'{"border-brand-500 ring-1 ring-brand-500" if pid == cur_plan else "border-slate-200"}">'
        f'<div class="flex items-center justify-between">'
        f"<p class=\"font-bold\">{_e(p['label'])}</p>"
        + ('<span class="badge bg-brand-100 text-brand-700">ACTUEL</span>' if pid == cur_plan else "")
        + "</div>"
        f"<p class=\"text-2xl font-black mt-1\">{_e(_plan_price_label(pid))}</p>"
        f"<p class=\"text-xs text-slate-500 mt-1\">{_e(p['desc'])}</p>"
        f'<ul class="text-[11px] text-slate-500 mt-3 space-y-1">'
        f"<li>{p['max_sends_per_user_per_10min']} envois/user/10min</li>"
        f"<li>{p['max_sends_per_ip_per_10min']} envois/IP/10min</li>"
        f"<li>Cooldown {p['cooldown_seconds']}s · {p['max_verify_attempts']} essais · OTP {p['otp_ttl_seconds']//60} min</li>"
        f"</ul>"
        f'<form method="post" action="/dashboard/plan" class="mt-auto pt-4">'
        f'<input type="hidden" name="plan" value="{_e(pid)}">'
        + (
            '<button disabled class="btn-soft w-full opacity-50 cursor-default" data-noload>Plan actuel</button>'
            if pid == cur_plan
            else (
                f'<button disabled class="btn-soft w-full !bg-amber-50 !text-amber-700 !border-amber-200 cursor-default justify-center" data-noload>{UI_ICONS["clock"]} Demande en attente</button>'
                if pid == pending_plan
                else '<button class="btn-soft w-full justify-center">Choisir ce plan</button>'
            )
        )
        + ('<p class="text-[11px] text-amber-600 mt-2 flex items-center gap-1">'
           + UI_ICONS["clock"] + f' Passage prévu le <b>{_e(tenant.paid_until or "")}</b></p>'
           if scheduled_plan and pid == scheduled_plan else "")
        + "</form></div>"
        for pid, p in PLANS.items()
    )
    pending_panel = ""
    if pending_plan and pending_plan in PLANS:
        pending_tx = next(
            (
                p for p in await list_payments(redis, tenant.tenant_id, limit=10)
                if p.get("plan") == pending_plan
                and p.get("status") in ("pending_manual", "pending_verification", "created")
            ),
            None,
        )
        providers = available_providers(settings)
        pending_period = (pending_tx.get("period") if pending_tx else None) or "monthly"
        rows = "".join(
            '<form method="post" action="/dashboard/billing/checkout" class="flex-1 min-w-[11rem]">'
            f'<input type="hidden" name="plan" value="{_e(pending_plan)}">'
            f'<input type="hidden" name="provider" value="{_e(prov)}">'
            f'<input type="hidden" name="period" value="{_e(pending_period)}">'
            f'<button class="btn-soft w-full justify-center text-sm">{_e(meta["label"])}'
            f'<b class="text-brand-600 ml-1.5">{plan_price(settings, pending_plan, prov, pending_period)[0]:g} {_e(plan_price(settings, pending_plan, prov, pending_period)[1])}</b>'
            f'<span class="text-[10px] font-normal text-slate-400 ml-1.5">· {_e(meta["via"])}</span></button></form>'
            for prov, meta in providers.items()
            if plan_price(settings, pending_plan, prov, pending_period)[0]
        )
        chosen_manual = (pending_tx.get("provider") if pending_tx else None) or ""
        if chosen_manual in MANUAL_METHODS or chosen_manual == "manual":
            meta = MANUAL_METHODS.get(chosen_manual, {"label": "Paiement manuel"})
            declared = (pending_tx or {}).get("status") == "pending_verification"
            if declared:
                pay_zone = (
                    f'<p class="text-xs text-slate-600">Moyen choisi : <b>{_e(meta["label"])}</b> — '
                    f'référence <code class="text-[11px]">{_e((pending_tx or {}).get("payer_ref") or "")}</code>.</p>'
                    '<p class="text-xs text-slate-500 mt-2">Paiement déclaré — votre plan sera '
                    'activé dès vérification par l’administrateur.</p>'
                )
            else:
                details = manual_method_details(settings, chosen_manual)
                coords = (
                    f'<p class="text-xs mt-2">Coordonnées de paiement : '
                    f'<code class="text-[11px]">{_e(details)}</code></p>'
                    if details else ""
                )
                pay_zone = (
                    f'<p class="text-xs text-slate-600">Moyen choisi : '
                    f'<b>{_e(meta["label"])}</b>.{coords}</p>'
                    f'<a href="/dashboard/billing/pay?tx={_e((pending_tx or {}).get("tx_id") or "")}" '
                    'class="inline-block mt-2.5 text-xs font-semibold text-brand-600 hover:text-brand-700" '
                    'data-noload>Déclarer mon paiement →</a>'
                )
            if rows:
                pay_zone += (
                    '<p class="text-xs font-semibold text-slate-600 mt-4 mb-2.5">'
                    'Ou payer en ligne — activation immédiate :</p>'
                    f'<div class="flex flex-wrap gap-2.5">{rows}</div>'
                )
        elif rows:
            pay_zone = (
                f'<p class="text-xs font-semibold text-slate-600 mb-2.5">'
                f'Payer en ligne — activation immédiate après confirmation :</p>'
                f'<div class="flex flex-wrap gap-2.5">{rows}</div>'
                f'<p class="text-[11px] text-slate-400 mt-3">Virement, cash ou autre moyen ? '
                f'Votre demande reste en attente — l’administrateur l’activera manuellement '
                f'après réception du paiement.</p>'
            )
        else:
            pay_zone = (
                '<p class="text-xs text-slate-500">Le paiement en ligne n’est pas activé sur cette '
                'instance. Votre demande sera validée manuellement par l’administrateur dès '
                'réception du paiement — contactez le support ci-dessous pour les modalités.</p>'
            )
        pending_panel = f"""
  <div class="mt-5 rounded-xl border border-amber-200 bg-amber-50/60 p-4">
    <div class="flex items-center gap-2 mb-3">
      <span class="text-amber-600">{UI_ICONS['clock']}</span>
      <h3 class="font-bold text-sm">Finaliser le paiement — <span class="text-brand-600">{_e(PLANS[pending_plan]['label'])}</span>
      <span class="text-xs font-normal text-slate-400 ml-1">· {PERIODS.get(pending_period, PERIODS["monthly"])["label"]}</span></h3>
    </div>
    {pay_zone}
    <form method="post" action="/dashboard/plan/cancel" class="mt-3">
      <button class="text-[11px] text-slate-400 hover:text-rose-600 transition underline underline-offset-2" data-noload>
        Annuler la demande</button>
    </form>
  </div>"""

    payments = await list_payments(redis, tenant.tenant_id, limit=500)
    pstatuses = sorted({p.get("status") for p in payments})
    if pstatus:
        payments = [p for p in payments if p.get("status") == pstatus]
    P_PER = 8
    ptotal = len(payments)
    ppage = max(1, ppage)
    payments = payments[(ppage - 1) * P_PER: ppage * P_PER]
    _pst_names = {"paid": "Payé", "created": "En attente",
                  "pending_manual": "En attente",
                  "pending_verification": "À vérifier", "failed": "Échoué"}
    pst_opts = '<option value="">Tous les statuts</option>' + "".join(
        f'<option value="{_e(st)}"{" selected" if st == pstatus else ""}>'
        f'{_e(_pst_names.get(st, st))}</option>'
        for st in pstatuses
    )
    ppager = _pager("/dashboard", ppage, ptotal, P_PER, "ppage",
                    {"pstatus": pstatus}, anchor="#sec-payments")
    pay_history = ""
    if payments:
        _st = {"paid": ("Payé", "bg-emerald-100 text-emerald-700"),
               "created": ("En attente", "bg-amber-100 text-amber-700"),
               "pending_manual": ("En attente", "bg-amber-100 text-amber-700"),
               "failed": ("Échoué", "bg-rose-100 text-rose-700")}
        prow = "".join(
            f'<tr class="hover:bg-slate-50 transition">'
            f'<td class="py-2 pr-3 text-xs text-slate-500 whitespace-nowrap">{_e(p.get("date") or "—")}</td>'
            f'<td class="py-2 pr-3"><span class="badge bg-brand-50 text-brand-700">{_e(PLANS.get(p.get("plan"), {}).get("label") or "—")}</span></td>'
            f'<td class="py-2 pr-3 text-xs font-semibold whitespace-nowrap">{_e(str(p.get("amount") or "—"))} {_e(p.get("currency") or "")}</td>'
            f'<td class="py-2 pr-3 text-xs text-slate-500">{_e(p.get("method") or p.get("provider") or "—")}</td>'
            f'<td class="py-2"><span class="badge {_st.get(p.get("status"), ("—", "bg-slate-100 text-slate-500"))[1]}">'
            f'{_st.get(p.get("status"), ("—", ""))[0]}</span></td></tr>'
            for p in payments
        )
        pay_history = f"""
<div id="sec-payments" class="card p-4 sm:p-6 mb-6 scroll-mt-24">
  <div class="flex items-center justify-between mb-4">
    <h2 class="font-bold">Historique des paiements</h2>
    <form method="get" action="/dashboard#sec-payments">
      <select name="pstatus" onchange="this.form.submit()"
              class="input !py-1.5 !px-2 text-xs">{pst_opts}</select>
    </form>
  </div>
  <div class="overflow-x-auto"><table class="w-full text-sm">
    <tbody class="divide-y divide-slate-100">{prow}</tbody></table></div>
  {ppager}
</div>"""

    plan_card_html = f"""
<div id="sec-plan" class="card p-4 sm:p-6 mb-6 scroll-mt-24">
  <h2 class="font-bold mb-1">Votre abonnement</h2>
  <p class="text-sm text-slate-500 mb-4">Plan actuel : <b>{_e(PLANS[cur_plan]['label'])}</b> —
  les plans payants sont activés après validation du paiement.</p>
  <div class="grid sm:grid-cols-3 gap-3">{plan_cards}</div>
  {pending_panel}
</div>"""

    plan_label = _e(PLANS.get(tenant.plan, {}).get("label", tenant.plan.upper()))
    plan_badge = (
        '<span class="badge bg-amber-100 text-amber-700">PARTENAIRE ∞</span>'
        if tenant.unlimited else
        f'<span class="badge bg-brand-50 text-brand-700 border border-brand-100">{plan_label}</span>'
    )
    # Mode sandbox : bannière + boîte de capture des envois simulés
    sandbox_html = ""
    if tenant.sandbox:
        from src.services.sandbox import list_outbox
        outbox = await list_outbox(redis, tenant.tenant_id, 10)
        sb_rows = ""
        for m in outbox:
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(m.get("ts", 0)))
            sb_rows += f"""
        <tr class="border-t border-slate-100">
          <td class="py-2 pl-5 pr-4 text-xs text-slate-500 whitespace-nowrap">{ts}</td>
          <td class="py-2 pr-4 text-xs">{_e(m.get("to") or "")}</td>
          <td class="py-2 pr-4"><span class="badge bg-slate-100 text-slate-600">{_e(m.get("type") or "")}</span></td>
          <td class="py-2 pr-4 text-xs font-semibold">{_e(m.get("subject") or "")}</td>
          <td class="py-2 pr-5 text-xs">
            <details><summary class="cursor-pointer text-brand-600">corps</summary>
            <pre class="mt-1 p-2 bg-slate-50 rounded text-[11px] whitespace-pre-wrap max-h-40 overflow-auto">{_e(m.get("text_body") or "")}</pre></details>
          </td>
        </tr>"""
        sandbox_html = f"""
<div class="card p-4 sm:p-6 mb-6 border-amber-300 border-2">
  <div class="flex items-center gap-2 mb-2">
    <span class="badge bg-amber-100 text-amber-700">MODE SANDBOX</span>
    <p class="text-xs text-slate-500">Aucun email réel n'est envoyé — les
    envois sont capturés ci-dessous pour vos tests d'intégration.</p>
  </div>
  <div class="overflow-x-auto"><table class="w-full min-w-[640px]"><tbody>
    {sb_rows or '<tr><td class="py-6 text-center text-sm text-slate-400">Aucun envoi capturé pour le moment.</td></tr>'}
  </tbody></table></div>
</div>"""

    content = f"""
<div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-8">
  <div>
    <h1 class="text-2xl font-extrabold mb-1">Dashboard</h1>
    <p class="text-sm text-slate-500">Bienvenue {_e(user.app_name)} — voici votre espace API.</p>
  </div>
  <div class="flex items-center gap-2 shrink-0">
    {plan_badge}
    <a href="/demo" class="btn-outline text-xs py-2 px-4">Tester l'API</a>
  </div>
</div>

{sandbox_html}

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
  <form method="post" action="/dashboard/regenerate-keys" class="mt-4"
        data-confirm-title="Régénérer les clés"
        data-confirm="Régénérer vos clés API et HMAC ? Les anciennes seront invalidées immédiatement et les intégrations existantes cesseront de fonctionner."
        data-confirm-ok="Régénérer" data-confirm-danger>
    <button class="btn-danger">
      Régénérer mes clés (invalide les anciennes)
    </button>
  </form>
</div>

{plan_card_html}
{pay_history}
<div id="sec-smtp" class="card p-4 sm:p-6 mb-6 scroll-mt-24">
  <div class="flex items-center justify-between mb-1">
    <h2 class="font-bold">Configuration de votre expéditeur email</h2>
    {smtp_status_badge}
  </div>
  <p class="text-sm text-slate-500 mb-4">Configurez l'adresse email utilisée pour envoyer VOS emails —
  tout serveur SMTP est supporté (Gmail, Yahoo, iCloud, Outlook, IONOS, OVH, Zoho…).
  Gmail, Yahoo et iCloud exigent un <a class="text-brand-600 underline" href="https://myaccount.google.com/apppasswords" target="_blank">mot de passe d'application</a>.
  <span class="block mt-1 text-xs">Filet de sécurité : si votre serveur tombe en panne, les envois basculent automatiquement sur l'expéditeur plateforme (« VotreMarque via {_e(settings.from_name)} ») plutôt que d'échouer.</span></p>
  <form method="post" action="/dashboard/config" class="grid md:grid-cols-2 gap-4">
    <div><label class="text-xs font-semibold text-slate-600">Marque affichée</label>
    <input name="brand_name" class="input mt-1" value="{_e(tenant.brand_name)}"></div>
    <div><label class="text-xs font-semibold text-slate-600">Adresse expéditrice</label>
    <input name="from_email" type="email" class="input mt-1" value="{_e(tenant.from_email or '')}" placeholder="no-reply@votredomaine.com">
    <p class="text-[11px] text-amber-600 mt-1">Doit généralement correspondre au SMTP User (ou à un alias vérifié) — sinon le fournisseur réécrit l'expéditeur.</p></div>
    <div><label class="text-xs font-semibold text-slate-600">Nom expéditeur</label>
    <input name="from_name" class="input mt-1" value="{_e(tenant.from_name or '')}" placeholder="{_e(tenant.brand_name)}"></div>
    <div><label class="text-xs font-semibold text-slate-600">SMTP Host</label>
    <input name="smtp_host" class="input mt-1" value="{_e(_cfg(tenant,'smtp_host') or '')}" placeholder="{_e(settings.smtp_host)}">
    <p class="text-[11px] text-slate-400 mt-1">Vide = config globale (.env) · Gmail : smtp.gmail.com · Yahoo : smtp.mail.yahoo.com · iCloud : smtp.mail.me.com · Outlook : smtp.office365.com · IONOS : smtp.ionos.fr · OVH : ssl0.ovh.net · Zoho : smtp.zoho.com</p></div>
    <div><label class="text-xs font-semibold text-slate-600">SMTP Port</label>
    <input name="smtp_port" type="number" min="1" max="65535" class="input mt-1" value="{_e(str(_cfg(tenant,'smtp_port') or ''))}" placeholder="{settings.smtp_port}">
    <p class="text-[11px] text-slate-400 mt-1">587 = STARTTLS (la plupart des fournisseurs) · 465 = SSL — détection automatique selon le port.</p></div>
    <div><label class="text-xs font-semibold text-slate-600">SMTP User</label>
    <input name="smtp_user" class="input mt-1" value="{_e(tenant.smtp_user or '')}" placeholder="votre.email@gmail.com"></div>
    <div><label class="text-xs font-semibold text-slate-600">SMTP Password (app password)</label>
    <input name="smtp_password" type="password" class="input mt-1" placeholder="16 caractères">
    <p class="text-[11px] text-slate-400 mt-1">Laisser vide pour conserver le mot de passe actuel.</p></div>
    <div class="md:col-span-2"><label class="text-xs font-semibold text-slate-600">Webhook de notification (URL)</label>
    <input name="webhook_url" type="url" class="input mt-1" value="{_e(tenant.webhook_url or '')}" placeholder="https://votreapp.com/webhooks/mailhub">
    <p class="text-[11px] text-slate-400 mt-1">Votre endpoint reçoit les statuts d'envoi en temps réel : email.sent, email.failed, email.dead, otp.sent, otp.verified — POST JSON signé X-MailHub-Signature avec votre secret HMAC.</p></div>
    <div class="md:col-span-2 flex gap-3">
      <button class="btn-primary">Sauvegarder</button>
    </div>
  </form>
  <form method="post" action="/dashboard/test-email" class="mt-3 flex flex-col sm:flex-row gap-2 sm:items-center">
    <input name="test_to" type="email" class="input sm:max-w-xs" placeholder="envoyer un email de test à..." required>
    <button class="text-xs font-semibold text-brand-600 border border-blue-200 rounded-lg px-4 py-2.5 hover:bg-blue-50 whitespace-nowrap">Tester l'envoi</button>
  </form>

  <div class="mt-4 pt-4 border-t border-slate-100 grid md:grid-cols-2 gap-4">
    <div>
      <p class="text-xs font-semibold text-slate-600 mb-2">Santé de la connexion SMTP</p>
      {smtp_health_html}
      <form method="post" action="/dashboard/smtp-check" class="mt-2">
        <button class="btn-soft text-xs">Tester la connexion</button>
      </form>
    </div>
    <div>
      <div class="flex items-center justify-between mb-2">
        <p class="text-xs font-semibold text-slate-600">Délivrabilité du domaine</p>
        <form method="post" action="/dashboard/dns-check">
          <button class="btn-soft text-xs">Vérifier le DNS</button>
        </form>
      </div>
      {dns_check_html}
    </div>
  </div>
</div>

<div id="sec-branding" class="card p-4 sm:p-6 mb-6 scroll-mt-24">
  <h2 class="font-bold mb-1">Personnalisation des emails</h2>
  <p class="text-sm text-slate-500 mb-4">Couleur, logo et pied de page appliqués à tous vos emails (OTP et transactionnels). L'aperçu se met à jour en direct.</p>
  <form id="brandingForm" method="post" action="/dashboard/branding" enctype="multipart/form-data" class="grid md:grid-cols-2 gap-4 mb-4">
    <div><label class="text-xs font-semibold text-slate-600">Couleur de marque</label>
    <div class="flex gap-2 mt-1">
      <input name="brand_color" type="color" class="h-10 w-14 rounded-lg border border-slate-200 cursor-pointer" value="{_e(tenant.brand_color)}">
      <input id="brandColorHex" class="input flex-1 font-mono" value="{_e(tenant.brand_color)}" maxlength="7" spellcheck="false">
    </div></div>
    <div><label class="text-xs font-semibold text-slate-600">Logo — URL ou upload</label>
    <div class="flex gap-2 mt-1">
      <input name="email_logo_url" type="url" class="input flex-1" value="{_e(tenant.email_logo_url or '')}" placeholder="https://votresite.com/logo.png">
      <label class="btn-outline text-xs px-4 py-2.5 cursor-pointer whitespace-nowrap inline-flex items-center gap-1.5">
        <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M12 16V4m0 0l-4 4m4-4l4 4M4 20h16"/></svg>
        Choisir
        <input name="logo_file" type="file" accept="image/png,image/jpeg,image/gif,image/webp" class="hidden">
      </label>
    </div>
    <p class="text-[11px] text-slate-400 mt-1"><span id="logoFileName">PNG/JPG/GIF/WebP, 512 Ko max, fond transparent conseillé (~180×36 px). Upload prioritaire sur l'URL.</span></p></div>
    <div class="md:col-span-2"><label class="text-xs font-semibold text-slate-600">Pied de page personnalisé (optionnel)</label>
    <input name="email_footer" class="input mt-1" value="{_e(tenant.email_footer or '')}" placeholder="Ex : © 2026 MaSociété — contact@maboite.com">
    <p class="text-[11px] text-slate-400 mt-1">Vide = « Message automatique de VotreMarque — merci de ne pas répondre. »</p></div>
    <div class="md:col-span-2"><button class="btn-primary">Sauvegarder le branding</button></div>
  </form>
  <p class="text-xs font-semibold text-slate-600 mb-2">Aperçu de votre email OTP :</p>
  <iframe id="brandPreview" src="/dashboard/email-preview" class="w-full h-96 rounded-xl border border-slate-200 bg-white" title="Aperçu email"></iframe>
</div>
<script>
(function() {{
  var form = document.getElementById('brandingForm');
  var frame = document.getElementById('brandPreview');
  var picker = form.elements['brand_color'];
  var hex = document.getElementById('brandColorHex');
  var logoUrl = form.elements['email_logo_url'];
  var logoFile = form.elements['logo_file'];
  var footer = form.elements['email_footer'];
  var fileName = document.getElementById('logoFileName');
  var timer;
  function schedule() {{ clearTimeout(timer); timer = setTimeout(refresh, 350); }}
  async function refresh() {{
    var fd = new FormData();
    fd.append('brand_color', picker.value);
    fd.append('email_logo_url', logoUrl.value);
    fd.append('email_footer', footer.value);
    if (logoFile.files[0]) fd.append('logo_file', logoFile.files[0]);
    try {{
      var r = await fetch('/dashboard/email-preview', {{ method: 'POST', body: fd }});
      if (r.ok) frame.srcdoc = await r.text();
    }} catch (e) {{ /* réseau : l'aperçu garde la version sauvegardée */ }}
  }}
  picker.addEventListener('input', function() {{ hex.value = picker.value; schedule(); }});
  hex.addEventListener('input', function() {{
    if (/^#[0-9a-fA-F]{{6}}$/.test(hex.value)) {{ picker.value = hex.value; schedule(); }}
  }});
  logoUrl.addEventListener('input', schedule);
  footer.addEventListener('input', schedule);
  logoFile.addEventListener('change', function() {{
    if (logoFile.files[0]) fileName.textContent = 'Fichier : ' + logoFile.files[0].name;
    schedule();
  }});
}})();
</script>

<div id="sec-integration" class="card p-4 sm:p-6 mb-6 scroll-mt-24">
  <h2 class="font-bold mb-1">Intégrer l'API dans votre projet</h2>
  <p class="text-sm text-slate-500 mb-4">Chaque requête doit être signée : <code>HMAC-SHA256(body + timestamp, votre secret)</code>.</p>
  <div class="bg-[#0d0f1e] text-slate-100 rounded-lg p-5 text-xs overflow-auto font-mono">
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
  <p class="text-xs text-slate-500 mt-3">Node.js / PHP / cURL : voir <a href="/docs" class="text-brand-600 underline">la documentation Swagger</a>.</p>
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
    webhook_url: str = Form(""),
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
    tenant = await store.get(user.tenant_id)
    new_host = smtp_host.strip() or None
    updates = {
        "brand_name": brand_name.strip() or "SendBID",
        "from_email": from_email.strip() or None,
        "from_name": from_name.strip() or None,
        "smtp_host": new_host,
        "smtp_port": port,
        "smtp_user": smtp_user.strip() or None,
    }
    # Horodatage warm-up : première config ou changement de serveur ->
    # le compteur repart à zéro ; SMTP retiré -> warm-up désactivé.
    if tenant and new_host != tenant.smtp_host:
        updates["smtp_configured_at"] = time.time() if new_host else None
    wh = webhook_url.strip()
    updates["webhook_url"] = wh if wh.startswith(("http://", "https://")) else None
    if smtp_password.strip():
        updates["smtp_password"] = smtp_password.strip()
    await store.update(user.tenant_id, updates)
    return RedirectResponse("/dashboard", status_code=303)


_LOGO_MAX_BYTES = 512 * 1024  # 512 Ko — assez pour un logo, évite de gonfler Redis
_LOGO_SIGNATURES = {
    "image/png": b"\x89PNG",
    "image/jpeg": b"\xff\xd8\xff",
    "image/gif": b"GIF8",
    "image/webp": b"RIFF",
}


def _logo_valid(data: bytes, content_type: str) -> bool:
    """Signature magique + taille OK pour un logo embarqué."""
    sig = _LOGO_SIGNATURES.get(content_type)
    return bool(
        data and len(data) <= _LOGO_MAX_BYTES and sig and data.startswith(sig)
    )


async def _read_logo_upload(form) -> tuple[bytes, str] | None:
    """Extrait un logo uploadé -> (bytes, content-type) validé, sinon None."""
    f = form.get("logo_file")
    if not isinstance(f, UploadFile) or not f.filename:
        return None
    data = await f.read()
    ct = f.content_type or ""
    if not _logo_valid(data, ct):
        raise HTTPException(
            status_code=422,
            detail="Logo invalide — PNG/JPG/GIF/WebP, 512 Ko max.",
        )
    return data, ct


@router.get("/brand/{tenant_id}/logo")
async def brand_logo(tenant_id: str, request: Request):
    """Logo de marque public — référencé dans les emails (les clients mail le récupèrent)."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tenant_id):
        raise HTTPException(status_code=404, detail="Logo introuvable")
    raw = await request.app.state.redis.get(f"brandlogo:{tenant_id}")
    if raw is None:
        raise HTTPException(status_code=404, detail="Logo introuvable")
    meta = json.loads(raw)
    return Response(
        content=base64.b64decode(meta["b64"]),
        media_type=meta["ct"],
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.post("/dashboard/branding")
async def dashboard_branding(
    request: Request,
    _: None = Depends(require_same_origin),
):
    user = await _require_user(request)
    form = await request.form()
    color = str(form.get("brand_color") or "#5060a8").strip()
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        raise HTTPException(status_code=422, detail="Couleur invalide (format #rrggbb)")
    logo = str(form.get("email_logo_url") or "").strip()
    if logo and not logo.startswith(("https://", "http://")):
        raise HTTPException(status_code=422, detail="URL de logo invalide")
    uploaded = await _read_logo_upload(form)
    if uploaded:
        data, ct = uploaded
        await request.app.state.redis.set(
            f"brandlogo:{user.tenant_id}",
            json.dumps({"ct": ct, "b64": base64.b64encode(data).decode()}),
        )
        # URL absolue pour les clients mail ; relative si PUBLIC_BASE_URL absent
        base = request.app.state.settings.public_base_url.rstrip("/")
        logo = f"{base}/brand/{user.tenant_id}/logo"
    await TenantStore(request.app.state.redis).update(
        user.tenant_id,
        {
            "brand_color": color,
            "email_logo_url": logo or None,
            "email_footer": str(form.get("email_footer") or "").strip() or None,
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


@router.post("/dashboard/email-preview", response_class=HTMLResponse)
async def dashboard_email_preview_live(
    request: Request, _: None = Depends(require_same_origin)
):
    """Aperçu instantané : rend l'email avec les valeurs du formulaire (non sauvegardées)."""
    user = await _require_user(request)
    tenant = await TenantStore(request.app.state.redis).get(user.tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant introuvable")
    form = await request.form()
    color = str(form.get("brand_color") or "")
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        color = tenant.brand_color
    logo = str(form.get("email_logo_url") or "").strip()
    if logo and not logo.startswith(("https://", "http://")):
        logo = ""
    logo = logo or tenant.email_logo_url
    uploaded = await _read_logo_upload(form)
    if uploaded:
        data, ct = uploaded
        logo = f"data:{ct};base64,{base64.b64encode(data).decode()}"
    return HTMLResponse(
        otp_html(
            "482915",
            tenant.brand_name,
            "fr",
            ttl_minutes=max(1, round(tenant.otp_ttl_seconds / 60)),
            color=color,
            logo_url=logo,
            footer=str(form.get("email_footer") or "").strip() or tenant.email_footer,
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
        tenant = await TenantStore(redis).get(user.tenant_id)
        today = date.today().isoformat()
        if tenant and tenant.paid_until and tenant.paid_until > today:
            # SaaS standard : le downgrade prend effet en fin de période payée
            await TenantStore(redis).update(
                user.tenant_id,
                {"plan_scheduled": "free", "plan_requested": None},
            )
            return RedirectResponse("/dashboard", status_code=303)
        # Pas de période payée → application immédiate
        await TenantStore(redis).update(
            user.tenant_id,
            {"plan": plan, "plan_requested": None, "plan_scheduled": None,
             **plan_quota_updates(plan)},
        )
        return RedirectResponse("/dashboard", status_code=303)
    # Plan payant → page checkout : formulaire de facturation + moyen de paiement
    return RedirectResponse("/dashboard/billing?plan=" + plan, status_code=303)


@router.post("/dashboard/plan/cancel")
async def dashboard_plan_cancel(
    request: Request,
    _: None = Depends(require_same_origin),
):
    user = await _require_user(request)
    redis = request.app.state.redis
    await TenantStore(redis).update(
        user.tenant_id, {"plan_requested": None, "plan_scheduled": None}
    )
    return RedirectResponse("/dashboard", status_code=303)


@router.get("/dashboard/billing", response_class=HTMLResponse)
async def dashboard_billing_page(
    request: Request,
    plan: str = "",
    period: str = "monthly",
    err: str = "",
    settings: Settings = Depends(get_settings),
):
    user = await _require_user(request)
    if plan not in PLANS or plan == "free":
        return RedirectResponse("/dashboard#sec-plan", status_code=303)
    if period not in PERIODS:
        period = "monthly"
    redis = request.app.state.redis
    tenant = await TenantStore(redis).get(user.tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant introuvable")

    providers = available_providers(settings)
    method_cards = ""
    for prov, meta in providers.items():
        amt, cur = plan_price(settings, plan, prov, period)
        if not amt:
            continue
        method_cards += (
            '<label class="flex items-center gap-3 border border-slate-200 rounded-xl px-4 py-3 '
            'cursor-pointer transition hover:border-brand-300 '
            'has-[:checked]:border-brand-500 has-[:checked]:bg-brand-50/50">'
            f'<input type="radio" name="provider" value="{_e(prov)}" required class="accent-brand-600">'
            f'<span class="flex-1"><span class="font-semibold text-sm">{_e(meta["label"])}</span>'
            f'<span class="text-xs text-slate-400 ml-1.5">· {_e(meta["via"])}</span></span>'
            f'<b class="text-brand-600 text-sm whitespace-nowrap">{amt:g} {_e(cur)}</b></label>'
        )
    first_manual = " checked" if not method_cards else ""
    for prov, meta in MANUAL_METHODS.items():
        details = manual_method_details(settings, prov)
        coords = f' — <b class="font-medium">{_e(details)}</b>' if details else ""
        method_cards += (
            '<label class="flex items-center gap-3 border border-slate-200 rounded-xl px-4 py-3 '
            'cursor-pointer transition hover:border-brand-300 '
            'has-[:checked]:border-brand-500 has-[:checked]:bg-brand-50/50">'
            f'<input type="radio" name="provider" value="{_e(prov)}" required{first_manual} class="accent-brand-600">'
            f'<span class="flex-1"><span class="font-semibold text-sm">{_e(meta["label"])}</span>'
            f'<span class="text-xs text-slate-400 ml-1.5">· {_e(meta["via"])}{coords}</span></span>'
            '<span class="text-xs text-slate-400">Validé par l’admin</span></label>'
        )
        first_manual = ""
    price_rows = "".join(
        f'<li class="flex justify-between text-sm"><span class="text-slate-500">{_e(meta["label"])}</span>'
        f'<b>{amt:g} {_e(cur)}</b></li>'
        for prov, meta in providers.items()
        for amt, cur in [plan_price(settings, plan, prov, period)] if amt
    ) or '<li class="text-sm text-slate-500">Tarif convenu avec l’administrateur</li>'
    err_banner = ""
    if err:
        err_banner = (
            '<div class="alert alert-err mb-4">'
            + ("Informations de facturation incomplètes — vérifiez les champs requis."
               if err == "form" else
               "Le prestataire de paiement est momentanément indisponible — "
               "réessayez ou choisissez le paiement manuel.")
            + "</div>"
        )
    p_info = PLANS[plan]
    period_tabs = "".join(
        (
            f'<a href="/dashboard/billing?plan={_e(plan)}&period={_e(pid)}" data-noload '
            f'class="flex-1 text-center text-sm font-semibold rounded-lg px-4 py-2.5 border transition '
            + (
                "border-brand-500 bg-brand-50 text-brand-700"
                if pid == period
                else "border-slate-200 text-slate-500 hover:border-brand-300"
            )
            + f'">{_e(meta["label"])}</a>'
        )
        for pid, meta in PERIODS.items()
    )
    content = f"""
<div class="mb-6">
  <a href="/dashboard#sec-plan" class="text-xs text-slate-400 hover:text-brand-600 transition">← Retour au dashboard</a>
  <h1 class="text-2xl font-extrabold mt-1">Finaliser votre commande</h1>
  <p class="text-sm text-slate-500">Complétez vos informations de facturation puis choisissez un moyen de paiement.</p>
  <div class="card p-2 mt-4 flex gap-2 max-w-sm">{period_tabs}</div>
</div>
{err_banner}
<div class="grid lg:grid-cols-5 gap-6 items-start">
  <form method="post" action="/dashboard/billing/submit" class="lg:col-span-3 card p-4 sm:p-6 space-y-5">
    <input type="hidden" name="plan" value="{_e(plan)}">
    <input type="hidden" name="period" value="{_e(period)}">
    <div>
      <h2 class="font-bold text-sm mb-3">Informations de facturation</h2>
      <div class="grid sm:grid-cols-2 gap-4">
        <div><label class="text-xs font-semibold text-slate-600">Nom complet *</label>
        <input name="billing_name" class="input mt-1" required maxlength="120"
               value="{_e(tenant.billing_name or user.app_name or "")}"></div>
        <div><label class="text-xs font-semibold text-slate-600">Société</label>
        <input name="billing_company" class="input mt-1" maxlength="120"
               value="{_e(tenant.billing_company or "")}"></div>
        <div><label class="text-xs font-semibold text-slate-600">Email de facturation *</label>
        <input name="billing_email" type="email" class="input mt-1" required maxlength="200"
               value="{_e(tenant.billing_email or user.email)}"></div>
        <div><label class="text-xs font-semibold text-slate-600">Téléphone *</label>
        <input name="billing_phone" class="input mt-1" required maxlength="40" placeholder="+225 ..."
               value="{_e(tenant.billing_phone or "")}"></div>
      </div>
    </div>
    <div>
      <h2 class="font-bold text-sm mb-3">Moyen de paiement</h2>
      <div class="space-y-2">{method_cards}</div>
    </div>
    <button class="btn-primary w-full justify-center">Valider ma commande</button>
  </form>
  <div class="lg:col-span-2 card p-4 sm:p-6">
    <h2 class="font-bold text-sm mb-3">Récapitulatif</h2>
    <div class="flex items-center justify-between mb-3 pb-3 border-b border-slate-100">
      <span class="font-semibold">{_e(p_info['label'])}</span>
      <span class="badge bg-brand-50 text-brand-700">{PERIODS[period]['days']} jours</span>
    </div>
    <ul class="space-y-1.5 mb-4">
      <li class="flex justify-between text-sm"><span class="text-slate-500">Envois/utilisateur/10min</span><b>{p_info['max_sends_per_user_per_10min']}</b></li>
      <li class="flex justify-between text-sm"><span class="text-slate-500">Envois/IP/10min</span><b>{p_info['max_sends_per_ip_per_10min']}</b></li>
      <li class="flex justify-between text-sm"><span class="text-slate-500">Cooldown</span><b>{p_info['cooldown_seconds']}s</b></li>
      <li class="flex justify-between text-sm"><span class="text-slate-500">Essais de vérification</span><b>{p_info['max_verify_attempts']}</b></li>
    </ul>
    <div class="pt-3 border-t border-slate-100">
      <p class="text-xs font-semibold text-slate-600 mb-1.5">Tarif selon le moyen de paiement :</p>
      <ul class="space-y-1">{price_rows}</ul>
    </div>
    <p class="text-[11px] text-slate-400 mt-4">Paiement en ligne : activation immédiate.
    Paiement manuel : activation par l’administrateur après réception.</p>
  </div>
</div>"""
    return _dashboard_page(user, tenant, content, active="plan")


@router.post("/dashboard/billing/submit")
async def dashboard_billing_submit(
    request: Request,
    plan: str = Form(...),
    provider: str = Form(...),
    period: str = Form("monthly"),
    billing_name: str = Form(""),
    billing_email: str = Form(""),
    billing_phone: str = Form(""),
    billing_company: str = Form(""),
    settings: Settings = Depends(get_settings),
    _: None = Depends(require_same_origin),
):
    user = await _require_user(request)
    if plan not in PLANS or plan == "free":
        raise HTTPException(status_code=422, detail="Plan invalide")
    if period not in PERIODS:
        period = "monthly"
    online = provider in available_providers(settings)
    is_manual = provider in MANUAL_METHODS or provider == "manual"
    if not online and not is_manual:
        raise HTTPException(status_code=422, detail="Moyen de paiement invalide")
    if not (billing_name.strip() and billing_email.strip() and billing_phone.strip()):
        return RedirectResponse(f"/dashboard/billing?plan={plan}&err=form", status_code=303)
    redis = request.app.state.redis
    store = TenantStore(redis)
    await store.update(
        user.tenant_id,
        {
            "plan_requested": plan,
            "billing_name": billing_name.strip()[:120],
            "billing_email": billing_email.strip()[:200],
            "billing_phone": billing_phone.strip()[:40],
            "billing_company": billing_company.strip()[:120] or None,
        },
    )
    tenant = await store.get(user.tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant introuvable")
    if is_manual:
        tx_id = new_tx_id()
        await record_tx(
            redis,
            tx_id,
            {
                "tenant_id": user.tenant_id,
                "plan": plan,
                "provider": provider,
                "method": MANUAL_METHODS.get(provider, {}).get("label", "Paiement manuel"),
                "amount": 0,
                "currency": "",
                "status": "pending_manual",
                "period": period,
                "ts": int(time.time()),
                "date": date.today().isoformat(),
                "billing_name": tenant.billing_name,
                "billing_email": tenant.billing_email,
                "billing_phone": tenant.billing_phone,
            },
        )
        return RedirectResponse(f"/dashboard/billing/pay?tx={tx_id}", status_code=303)
    base = (settings.public_base_url or "").rstrip("/") or str(request.base_url).rstrip("/")
    res = await create_checkout(provider, plan, tenant, settings, base, redis, period=period)
    if "error" in res:
        return RedirectResponse(f"/dashboard/billing?plan={plan}&err=provider", status_code=303)
    return RedirectResponse(res["url"], status_code=303)


@router.post("/dashboard/billing/checkout")
async def dashboard_billing_checkout(
    request: Request,
    plan: str = Form(...),
    provider: str = Form(...),
    period: str = Form("monthly"),
    settings: Settings = Depends(get_settings),
    _: None = Depends(require_same_origin),
):
    user = await _require_user(request)
    if plan not in PLANS or plan == "free" or provider not in available_providers(settings):
        raise HTTPException(status_code=422, detail="Paiement invalide")
    redis = request.app.state.redis
    tenant = await TenantStore(redis).get(user.tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant introuvable")
    base = (settings.public_base_url or "").rstrip("/") or str(request.base_url).rstrip("/")
    res = await create_checkout(provider, plan, tenant, settings, base, redis, period=period)
    if "error" in res:
        return RedirectResponse("/dashboard?billing=err", status_code=303)
    return RedirectResponse(res["url"], status_code=303)


@router.get("/dashboard/billing/pay", response_class=HTMLResponse)
async def dashboard_billing_pay(
    request: Request,
    tx: str = "",
    done: str = "",
    settings: Settings = Depends(get_settings),
):
    user = await _require_user(request)
    redis = request.app.state.redis
    tenant = await TenantStore(redis).get(user.tenant_id)
    tx_data = await get_tx(redis, tx)
    if not tenant or not tx_data or tx_data.get("tenant_id") != user.tenant_id:
        return RedirectResponse("/dashboard", status_code=303)
    if tx_data.get("status") == "paid":
        return RedirectResponse("/dashboard?billing=ok", status_code=303)
    method = tx_data.get("provider") or "manual"
    meta = MANUAL_METHODS.get(method, {"label": "Paiement manuel", "via": ""})
    details = manual_method_details(settings, method)
    period_label = PERIODS.get(tx_data.get("period", "monthly"), PERIODS["monthly"])["label"]
    coords_block = ""
    if details:
        coords_block = f"""
      <div class="rounded-xl border border-brand-200 bg-brand-50/60 p-4 mb-5">
        <p class="text-xs font-semibold text-slate-500 mb-1.5">Coordonnées de paiement</p>
        <p class="font-mono text-sm font-semibold break-all">{_e(details)}</p>
      </div>"""
    declared = tx_data.get("status") == "pending_verification"
    confirm_zone = (
        f"""
      <div class="alert alert-warn mb-4">{UI_ICONS['clock']}
        <span>Paiement déclaré — vérification en cours par l'administrateur.</span></div>"""
        if declared
        else f"""
      <form method="post" action="/dashboard/billing/pay/confirm" class="space-y-4">
        <input type="hidden" name="tx" value="{_e(tx)}">
        <div class="grid sm:grid-cols-2 gap-4">
          <div><label class="text-xs font-semibold text-slate-600">Référence de transaction *</label>
          <input name="payer_ref" class="input mt-1" required maxlength="80"
                 placeholder="N° de transaction / référence du virement"></div>
          <div><label class="text-xs font-semibold text-slate-600">Nom ou n° expéditeur *</label>
          <input name="payer_info" class="input mt-1" required maxlength="120"
                 placeholder="Compte / numéro d'envoi"></div>
        </div>
        <button class="btn-primary w-full justify-center">J'ai effectué le paiement</button>
      </form>"""
    )
    plan_label = PLANS.get(tx_data.get("plan"), {}).get("label", (tx_data.get("plan") or "").upper())
    content = f"""
<div class="mb-6">
  <a href="/dashboard#sec-plan" class="text-xs text-slate-400 hover:text-brand-600 transition">← Retour au dashboard</a>
  <h1 class="text-2xl font-extrabold mt-1">Régler votre commande</h1>
  <p class="text-sm text-slate-500">Plan <b>{_e(plan_label)}</b> · {_e(period_label)} · {_e(meta['label'])}</p>
</div>
<div class="grid lg:grid-cols-5 gap-6 items-start">
  <div class="lg:col-span-3 card p-4 sm:p-6">
    <h2 class="font-bold text-sm mb-4">Effectuez le règlement</h2>
    <ol class="text-sm text-slate-600 space-y-2 mb-5 list-none">
      <li class="flex gap-2.5"><span class="step-n">1</span><span>Réglez via <b>{_e(meta['label'])}</b> avec les coordonnées ci-dessous.</span></li>
      <li class="flex gap-2.5"><span class="step-n">2</span><span>Déclarez votre paiement avec la référence de transaction.</span></li>
      <li class="flex gap-2.5"><span class="step-n">3</span><span>Votre plan est activé dès vérification — généralement très rapide.</span></li>
    </ol>
    {coords_block}
    {confirm_zone}
  </div>
  <div class="lg:col-span-2 card p-4 sm:p-6">
    <h2 class="font-bold text-sm mb-3">Commande <code class="text-xs text-slate-400">{_e(tx)}</code></h2>
    <ul class="space-y-1.5 text-sm">
      <li class="flex justify-between"><span class="text-slate-500">Plan</span><b>{_e(plan_label)}</b></li>
      <li class="flex justify-between"><span class="text-slate-500">Période</span><b>{_e(period_label)}</b></li>
      <li class="flex justify-between"><span class="text-slate-500">Moyen</span><b>{_e(meta['label'])}</b></li>
      <li class="flex justify-between"><span class="text-slate-500">Statut</span>
      <b class="{'text-amber-600' if not declared else 'text-brand-600'}">{'En attente de règlement' if not declared else 'En vérification'}</b></li>
      {f'<li class="flex justify-between"><span class="text-slate-500">Référence</span>'
       f'<code class="text-xs">{_e(tx_data.get("payer_ref") or "")}</code></li>' if declared else ''}
    </ul>
  </div>
</div>"""
    return _dashboard_page(user, tenant, content, active="plan")


@router.post("/dashboard/billing/pay/confirm")
async def dashboard_billing_pay_confirm(
    request: Request,
    tx: str = Form(...),
    payer_ref: str = Form(""),
    payer_info: str = Form(""),
    _: None = Depends(require_same_origin),
):
    user = await _require_user(request)
    redis = request.app.state.redis
    tx_data = await get_tx(redis, tx)
    if not tx_data or tx_data.get("tenant_id") != user.tenant_id:
        raise HTTPException(status_code=404, detail="Transaction introuvable")
    if not payer_ref.strip() or not payer_info.strip():
        return RedirectResponse(f"/dashboard/billing/pay?tx={tx}", status_code=303)
    await update_tx(
        redis,
        tx,
        status="pending_verification",
        payer_ref=payer_ref.strip()[:80],
        payer_info=payer_info.strip()[:120],
    )
    return RedirectResponse("/dashboard?plan_requested=" + tx_data["plan"], status_code=303)


@router.get("/dashboard/billing/return", response_class=HTMLResponse)
async def dashboard_billing_return(
    request: Request,
    p: str = "",
    provider: str = "",
    token: str = "",
    settings: Settings = Depends(get_settings),
):
    await _require_user(request)
    redis = request.app.state.redis
    # PayPal : la capture se fait ici (pas de webhook nécessaire pour la validation)
    if provider == "paypal" and token:
        order = await capture_paypal_order(settings, token)
        if order and order.get("status") == "COMPLETED":
            tx_id = ""
            for unit in order.get("purchase_units", []):
                tx_id = unit.get("custom_id") or unit.get("reference_id") or ""
            tx = await get_tx(redis, tx_id) if tx_id else None
            if tx:
                await apply_paid_plan(
                    redis,
                    tx_id=tx_id,
                    tenant_id=tx["tenant_id"],
                    plan=tx["plan"],
                    provider="paypal",
                    amount=tx.get("amount"),
                    currency=tx.get("currency", ""),
                    period=tx.get("period", "monthly"),
                )
                return RedirectResponse("/dashboard?billing=ok", status_code=303)
        return RedirectResponse("/dashboard?billing=err", status_code=303)
    if p == "cancel":
        return RedirectResponse("/dashboard?billing=cancel", status_code=303)
    # Stripe / mobile : le webhook applique le plan en arrière-plan
    return RedirectResponse("/dashboard?billing=pending", status_code=303)


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


@router.post("/dashboard/smtp-check")
async def dashboard_smtp_check(
    request: Request,
    settings: Settings = Depends(get_settings),
    _: None = Depends(require_same_origin),
):
    """Healthcheck SMTP à la demande (EHLO/TLS/login, sans envoi)."""
    user = await _require_user(request)
    redis = request.app.state.redis
    tenant = await TenantStore(redis).get(user.tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant introuvable")

    from src.services.smtp_health import check_smtp_health
    host = tenant.smtp_host or settings.smtp_host
    port = tenant.smtp_port or settings.smtp_port
    user_ = tenant.smtp_user or settings.smtp_user
    pwd = tenant.smtp_password or settings.smtp_password
    use_ssl = tenant.smtp_use_ssl
    if use_ssl is None:
        use_ssl = settings.smtp_use_ssl
    if use_ssl is None:
        use_ssl = int(port) == 465
    res = await check_smtp_health(host, int(port), user_, pwd, use_ssl)
    await TenantStore(redis).update(tenant.tenant_id, {
        "smtp_status": "ok" if res["ok"] else "failed",
        "smtp_status_error": "" if res["ok"]
            else f'{res["stage"]}: {res["error"]}',
        "smtp_checked_at": res["checked_at"],
    })
    return RedirectResponse("/dashboard#sec-smtp", status_code=303)


@router.post("/dashboard/dns-check")
async def dashboard_dns_check(
    request: Request,
    settings: Settings = Depends(get_settings),
    _: None = Depends(require_same_origin),
):
    """Vérification MX/SPF/DKIM/DMARC du domaine de l'expéditeur."""
    user = await _require_user(request)
    redis = request.app.state.redis
    tenant = await TenantStore(redis).get(user.tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant introuvable")

    from src.services.dns_check import check_email_domain
    from_email = tenant.from_email or settings.from_email
    domain = from_email.split("@")[-1] if "@" in from_email else ""
    if domain:
        result = await check_email_domain(domain)
        await redis.set(
            f"{tenant.key_prefix()}dnscheck", json.dumps(result), ex=7 * 24 * 3600
        )
    return RedirectResponse("/dashboard#sec-smtp", status_code=303)


# =====================================================================
# Démo interactive (accessible sans compte en DEBUG)
# =====================================================================

def _demo_page(default_tenant: str = "default") -> str:
    return """
<div class="max-w-4xl mx-auto py-10 px-6">
  <h1 class="text-2xl font-extrabold mb-1">Console de test</h1>
  <p class="text-sm text-slate-500 mb-8">Testez les endpoints en direct. Les emails sont loggés côté serveur (provider console).</p>

  <div class="card p-4 sm:p-6 mb-6">
    <h2 class="font-bold mb-4 flex items-center gap-2.5"><span class="step-n">1</span>Envoyer un OTP</h2>
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
    <button onclick="sendOtp(this)" class="btn-primary mt-4">Envoyer l'OTP</button>
    <pre id="sendResult" class="mt-4 text-xs bg-[#0d0f1e] text-emerald-300 rounded-lg p-4 hidden overflow-auto"></pre>
    <div id="otpCode" class="mt-3 text-center text-3xl font-extrabold tracking-[.4em] text-brand-600 hidden"></div>
  </div>

  <div class="card p-4 sm:p-6 mb-6">
    <h2 class="font-bold mb-4 flex items-center gap-2.5"><span class="step-n">2</span>Vérifier le code</h2>
    <div class="grid md:grid-cols-2 gap-4">
      <div><label class="text-xs font-semibold text-slate-600">Request ID</label>
      <input id="requestId" class="input mt-1" placeholder="req_..."></div>
      <div><label class="text-xs font-semibold text-slate-600">Code</label>
      <input id="code" class="input mt-1" maxlength="6" placeholder="123456"></div>
    </div>
    <button onclick="verifyOtp(this)" class="btn-primary mt-4">Vérifier</button>
    <pre id="verifyResult" class="mt-4 text-xs bg-[#0d0f1e] text-emerald-300 rounded-lg p-4 hidden overflow-auto"></pre>
  </div>

  <div class="card p-6">
    <h2 class="font-bold mb-4 flex items-center gap-2.5"><span class="step-n">3</span>Email transactionnel</h2>
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
    <button onclick="sendEmail(this)" class="btn-primary mt-4">Envoyer l'email</button>
    <pre id="emailResult" class="mt-4 text-xs bg-[#0d0f1e] text-emerald-300 rounded-lg p-4 hidden overflow-auto"></pre>
  </div>
</div>

<script>
function show(id, data, ok) {
  const el = document.getElementById(id);
  el.classList.remove('hidden');
  el.className = el.className.replace(/text-(emerald|rose)-300/, ok ? 'text-emerald-300' : 'text-rose-300');
  el.textContent = JSON.stringify(data, null, 2);
}
async function call(btn, fn) {
  btn.classList.add('is-loading'); btn.disabled = true;
  try { await fn(); }
  catch (e) { toast('Erreur réseau', 'err'); }
  finally { btn.classList.remove('is-loading'); btn.disabled = false; }
}
async function sendOtp(btn) {
  await call(btn, async () => {
    const res = await fetch("/demo/send", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({email: email.value, country_code: country.value, language: language.value,
        purpose: "login_2fa", tenant_id: otpTenant.value || "default"})});
    const data = await res.json(); show("sendResult", data, res.ok);
    if (!res.ok) { toast(data.detail || 'Erreur', 'err'); return; }
    if (data.request_id) requestId.value = data.request_id;
    if (data.debug_code) { const c = document.getElementById("otpCode");
      c.classList.remove("hidden"); c.textContent = "Code : " + data.debug_code; code.value = data.debug_code; }
    toast('OTP envoyé', 'ok');
  });
}
async function verifyOtp(btn) {
  await call(btn, async () => {
    const res = await fetch("/demo/verify", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({request_id: requestId.value, code: code.value,
        tenant_id: otpTenant.value || "default"})});
    const data = await res.json();
    show("verifyResult", data, res.ok && data.verified);
    if (res.ok && data.verified) toast('Code vérifié', 'ok');
    else toast(data.detail || 'Code incorrect', 'err');
  });
}
async function sendEmail(btn) {
  await call(btn, async () => {
    const t = emailType.value;
    const payload = {to_email: emailTo.value, type: t, language: emailLang.value,
      tenant_id: emailTenant.value || "default", data: {}};
    if (t === "welcome") payload.data = {name: "John Doe"};
    else if (t === "custom") { payload.subject = emailTitle.value; payload.text_body = emailMessage.value; }
    else payload.data = t === "notification" ? {title: emailTitle.value, message: emailMessage.value}
                      : {amount: "149,90", currency: "EUR", status: "confirmée"};
    const res = await fetch("/demo/email", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify(payload)});
    const data = await res.json(); show("emailResult", data, res.ok);
    if (res.ok) toast('Email envoyé', 'ok'); else toast(data.detail || 'Erreur', 'err');
  });
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
