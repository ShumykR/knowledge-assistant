"""Бізнес-логіка. Не знає про HTTP: приймає прості типи, кидає DomainError."""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from app.errors import DomainError, NotFoundError
from app.repositories import (
    Document,
    DocumentRepository,
    EmailTaken,
    RefreshRepository,
    UserRecord,
    UserRepository,
)
from app.security import (
    TokenService,
    hash_password,
    hash_refresh_token,
    needs_rehash,
    new_refresh_token,
    password_problem,
    verify_password,
)


class Unauthorized(DomainError):
    status = 401
    title = "Unauthorized"


class Conflict(DomainError):
    status = 409
    title = "Conflict"


class DocumentService:
    def __init__(self, repo: DocumentRepository) -> None:
        self._repo = repo

    def create(self, owner_id: int, title: str) -> Document:
        return self._repo.add(owner_id, title.strip())

    def list_mine(self, owner_id: int) -> list[Document]:
        return self._repo.list_for_owner(owner_id)

    def update(self, doc_id: int, **fields: object) -> None:
        self._repo.update(doc_id, **fields)

    def get_mine(self, owner_id: int, doc_id: int) -> Document:
        doc = self._repo.get(doc_id)
        # чужий документ відповідає так само, як неіснуючий: без IDOR і без підказок
        if doc is None or doc.owner_id != owner_id:
            raise NotFoundError("Document not found")
        return doc


class AuthService:
    def __init__(
        self,
        users: UserRepository,
        refresh: RefreshRepository,
        tokens: TokenService,
        *,
        clock: Callable[[], float],
        idle_ttl: int,
        absolute_ttl: int,
    ) -> None:
        self._users = users
        self._refresh = refresh
        self._tokens = tokens
        self._clock = clock
        self._idle = idle_ttl
        self._abs = absolute_ttl
        # хеш для невідомого email: перевірка триває стільки ж, відповідь не виказує, чи є акаунт
        self._dummy = hash_password("dummy-password-for-timing")

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self._clock(), UTC)

    def register(self, email: str, password: str) -> UserRecord:
        problem = password_problem(password)
        if problem:
            raise DomainError(problem)
        try:
            return self._users.create(email.strip().lower(), hash_password(password))
        except EmailTaken as exc:
            raise Conflict("Email is already registered") from exc

    def login(self, email: str, password: str) -> dict[str, object]:
        user = self._users.by_email(email.strip().lower())
        stored = user.password_hash if user and user.password_hash else self._dummy
        ok = verify_password(password, stored)
        if not user or not user.password_hash or not ok:
            raise Unauthorized("Invalid email or password")  # одна відповідь для обох випадків
        if needs_rehash(user.password_hash):
            self._users.set_password_hash(user.id, hash_password(password))
        return self._issue(user, family_id=str(uuid.uuid4()), absolute=None)

    def refresh(self, token: str) -> dict[str, object]:
        record = self._refresh.get_by_hash(hash_refresh_token(token))
        now = self._now()
        if record is None:
            raise Unauthorized("Invalid refresh token")
        if record.revoked:
            # повторне використання старого токена: його могли вкрасти. Закриваємо всю родину
            self._refresh.revoke_family(record.family_id, now)
            raise Unauthorized("Invalid refresh token")
        if now >= record.expires_at or now >= record.absolute_expires_at:
            raise Unauthorized("Refresh token expired")
        if not self._refresh.revoke(record.id, now):  # двоє паралельних refresh: виграє один
            self._refresh.revoke_family(record.family_id, now)
            raise Unauthorized("Invalid refresh token")
        user = self._users.by_id(record.user_id)
        if user is None:
            raise Unauthorized("Invalid refresh token")
        return self._issue(user, family_id=record.family_id, absolute=record.absolute_expires_at)

    def logout(self, token: str) -> None:
        record = self._refresh.get_by_hash(hash_refresh_token(token))
        if record is not None:
            self._refresh.revoke_family(record.family_id, self._now())

    def _issue(
        self, user: UserRecord, *, family_id: str, absolute: datetime | None
    ) -> dict[str, object]:
        now = self._now()
        absolute = absolute or now + timedelta(seconds=self._abs)
        expires = min(now + timedelta(seconds=self._idle), absolute)  # ковзне, але не довше за межу
        raw = new_refresh_token()
        self._refresh.add(user.id, family_id, hash_refresh_token(raw), expires, absolute)
        return {
            "access_token": self._tokens.issue_access(user.id, user.role),
            "refresh_token": raw,
            "token_type": "bearer",
            "expires_in": self._tokens.ttl,
        }

    def user_for(self, user_id: int) -> UserRecord | None:
        return self._users.by_id(user_id)

    def all_users(self) -> list[UserRecord]:
        return self._users.list_all()
