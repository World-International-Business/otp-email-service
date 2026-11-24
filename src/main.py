"""Point d'entrée FastAPI."""

import asyncio
import html
import logging
import sys
from contextlib import asynccontextmanager, suppress
from datetime import date

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pythonjsonlogger.jsonlogger import JsonFormatter
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.api.admin_routes import admin_router
from src.api.billing_routes import router as billing_router
from src.api.dependencies import get_email_provider, get_redis_client, get_settings
from src.api.event_routes import events_router
from src.api.metrics_routes import metrics_router
from src.api.middleware import JSONLogMiddleware, SecurityHeadersMiddleware
from src.api.routes import email_router, router
from src.api.track_routes import track_router
from src.api.unsub_routes import unsub_router
from src.core.tenants import PLANS, TenantStore, plan_quota_updates, seed_default_tenant
from src.services.billing_reminders import notify_plan_expired, run_billing_reminders
from src.services.email_queue import process_email_queue_once
from src.services.email_service import EmailService
from src.services.otp_service import OTPService
from src.services.webhooks import process_webhook_queue_once
from src.web.admin import router as admin_web_router
from src.web.docs_page import router as docs_router
from src.web.i18n import get_lang, translate_html
from src.web.routes import FAVICON_URI
from src.web.routes import router as web_router
from src.web.tailwind_css import TAILWIND_CSS


def setup_logging(log_level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    formatter = JsonFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        rename_fields={"levelname": "level", "asctime": "timestamp"},
    )
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(getattr(logging, log_level.upper(), logging.INFO))


def _preflight_warnings(settings) -> list[str]:
    """Signale au démarrage les écarts de config entre env local et prod.

    Chaque warning ci-dessous correspond à un oubli d'env déjà constaté
    (ex. PLAN_PRICE_* non remplis → landing « Sur mesure », ou des options
    de dev restées actives en production). Un seul log WARNING consolidé —
    non bloquant.
    """
    w = []
    if settings.debug:
        w.append("DEBUG=true : debug_code expose les OTP dans les réponses API")
    if settings.demo_enabled:
        w.append("DEMO_ENABLED=true : /demo envoie de vrais emails sans authentification")
    if settings.use_fakeredis:
        w.append("USE_FAKEREDIS=true : tenants, comptes, quotas et sessions perdus au redémarrage")
    if not settings.public_base_url:
        w.append(
            "PUBLIC_BASE_URL vide : magic links absents, tracking inactif, "
            "logos locaux non résolus, cookies non Secure"
        )
    if settings.email_provider == "console":
        w.append("EMAIL_PROVIDER=console : aucun email réel n'est envoyé")
    elif settings.email_provider in ("smtp", "gmail") and not (
        settings.smtp_user and settings.smtp_password
    ):
        w.append("EMAIL_PROVIDER=smtp sans SMTP_USER/SMTP_PASSWORD : les envois échoueront")
    if not settings.list_unsubscribe_url and not settings.public_base_url:
        w.append(
            "Aucun lien opt-out possible (LIST_UNSUBSCRIBE_URL et "
            "PUBLIC_BASE_URL vides) : délivrabilité dégradée (Gmail/Yahoo)"
        )
    _FREEMAIL = {
        "gmail.com", "googlemail.com", "yahoo.com", "yahoo.fr",
        "outlook.com", "hotmail.com", "hotmail.fr", "live.com", "msn.com",
        "aol.com", "icloud.com", "me.com", "proton.me", "protonmail.com",
        "gmx.com", "gmx.fr", "orange.fr", "wanadoo.fr", "free.fr", "sfr.fr",
    }
    from_domain = str(settings.from_email).rsplit("@", 1)[-1].lower()
    if from_domain in _FREEMAIL:
        w.append(
            f"FROM_EMAIL sur domaine freemail ({from_domain}) : réputation "
            "faible, mise en spam quasi certaine — utilisez une adresse sur "
            "votre domaine avec SPF + DKIM + DMARC configurés"
        )
    price_fields = [
        f for f in type(settings).model_fields if f.startswith("plan_price_")
    ]
    if not any(getattr(settings, f) for f in price_fields):
        w.append(
            "PLAN_PRICE_* tous à 0 : la landing affiche « Sur mesure » et "
            "aucun paiement en ligne n'est possible"
        )
    if not (
        settings.billing_bank_details
        or settings.billing_momo_number
        or settings.billing_manual_note
    ):
        w.append(
            "BILLING_BANK_DETAILS / BILLING_MOMO_* / BILLING_MANUAL_NOTE vides : "
            "les panneaux de paiement manuel n'affichent aucune coordonnée"
        )
    if settings.web_signup_enabled and not settings.signup_invite_code:
        w.append(
            "WEB_SIGNUP_ENABLED=true sans SIGNUP_INVITE_CODE : "
            "inscription publique ouverte à tous"
        )
    return w


async def _expire_billing_once(redis, email_provider=None, public_base_url: str = "") -> int:
    """Rétrograde les tenants dont la période payée est terminée.

    `paid_until` est une date ISO (YYYY-MM-DD) — comparaison lexicale sûre.
    Les partenaires `unlimited` et le plan gratuit ne sont jamais touchés.
    Retourne le nombre de tenants rétrogradés.
    """
    store = TenantStore(redis)
    today = date.today().isoformat()
    downgraded = 0
    for t in await store.list():
        if (
            t.paid_until
            and t.paid_until < today
            and not t.unlimited
            and t.plan != "free"
        ):
            old_plan = t.plan
            # Downgrade programmé (ex. retour au Starter en fin de période)
            # ou retour au plan gratuit par défaut.
            target = t.plan_scheduled if t.plan_scheduled in PLANS else "free"
            await store.update(
                t.tenant_id,
                {
                    "plan": target,
                    "plan_scheduled": None,
                    "billing_status": "expired",
                    **plan_quota_updates(target),
                },
            )
            downgraded += 1
            logging.getLogger(__name__).info(
                "billing_expired_downgrade",
                extra={"tenant_id": t.tenant_id, "paid_until": t.paid_until},
            )
            if email_provider:
                try:
                    await notify_plan_expired(
                        redis, email_provider, t.tenant_id, old_plan, public_base_url
                    )
                except Exception:
                    logging.getLogger(__name__).warning(
                        "billing_expired_notify_failed",
                        extra={"tenant_id": t.tenant_id},
                    )
    return downgraded


async def _expire_billing_loop(redis, settings, email_provider) -> None:
    """Rétrogradation des expirés + rappels J-7/J-3/J-1 aux clients."""
    while True:
        await asyncio.sleep(settings.billing_check_interval_seconds)
        try:
            await _expire_billing_once(
                redis, email_provider, settings.public_base_url or ""
            )
            await run_billing_reminders(
                redis, email_provider, settings.public_base_url or ""
            )
        except Exception:
            logging.getLogger(__name__).exception("billing_expiry_loop_error")


async def _smtp_health_once(redis, settings) -> int:
    """Healthcheck SMTP de tous les tenants (config tenant > globale).

    Stocke le résultat sur le tenant (smtp_status/checked_at) ; en cas de
    bascule ok -> failed : événement + webhook `tenant.smtp_failed` pour que
    le client répare ses identifiants AVANT que ses mails n'échouent.
    """
    from src.services.events import log_event
    from src.services.smtp_health import check_smtp_health

    checked = 0
    for t in await TenantStore(redis).list():
        host = t.smtp_host or settings.smtp_host
        port = t.smtp_port or settings.smtp_port
        user = t.smtp_user or settings.smtp_user
        password = t.smtp_password or settings.smtp_password
        use_ssl = t.smtp_use_ssl
        if use_ssl is None:
            use_ssl = settings.smtp_use_ssl
        if use_ssl is None:
            use_ssl = int(port) == 465
        if not host or not user:
            continue
        res = await check_smtp_health(host, int(port), user, password, use_ssl)
        status = "ok" if res["ok"] else "failed"
        changed = t.smtp_status and t.smtp_status != status
        await TenantStore(redis).update(t.tenant_id, {
            "smtp_status": status,
            "smtp_status_error": "" if res["ok"]
                else f'{res["stage"]}: {res["error"]}',
            "smtp_checked_at": res["checked_at"],
        })
        if not res["ok"] and (changed or not t.smtp_status):
            await log_event(
                redis, t, "smtp_health_failed",
                stage=res["stage"], error=res["error"][:100],
            )
            from src.services.webhooks import enqueue_webhook
            await enqueue_webhook(
                redis, t, "tenant.smtp_failed",
                {"tenant_id": t.tenant_id, "stage": res["stage"],
                 "error": res["error"][:150]},
            )
        checked += 1
    return checked


async def _smtp_health_loop(redis, settings) -> None:
    """Boucle périodique du healthcheck SMTP (0 = désactivé)."""
    interval = settings.smtp_health_check_interval_seconds
    if interval <= 0:
        return
    while True:
        await asyncio.sleep(interval)
        try:
            await _smtp_health_once(redis, settings)
        except Exception:
            logging.getLogger(__name__).exception("smtp_health_loop_error")


async def _queue_worker_loop(app: FastAPI, poll_seconds: float) -> None:
    """Worker de fond : livraison des emails en file + webhooks sortants."""
    log = logging.getLogger(__name__)
    while True:
        try:
            await process_email_queue_once(
                app.state.redis, app.state.email_service,
                max_attempts=app.state.settings.email_max_attempts,
            )
            await process_webhook_queue_once(app.state.redis)
        except Exception:
            log.exception("queue_worker_error")
        await asyncio.sleep(poll_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_level)

    redis = get_redis_client(settings)
    await redis.ping()

    email_provider = get_email_provider(settings)
    app.state.settings = settings
    app.state.redis = redis
    app.state.otp_service = OTPService(redis, settings, email_provider)
    app.state.email_service = EmailService(redis, settings, email_provider)

    # Tenant par défaut créé depuis les variables d'env (rétrocompatibilité)
    await seed_default_tenant(redis, settings)

    billing_task = asyncio.create_task(
        _expire_billing_loop(redis, settings, email_provider)
    )
    queue_task = asyncio.create_task(
        _queue_worker_loop(app, settings.email_queue_poll_seconds)
    )
    health_task = asyncio.create_task(_smtp_health_loop(redis, settings))

    logging.getLogger(__name__).info("service_started", extra={"version": settings.app_version})
    if warnings := _preflight_warnings(settings):
        logging.getLogger(__name__).warning("preflight_config", extra={"issues": warnings})
    yield
    for task in (billing_task, queue_task, health_task):
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    await redis.aclose()
    logging.getLogger(__name__).info("service_stopped")


app = FastAPI(
    title="OTP Email Service",
    description="Microservice OTP par email pour SendBID / PayBID.",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

@app.exception_handler(StarletteHTTPException)
async def http_exception_page(request: Request, exc: StarletteHTTPException):
    """API (/v1/*) -> JSON ; interface web -> page d'erreur HTML lisible."""
    # Les redirects web sont levés via HTTPException(303) — les laisser passer.
    if exc.status_code in (301, 302, 303, 307, 308):
        return RedirectResponse(
            exc.headers["Location"], status_code=exc.status_code
        )
    if (
        request.url.path.startswith("/v1")
        or request.url.path.startswith("/admin/api")
        or request.url.path == "/health"
    ):
        return JSONResponse(
            {"detail": exc.detail},
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
        )
    messages = {
        404: "Page introuvable",
        403: "Accès refusé",
        422: "Données invalides",
        429: "Trop de requêtes — réessayez dans un instant",
    }
    title = messages.get(exc.status_code, "Erreur")
    detail = html.escape(str(exc.detail or title))
    return _error_page(exc.status_code, title, detail, exc.headers, request)


def _error_page(
    code: int, title: str, detail: str, headers=None, request: Request | None = None
) -> HTMLResponse:
    fonts = (
        "https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800"
        "&family=Space+Grotesk:wght@600;700&display=swap"
    )
    page = f"""<!DOCTYPE html><html lang="fr"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" type="image/png" href="{FAVICON_URI}">
<title>{title} — MailHub</title>
<style>{TAILWIND_CSS}</style>
<link href="{fonts}" rel="stylesheet"></head>
<body class="bg-slate-50 min-h-screen flex items-center justify-center px-4">
<div class="card p-10 max-w-md w-full text-center shadow-lg">
  <div class="flex justify-center mb-5">
    <img src="/static/mailhub-logo.png?v=3" alt="MailHub" class="h-16 w-auto">
  </div>
  <p class="text-5xl font-black mb-2" style="background:linear-gradient(92deg,#5060a8,#f08810);
     -webkit-background-clip:text;background-clip:text;color:transparent">{code}</p>
  <h1 class="text-xl font-bold mb-2">{title}</h1>
  <p class="text-sm text-slate-500 mb-7">{detail}</p>
  <a href="/" class="btn-primary">Retour à l'accueil</a>
</div></body></html>"""
    if request is not None:
        page = translate_html(page, get_lang(request), request.url.path)
    return HTMLResponse(page, status_code=code, headers=headers)


@app.exception_handler(RequestValidationError)
async def validation_exception_page(request: Request, exc: RequestValidationError):
    """Formulaires web invalides -> page HTML lisible ; API -> JSON 422."""
    if request.url.path.startswith("/v1"):
        # Format lisible pour les clients API : champ + message, sans le
        # bruit pydantic (loc interne, input, url de doc…).
        errors = [
            {
                "field": ".".join(str(p) for p in e.get("loc", []) if p != "body"),
                "message": e.get("msg", "invalid"),
            }
            for e in exc.errors()
        ]
        return JSONResponse({"detail": errors or "Invalid request"}, status_code=422)
    missing = [
        str(e.get("loc", ["?"])[-1])
        for e in exc.errors()
        if e.get("type") == "missing"
    ]
    detail = (
        f"Champs manquants : {', '.join(missing)}"
        if missing
        else "Données du formulaire invalides."
    )
    return _error_page(422, "Données invalides", detail, request=request)


@app.get("/health")
async def health():
    try:
        await app.state.redis.ping()
        redis_status = "ok"
    except Exception:
        redis_status = "error"
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redis unavailable",
        )
    return {
        "status": "ok",
        "redis": redis_status,
        "version": app.state.settings.app_version,
    }


app.add_middleware(JSONLogMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.include_router(web_router)
app.include_router(admin_web_router)
app.include_router(docs_router)
app.include_router(router)
app.include_router(email_router)
app.include_router(admin_router)
app.include_router(billing_router)
app.include_router(events_router)
app.include_router(metrics_router)
app.include_router(track_router)
app.include_router(unsub_router)
