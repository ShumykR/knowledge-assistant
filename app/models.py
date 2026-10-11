"""Схема БД (SQLAlchemy 2.0). Міграції Alembic (migrations/) мають збігатися з моделями."""

from datetime import UTC, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

EMBEDDING_DIM = 1536  # має збігатися з обраною моделлю embeddings


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="user", server_default="user")


class Document(Base):
    __tablename__ = "documents"
    # «мої документи, нові першими»: рівність по owner_id + сортування по created_at
    __table_args__ = (Index("ix_documents_owner_created", "owner_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(200))
    # created -> queued -> indexed | failed; змінюється завантаженням і фоновим воркером
    status: Mapped[str] = mapped_column(String(16), default="created", server_default="created")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # expand-фаза міграції 0002: NULLABLE, поки не заповнені старі рядки
    content_hash: Mapped[str | None] = mapped_column(String(64))
    # метадані файлу (M3): ім'я лише для показу, у сховищі файл лежить під випадковим ключем
    filename: Mapped[str | None] = mapped_column(String(120))
    content_type: Mapped[str | None] = mapped_column(String(64))
    size_bytes: Mapped[int | None]
    storage_key: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(String(500))

    chunks: Mapped[list["Chunk"]] = relationship(cascade="all, delete-orphan", passive_deletes=True)


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "position", name="uq_chunks_document_position"),
        # приблизний пошук найближчих сусідів (косинусна відстань); параметри HNSW у міграції
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    position: Mapped[int]
    content: Mapped[str] = mapped_column(Text)
    # на SQLite (dev і швидкі тести) вектор зберігається як JSON; в PostgreSQL це pgvector
    embedding: Mapped[list[float]] = mapped_column(
        Vector(EMBEDDING_DIM).with_variant(JSON(), "sqlite")
    )


class ChatSession(Base):
    __tablename__ = "chat_sessions"
    __table_args__ = (Index("ix_chat_sessions_user_created", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RefreshToken(Base):
    """Refresh-токен: зберігається лише SHA-256, використовується один раз (ротація)."""

    __tablename__ = "refresh_tokens"
    __table_args__ = (Index("ix_refresh_tokens_family", "family_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    family_id: Mapped[str] = mapped_column(String(36))  # один вхід = одна «родина» токенів
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # за неактивністю
    absolute_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True)
    )  # не продовжується
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OutboxEvent(Base):
    """Подія пишеться в тій самій транзакції, що й дані; публікується окремо (at-least-once)."""

    __tablename__ = "outbox_events"
    __table_args__ = (Index("ix_outbox_published_id", "published_at", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, object]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DeadLetter(Base):
    """Завдання, що вичерпало спроби: лежить тут для розбору й ручного повтору."""

    __tablename__ = "dead_letters"

    id: Mapped[int] = mapped_column(primary_key=True)
    job: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, object]] = mapped_column(JSON)
    error: Mapped[str] = mapped_column(String(1000))
    attempts: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
