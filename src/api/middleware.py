"""Middleware de logging JSON structuré."""

import logging
import time
import uuid

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from src.api.dependencies import get_client_ip


class JSONLogMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, logger_name: str = "otp_email_service"):
        super().__init__(app)
        self.logger = logging.getLogger(logger_name)

    async def dispatch(self, request: Request, call_next):
        request_id = str(uuid.uuid4())[:8]
        request.state.request_id = request_id

        start = time.perf_counter()
        response = await call_next(request)
        duration = round((time.perf_counter() - start) * 1000, 2)

        self.logger.info(
            "http_request",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": duration,
                "client_ip": get_client_ip(request),
            },
        )
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Headers de sécurité HTTP sur toutes les réponses.

    HSTS n'est émis que lorsque PUBLIC_BASE_URL est en https:// — inutile
    (et ignoré) en HTTP local.
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        headers = response.headers
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        settings = getattr(request.app.state, "settings", None)
        if settings and str(settings.public_base_url).startswith("https://"):
            headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response
