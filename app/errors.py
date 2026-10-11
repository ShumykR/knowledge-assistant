"""Єдиний формат помилок: RFC 7807 (application/problem+json)."""

import logging
from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger(__name__)
PROBLEM = "application/problem+json"


class DomainError(Exception):
    """Базова помилка бізнес-логіки: сервіси кидають її, хендлер робить із неї problem+json."""

    status = 400
    title = "Bad request"

    def __init__(self, detail: str, headers: dict[str, str] | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.headers = headers


class NotFoundError(DomainError):
    status = 404
    title = "Not found"


def problem(
    status: int, title: str, detail: str, headers: Mapping[str, str] | None = None, **extra: Any
) -> JSONResponse:
    body = {"type": "about:blank", "title": title, "status": status, "detail": detail, **extra}
    return JSONResponse(body, status_code=status, media_type=PROBLEM, headers=headers)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def _domain(_: Request, exc: DomainError) -> JSONResponse:
        return problem(exc.status, exc.title, exc.detail, exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return problem(exc.status_code, "HTTP error", str(exc.detail), exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        # у відповідь лише розташування й тип помилки, без введених значень
        errors = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
        return problem(422, "Validation error", "Request validation failed", errors=errors)

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error", exc_info=exc)
        # деталі винятку клієнту не віддаємо
        return problem(500, "Internal server error", "Unexpected error")


class TooManyRequests(DomainError):
    status = 429
    title = "Too many requests"

    def __init__(self, retry_after: int) -> None:
        super().__init__("Rate limit exceeded", {"Retry-After": str(retry_after)})
