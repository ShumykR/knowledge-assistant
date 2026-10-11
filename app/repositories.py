"""Шар даних на SQLAlchemy. Сервіси отримують `sessionmaker` і самі не знають про SQL.

У dev і швидких тестах працює той самий код на SQLite у памʼяті (див. app/db.py), у production
на PostgreSQL: окремих «фейкових» репозиторіїв немає, тож тести перевіряють справжній SQL.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.models import Document as DocumentRow
from app.models import RefreshToken, User


def aware(dt: datetime) -> datetime:
    """SQLite повертає naive datetime; у БД ми завжди зберігаємо UTC."""
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Document:
    id: int
    owner_id: int
    title: str
    status: str = "created"
    filename: str | None = None
    content_type: str | None = None
    size_bytes: int | None = None
    storage_key: str | None = None
    error: str | None = None


def _doc(row: DocumentRow) -> Document:
    return Document(
        row.id,
        row.owner_id,
        row.title,
        row.status,
        row.filename,
        row.content_type,
        row.size_bytes,
        row.storage_key,
        row.error,
    )


FILE_FIELDS = {"status", "filename", "content_type", "size_bytes", "storage_key", "error"}


@dataclass(frozen=True)
class UserRecord:
    id: int
    email: str
    role: str
    password_hash: str | None


@dataclass(frozen=True)
class RefreshRecord:
    id: int
    user_id: int
    family_id: str
    expires_at: datetime
    absolute_expires_at: datetime
    revoked: bool


class EmailTaken(Exception):
    pass


def _user(row: User) -> UserRecord:
    return UserRecord(row.id, row.email, row.role, row.password_hash)


class DocumentRepository:
    def __init__(self, sf: "sessionmaker[Session]") -> None:
        self._sf = sf

    def add(self, owner_id: int, title: str) -> Document:
        with self._sf.begin() as s:
            row = DocumentRow(owner_id=owner_id, title=title)
            s.add(row)
            s.flush()
            return _doc(row)

    def get(self, doc_id: int) -> Document | None:
        with self._sf() as s:
            row = s.get(DocumentRow, doc_id)
            return None if row is None else _doc(row)

    def list_for_owner(self, owner_id: int) -> list[Document]:
        with self._sf() as s:
            rows = s.scalars(
                select(DocumentRow)
                .where(DocumentRow.owner_id == owner_id)
                .order_by(DocumentRow.created_at.desc(), DocumentRow.id.desc())
            ).all()
            return [_doc(r) for r in rows]

    def update(self, doc_id: int, **fields: object) -> None:
        unknown = set(fields) - FILE_FIELDS
        if unknown:
            raise ValueError(f"unknown fields: {sorted(unknown)}")
        with self._sf.begin() as s:
            s.execute(update(DocumentRow).where(DocumentRow.id == doc_id).values(**fields))


class UserRepository:
    def __init__(self, sf: "sessionmaker[Session]") -> None:
        self._sf = sf

    def create(self, email: str, password_hash: str, role: str = "user") -> UserRecord:
        try:
            with self._sf.begin() as s:
                row = User(email=email, password_hash=password_hash, role=role)
                s.add(row)
                s.flush()
                return _user(row)
        except IntegrityError as exc:
            raise EmailTaken from exc

    def by_email(self, email: str) -> UserRecord | None:
        with self._sf() as s:
            row = s.scalar(select(User).where(User.email == email))
            return None if row is None else _user(row)

    def by_id(self, user_id: int) -> UserRecord | None:
        with self._sf() as s:
            row = s.get(User, user_id)
            return None if row is None else _user(row)

    def list_all(self) -> list[UserRecord]:
        with self._sf() as s:
            return [_user(r) for r in s.scalars(select(User).order_by(User.id))]

    def set_password_hash(self, user_id: int, password_hash: str) -> None:
        with self._sf.begin() as s:
            s.execute(update(User).where(User.id == user_id).values(password_hash=password_hash))


class RefreshRepository:
    def __init__(self, sf: "sessionmaker[Session]") -> None:
        self._sf = sf

    def add(
        self,
        user_id: int,
        family_id: str,
        token_hash: str,
        expires_at: datetime,
        absolute_expires_at: datetime,
    ) -> None:
        with self._sf.begin() as s:
            s.add(
                RefreshToken(
                    user_id=user_id,
                    family_id=family_id,
                    token_hash=token_hash,
                    expires_at=expires_at,
                    absolute_expires_at=absolute_expires_at,
                )
            )

    def get_by_hash(self, token_hash: str) -> RefreshRecord | None:
        with self._sf() as s:
            r = s.scalar(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
            if r is None:
                return None
            return RefreshRecord(
                r.id,
                r.user_id,
                r.family_id,
                aware(r.expires_at),
                aware(r.absolute_expires_at),
                r.revoked_at is not None,
            )

    def revoke(self, token_id: int, now: datetime) -> bool:
        """Атомарно відкликає токен; False, якщо його вже відкликали (змагання двох refresh)."""
        with self._sf.begin() as s:
            res = s.execute(
                update(RefreshToken)
                .where(RefreshToken.id == token_id, RefreshToken.revoked_at.is_(None))
                .values(revoked_at=now)
            )
            return bool(res.rowcount)  # type: ignore[attr-defined]

    def revoke_family(self, family_id: str, now: datetime) -> None:
        with self._sf.begin() as s:
            s.execute(
                update(RefreshToken)
                .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
                .values(revoked_at=now)
            )
