import logging
import secrets
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import cast

import redis as redis_lib
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.api import router
from app.config import Settings, get_settings
from app.db import make_engine, make_memory_engine, make_session_factory
from app.errors import install_error_handlers
from app.ingest import Ingestor
from app.jobs import ArqQueue, InlineQueue, Queue
from app.kv import KV, MemoryKV
from app.logging_utils import install_pii_filter
from app.ratelimit import RateLimiter
from app.repositories import DocumentRepository, RefreshRepository, UserRepository
from app.security import TokenService
from app.services import AuthService, DocumentService
from app.wiring import build_embedder, build_storage

log = logging.getLogger(__name__)
MIN_SECRET_LENGTH = 32


def _jwt_secret(settings: Settings) -> str:
    secret = settings.jwt_secret.get_secret_value()
    if settings.env == "prod":
        if len(secret) < MIN_SECRET_LENGTH:
            raise RuntimeError(
                f"KA_JWT_SECRET (>= {MIN_SECRET_LENGTH} символів) обовʼязковий у prod"
            )
        return secret
    if secret:
        return secret
    log.warning("KA_JWT_SECRET не задано: токени дійсні лише до перезапуску процесу")
    return secrets.token_urlsafe(48)


def create_app(
    settings: Settings | None = None,
    session_factory: "sessionmaker[Session] | None" = None,
    kv: KV | None = None,
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    settings = settings or get_settings()
    install_pii_filter()
    app = FastAPI(
        title="Knowledge Assistant",
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )
    checks: dict[str, Callable[[], object]] = {}

    if session_factory is None:
        engine = (
            make_engine(settings.database_url) if settings.database_url else make_memory_engine()
        )
        session_factory = make_session_factory(engine)
    sf = session_factory

    def check_db() -> None:
        with sf() as s:
            s.execute(text("SELECT 1"))

    checks["database"] = check_db

    store: KV
    if kv is not None:
        store = kv
    elif settings.redis_url:
        client = redis_lib.Redis.from_url(
            settings.redis_url, socket_timeout=1, decode_responses=True
        )
        checks["redis"] = client.ping
        store = cast(KV, client)
    else:
        store = MemoryKV(clock)

    tokens = TokenService(
        _jwt_secret(settings),
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience,
        ttl=settings.access_ttl,
        clock=clock,
    )
    app.state.kv = store
    app.state.session_factory = sf
    app.state.tokens = tokens
    app.state.limiter = RateLimiter(store, clock)
    app.state.limits = SimpleNamespace(
        login_limit=settings.login_limit,
        login_email_limit=settings.login_email_limit,
        login_window=settings.login_window,
        api_limit=settings.api_limit,
        api_window=settings.api_window,
        max_upload_bytes=settings.max_upload_bytes,
        presign_ttl=settings.presign_ttl,
    )
    storage = build_storage(settings)
    app.state.storage = storage
    queue: Queue
    if settings.redis_url:
        queue = ArqQueue(settings.redis_url)
    else:  # без Redis індексуємо одразу (dev, тести)
        queue = InlineQueue(
            Ingestor(sf, storage, build_embedder(settings), max_bytes=settings.max_upload_bytes)
        )
    app.state.queue = queue
    app.state.ready_checks = checks
    app.state.documents = DocumentService(DocumentRepository(sf))
    app.state.auth = AuthService(
        UserRepository(sf),
        RefreshRepository(sf),
        tokens,
        clock=clock,
        idle_ttl=settings.refresh_idle_ttl,
        absolute_ttl=settings.refresh_absolute_ttl,
    )
    install_error_handlers(app)
    app.include_router(router)
    return app


app = create_app()
