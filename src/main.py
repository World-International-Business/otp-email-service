"""Point d'entrée FastAPI."""

import logging
import sys

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, status
from pythonjsonlogger.jsonlogger import JsonFormatter

from src.api.dependencies import get_email_provider, get_redis_client, get_settings
from src.api.middleware import JSONLogMiddleware
from src.api.admin_routes import admin_router
from src.api.routes import email_router, router
from src.core.tenants import seed_default_tenant
from src.services.email_service import EmailService
from src.services.otp_service import OTPService
from src.web.docs_page import router as docs_router
from src.web.routes import router as web_router


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

    logging.getLogger(__name__).info("service_started", extra={"version": settings.app_version})
    yield
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
app.include_router(web_router)
app.include_router(docs_router)
app.include_router(router)
app.include_router(email_router)
app.include_router(admin_router)
