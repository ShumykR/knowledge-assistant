"""Інтеграційні тести на справжньому PostgreSQL + pgvector.

Запуск: docker compose up -d --wait db, потім
KA_TEST_DATABASE_URL=postgresql+psycopg://ka:ka-dev-only@127.0.0.1:5432/ka pytest -m integration
Тести ЗНИЩУЮТЬ дані в цій базі (downgrade до base), тож не вказуй чужу базу.
"""

import os
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.main import create_app
from app.models import EMBEDDING_DIM, Base, Chunk, Document, User
from app.repositories import DocumentRepository, EmailTaken, RefreshRepository, UserRepository

URL = os.environ.get("KA_TEST_DATABASE_URL", "")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not URL, reason="KA_TEST_DATABASE_URL не задано"),
]


def alembic_cfg() -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", URL)
    return cfg


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    cfg = alembic_cfg()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    eng = create_engine(URL)
    yield eng
    eng.dispose()


@pytest.fixture
def sf(engine: Engine) -> Iterator["sessionmaker[Session]"]:
    with engine.begin() as c:
        c.execute(
            text(
                "TRUNCATE users, documents, chunks, chat_sessions, outbox_events, dead_letters RESTART IDENTITY CASCADE"
            )
        )
    yield sessionmaker(engine, expire_on_commit=False)


def vec(first: float) -> list[float]:
    return [first] + [0.0] * (EMBEDDING_DIM - 1)


def test_models_match_migrations(engine: Engine) -> None:
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


def test_downgrade_and_upgrade_again(engine: Engine) -> None:
    cfg = alembic_cfg()
    command.downgrade(cfg, "0001")  # відкат лише expand-міграції
    command.upgrade(cfg, "head")


def test_repository_roundtrip(sf: "sessionmaker[Session]") -> None:
    users, repo = UserRepository(sf), DocumentRepository(sf)
    alice = users.create("alice@x.io", "hash")
    bob = users.create("bob@x.io", "hash")
    first = repo.add(alice.id, "Перший")
    second = repo.add(alice.id, "Другий")
    repo.add(bob.id, "Чужий")
    assert repo.get(first.id) == first
    assert [d.title for d in repo.list_for_owner(alice.id)] == ["Другий", "Перший"]
    assert [d.title for d in repo.list_for_owner(bob.id)] == ["Чужий"]
    assert repo.get(second.id + 100) is None


def test_email_is_unique(sf: "sessionmaker[Session]") -> None:
    users = UserRepository(sf)
    users.create("a@x.io", "h")
    with pytest.raises(EmailTaken):
        users.create("a@x.io", "h")


def test_refresh_token_revoke_is_atomic(sf: "sessionmaker[Session]") -> None:
    from datetime import UTC, datetime, timedelta

    user = UserRepository(sf).create("a@x.io", "h")
    refresh = RefreshRepository(sf)
    now = datetime.now(UTC)
    refresh.add(user.id, "fam", "hash1", now + timedelta(days=1), now + timedelta(days=2))
    record = refresh.get_by_hash("hash1")
    assert record is not None and not record.revoked
    assert refresh.revoke(record.id, now) is True
    assert refresh.revoke(record.id, now) is False  # другий паралельний refresh програє
    again = refresh.get_by_hash("hash1")
    assert again is not None and again.revoked


def test_deleting_document_cascades_to_chunks(sf: "sessionmaker[Session]") -> None:
    with sf.begin() as s:
        user = User(email="a@x.io")
        s.add(user)
        s.flush()
        doc = Document(
            owner_id=user.id, title="d", chunks=[Chunk(position=0, content="c", embedding=vec(1))]
        )
        s.add(doc)
        s.flush()
        doc_id = doc.id
    with sf.begin() as s:
        s.delete(s.get(Document, doc_id))
    with sf() as s:
        assert s.execute(text("SELECT count(*) FROM chunks")).scalar_one() == 0


def test_chunk_position_is_unique_per_document(sf: "sessionmaker[Session]") -> None:
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError), sf.begin() as s:
        user = User(email="a@x.io")
        s.add(user)
        s.flush()
        doc = Document(owner_id=user.id, title="d")
        s.add(doc)
        s.flush()
        s.add_all(
            [
                Chunk(document_id=doc.id, position=0, content="a", embedding=vec(1)),
                Chunk(document_id=doc.id, position=0, content="b", embedding=vec(1)),
            ]
        )


def test_nearest_neighbours_by_cosine(sf: "sessionmaker[Session]") -> None:
    from sqlalchemy import select

    with sf.begin() as s:
        user = User(email="a@x.io")
        s.add(user)
        s.flush()
        doc = Document(owner_id=user.id, title="d")
        s.add(doc)
        s.flush()
        near = [1.0, 0.1] + [0.0] * (EMBEDDING_DIM - 2)
        far = [0.0, 1.0] + [0.0] * (EMBEDDING_DIM - 2)
        s.add_all(
            [
                Chunk(document_id=doc.id, position=0, content="far", embedding=far),
                Chunk(document_id=doc.id, position=1, content="near", embedding=near),
            ]
        )
    with sf() as s:
        query = [1.0] + [0.0] * (EMBEDDING_DIM - 1)
        rows = s.scalars(
            select(Chunk).order_by(Chunk.embedding.cosine_distance(query)).limit(2)
        ).all()
    assert [c.content for c in rows] == ["near", "far"]


def test_vector_search_can_use_hnsw_index(engine: Engine) -> None:
    q = "[" + ",".join(["1"] + ["0"] * (EMBEDDING_DIM - 1)) + "]"
    with engine.begin() as c:
        c.execute(
            text("SET LOCAL enable_seqscan = off")
        )  # на порожній таблиці планувач інакше обирає Seq Scan
        plan = "\n".join(
            r[0]
            for r in c.execute(
                text(
                    "EXPLAIN SELECT id FROM chunks ORDER BY embedding <=> CAST(:q AS vector) LIMIT 5"
                ),
                {"q": q},
            )
        )
    assert "ix_chunks_embedding_hnsw" in plan, plan


def test_documents_listing_uses_composite_index(engine: Engine) -> None:
    with engine.begin() as c:
        c.execute(text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join(
            r[0]
            for r in c.execute(
                text(
                    "EXPLAIN SELECT id FROM documents WHERE owner_id = 1 ORDER BY created_at DESC LIMIT 20"
                )
            )
        )
    assert "ix_documents_owner_created" in plan and "Sort" not in plan, plan


def test_ready_ok_and_fails_when_db_is_down(sf: "sessionmaker[Session]") -> None:
    ok = TestClient(create_app(Settings(), session_factory=sf))
    assert ok.get("/health/ready").status_code == 200

    dead = create_engine(
        "postgresql+psycopg://ka:x@127.0.0.1:1/ka", connect_args={"connect_timeout": 1}
    )
    down = TestClient(create_app(Settings(), session_factory=sessionmaker(dead)))
    r = down.get("/health/ready")
    assert r.status_code == 503 and r.json()["failed"] == ["database"]
