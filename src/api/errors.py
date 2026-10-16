"""Réponses d'erreur standardisées au format RFC 7807 (application/problem+json).

Toutes les erreurs de l'API renvoient un corps JSON uniforme :

    {
      "type": "about:blank",
      "title": "Too Many Requests",
      "status": 429,
      "detail": "Too many OTP requests",
      "instance": "/v1/otp/send",
      "code": "too_many_requests"
    }

Les redirections internes (HTTPException avec status < 400, ex. /login) sont
retransmises telles quelles pour ne pas casser le flux web.
"""

import logging
import re

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

PROBLEM_MEDIA_TYPE = "application/problem+json"

_STATUS_TITLES = {
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    413: "Payload Too Large",
    415: "Unsupported Media Type",
    422: "Unprocessable Entity",
    429: "Too Many Requests",
    500: "Internal Server Error",
    502: "Bad Gateway",
    503: "Service Unavailable",
}


def _slug(title: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", title.lower())).strip("_")


def problem(
    status: int,
    detail: str,
    instance: str,
    *,
    code: str | None = None,
    headers: dict | None = None,
    extra: dict | None = None,
) -> JSONResponse:
    """Construit une réponse RFC 7807."""
    title = _STATUS_TITLES.get(status, "Error")
    body = {
        "type": "about:blank",
        "title": title,
        "status": status,
        "detail": detail,
        "instance": instance,
        "code": code or _slug(title),
    }
    if extra:
        body.update(extra)
    return JSONResponse(
        status_code=status,
        content=body,
        media_type=PROBLEM_MEDIA_TYPE,
        headers=headers,
    )


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> Response:
    # Les statuts < 400 (ex: 303 redirect du portail web) ne sont pas des erreurs.
    if exc.status_code < 400:
        return Response(status_code=exc.status_code, headers=dict(exc.headers or {}))

    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return problem(
        exc.status_code,
        detail,
        request.url.path,
        headers=getattr(exc, "headers", None),
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    errors = [
        {
            "field": ".".join(str(loc) for loc in e.get("loc", [])),
            "message": e.get("msg", ""),
            "type": e.get("type", ""),
        }
        for e in exc.errors()
    ]
    return problem(
        422,
        "Request validation failed",
        request.url.path,
        extra={"errors": errors},
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception(
        "unhandled_exception",
        extra={"path": request.url.path, "method": request.method},
    )
    return problem(500, "Internal server error", request.url.path)


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
