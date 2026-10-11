from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, Chunk, DeadLetter, Document, OutboxEvent, RefreshToken, User


def make_engine(url: str) -> Engine:
    # pool_pre_ping: мертве зʼєднання (рестарт БД) виявляється до запиту, а не під ним
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)


def make_memory_engine() -> Engine:
    """SQLite у памʼяті для dev і тестів (вектор там зберігається як JSON)."""
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    tables = [t.__table__ for t in (User, Document, Chunk, RefreshToken, OutboxEvent, DeadLetter)]
    Base.metadata.create_all(engine, tables=tables)  # type: ignore[arg-type]
    return engine


def make_session_factory(engine: Engine) -> "sessionmaker[Session]":
    return sessionmaker(engine, expire_on_commit=False)
