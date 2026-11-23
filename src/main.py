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
from src.api.dependencies import get_email_provider, get_redis_client, get_settings
from src.api.middleware import JSONLogMiddleware, SecurityHeadersMiddleware
from src.api.routes import email_router, router
from src.core.tenants import TenantStore, plan_quota_updates, seed_default_tenant
from src.services.email_service import EmailService
from src.services.otp_service import OTPService
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


async def _expire_billing_once(redis) -> int:
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
            await store.update(
                t.tenant_id,
                {
                    "plan": "free",
                    "billing_status": "expired",
                    **plan_quota_updates("free"),
                },
            )
            downgraded += 1
            logging.getLogger(__name__).info(
                "billing_expired_downgrade",
                extra={"tenant_id": t.tenant_id, "paid_until": t.paid_until},
            )
    return downgraded


async def _expire_billing_loop(redis, interval: int) -> None:
    """Boucle périodique de résiliation automatique des abonnements expirés."""
    while True:
        await asyncio.sleep(interval)
        try:
            await _expire_billing_once(redis)
        except Exception:
            logging.getLogger(__name__).exception("billing_expiry_loop_error")


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
        _expire_billing_loop(redis, settings.billing_check_interval_seconds)
    )

    logging.getLogger(__name__).info("service_started", extra={"version": settings.app_version})
    yield
    billing_task.cancel()
    with suppress(asyncio.CancelledError):
        await billing_task
    await redis.close()
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
    <img src="/static/mailhub-logo.png" alt="MailHub" class="h-16 w-auto">
  </div>
  <p class="text-5xl font-black mb-2" style="background:linear-gradient(92deg,#5b63c8,#f5a623);
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
        return JSONResponse({"detail": exc.errors()}, status_code=422)
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
