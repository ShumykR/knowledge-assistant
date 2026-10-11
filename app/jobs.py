"""Черга завдань інгесту: ARQ (Redis) у production, виконання на місці без Redis (dev, тести)."""

import asyncio
import logging
from typing import Any, Protocol

from fastapi.concurrency import run_in_threadpool

from app.ingest import Ingestor, process

log = logging.getLogger(__name__)


class Queue(Protocol):
    async def enqueue_ingest(self, document_id: int) -> None: ...


class InlineQueue:
    """Без Redis: індексує одразу, в потоці. Одна спроба, помилка йде в dead_letters."""

    def __init__(self, ingestor: Ingestor) -> None:
        self._ingestor = ingestor

    async def enqueue_ingest(self, document_id: int) -> None:
        await run_in_threadpool(process, self._ingestor, document_id, 1, 1)


class ArqQueue:
    def __init__(self, redis_url: str) -> None:
        self._url = redis_url
        self._pool: Any = None
        self._lock = asyncio.Lock()

    async def _get_pool(self) -> Any:
        async with self._lock:
            if self._pool is None:
                from arq import create_pool
                from arq.connections import RedisSettings

                self._pool = await create_pool(RedisSettings.from_dsn(self._url))
            return self._pool

    async def enqueue_ingest(self, document_id: int) -> None:
        pool = await self._get_pool()
        # _job_id робить постановку ідемпотентною: той самий документ двічі в черзі не буде
        await pool.enqueue_job("ingest_document", document_id, _job_id=f"ingest:{document_id}")
