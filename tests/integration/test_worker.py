"""ARQ-воркер на справжніх PostgreSQL і Redis: черга, ретраї, dead letters, outbox, векторний пошук.

Запуск: KA_TEST_DATABASE_URL=... KA_TEST_REDIS_URL=... pytest -m integration
"""

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
import redis
from arq.connections import RedisSettings
from arq.worker import Worker
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app import config, worker
from app.config import Settings
from app.ingest import HashEmbedder, publish_pending
from app.main import create_app
from app.models import Chunk, DeadLetter, OutboxEvent
from tests.conftest import PASSWORD

DB = os.environ.get("KA_TEST_DATABASE_URL", "")
REDIS = os.environ.get("KA_TEST_REDIS_URL", "")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (DB and REDIS), reason="потрібні KA_TEST_DATABASE_URL і KA_TEST_REDIS_URL"
    ),
]


@pytest.fixture
def stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    with create_engine(DB).begin() as c:
        c.execute(text("TRUNCATE users, outbox_events, dead_letters RESTART IDENTITY CASCADE"))
    redis.Redis.from_url(REDIS).flushdb()
    env = {
        "KA_DATABASE_URL": DB,
        "KA_REDIS_URL": REDIS,
        "KA_JWT_SECRET": "s" * 40,
        "KA_STORAGE_DIR": str(tmp_path / "uploads"),
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    config.get_settings.cache_clear()
    monkeypatch.setattr(worker, "retry_delay", lambda attempt: 0)  # без очікування між спробами
    monkeypatch.setattr(worker, "MAX_TRIES", 3)
    # `with`: один event loop на весь тест, як у справжньому сервері (пул ARQ привʼязаний до loop)
    with TestClient(create_app(Settings()), raise_server_exceptions=False) as client:
        yield client


def run_worker() -> None:
    async def go() -> None:
        # Worker створюється ВСЕРЕДИНІ запущеного loop: він бере asyncio.get_event_loop() в __init__
        w = Worker(
            functions=worker.WorkerSettings.functions,
            on_startup=worker.startup,
            redis_settings=RedisSettings.from_dsn(REDIS),
            burst=True,
            max_tries=worker.MAX_TRIES + 1,
            handle_signals=False,
        )
        await asyncio.wait_for(w.main(), 60)
        await w.pool.aclose()  # на Windows w.close() потребує SIGUSR1, якого там немає

    asyncio.run(go())


def login(client: TestClient) -> dict[str, str]:
    body = {"email": "a@x.io", "password": PASSWORD}
    client.post("/api/v1/auth/register", json=body)
    token = client.post("/api/v1/auth/login", json=body).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def upload(client: TestClient, h: dict[str, str], data: bytes) -> int:
    doc_id = client.post("/api/v1/documents", json={"title": "t"}, headers=h).json()["id"]
    r = client.put(
        f"/api/v1/documents/{doc_id}/file", params={"filename": "a.txt"}, content=data, headers=h
    )
    assert r.status_code == 202 and r.json()["status"] == "queued"  # у черзі, ще не проіндексовано
    return int(doc_id)


def status(client: TestClient, h: dict[str, str], doc_id: int) -> dict[str, object]:
    r = client.get(f"/api/v1/documents/{doc_id}", headers=h)
    body: dict[str, object] = r.json()
    return body


def test_worker_indexes_document_and_vector_search_finds_it(stack: TestClient) -> None:
    h = login(stack)
    doc1 = upload(stack, h, "Як налаштувати резервні копії бази даних PostgreSQL".encode())
    doc2 = upload(stack, h, "Рецепт борщу з буряком і капустою".encode())
    run_worker()
    assert status(stack, h, doc1)["status"] == status(stack, h, doc2)["status"] == "indexed"

    query = HashEmbedder().embed(["резервні копії бази"])[0]
    with Session(create_engine(DB)) as s:
        nearest = s.scalars(
            select(Chunk).order_by(Chunk.embedding.cosine_distance(query)).limit(1)
        ).one()
        assert nearest.document_id == doc1
        events = s.scalars(select(OutboxEvent)).all()
    assert len(events) == 2 and all(e.event_type == "document.indexed" for e in events)


def test_failing_job_is_retried_then_lands_in_dead_letters(
    stack: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = login(stack)
    doc_id = upload(stack, h, b"some text that will never be embedded")

    class Broken:
        def embed(self, texts: list[str]) -> list[list[float]]:
            raise ConnectionError("embeddings API is down")

    monkeypatch.setattr("app.worker.build_embedder", lambda settings: Broken())
    run_worker()

    doc = status(stack, h, doc_id)
    assert doc["status"] == "failed" and "ConnectionError" in str(doc["error"])
    with Session(create_engine(DB)) as s:
        dead = s.scalars(select(DeadLetter)).one()
    assert dead.attempts == 3 and dead.payload == {"document_id": doc_id}


def test_publish_outbox_job_marks_events(stack: TestClient) -> None:
    h = login(stack)
    upload(stack, h, b"hello outbox world")
    run_worker()
    sf = stack.app.state.session_factory  # type: ignore[attr-defined]
    seen: list[str] = []
    assert publish_pending(sf, lambda t, p: seen.append(t)) == 1
    assert seen == ["document.indexed"]
    assert publish_pending(sf, lambda t, p: seen.append(t)) == 0
