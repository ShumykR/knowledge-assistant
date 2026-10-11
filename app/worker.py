"""Воркер ARQ: `arq app.worker.WorkerSettings`.

- `ingest_document`: до MAX_TRIES спроб із наростаючою паузою; після останньої документ
  стає `failed`, а завдання потрапляє в таблицю `dead_letters`;
- `publish_outbox`: щоп'ять секунд публікує події `outbox_events` (at-least-once).
"""

import asyncio
import logging
from typing import Any

from arq import Retry, cron
from arq.connections import RedisSettings

from app.config import get_settings
from app.db import make_engine, make_session_factory
from app.ingest import Ingestor, JobOutcome, process, publish_pending, retry_delay
from app.wiring import build_embedder, build_storage

log = logging.getLogger(__name__)
MAX_TRIES = 4


def log_publisher(event_type: str, payload: dict[str, Any]) -> None:
    """Заглушка шини подій: у наступних віхах сюди підключається справжній споживач."""
    log.info("event %s %s", event_type, payload)


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    sf = make_session_factory(make_engine(settings.database_url))
    ctx["sf"] = sf
    ctx["ingestor"] = Ingestor(
        sf, build_storage(settings), build_embedder(settings), max_bytes=settings.max_upload_bytes
    )


async def ingest_document(ctx: dict[str, Any], document_id: int) -> str:
    outcome = await asyncio.to_thread(
        process, ctx["ingestor"], document_id, ctx["job_try"], MAX_TRIES
    )
    if outcome == JobOutcome.RETRY:
        raise Retry(defer=retry_delay(ctx["job_try"]))
    return outcome


async def publish_outbox(ctx: dict[str, Any]) -> int:
    return await asyncio.to_thread(publish_pending, ctx["sf"], log_publisher)


class WorkerSettings:
    functions = [ingest_document]
    cron_jobs = [cron(publish_outbox, second={0, 10, 20, 30, 40, 50}, run_at_startup=True)]
    on_startup = startup
    max_tries = (
        MAX_TRIES + 1
    )  # запас: остаточну невдачу фіксуємо самі, а не через MaxRetriesExceeded
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url or "redis://127.0.0.1:6379/0")
