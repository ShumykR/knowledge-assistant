"""Паролі (scrypt) і токени (JWT access + непрозорий refresh)."""

import base64
import hashlib
import hmac
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import jwt

# --- паролі -------------------------------------------------------------------------------
# Параметри зберігаються в самому записі, тож їх можна посилювати: старі хеші перевіряються за
# своїми параметрами й перехешуються при вході (needs_rehash).
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**15, 8, 1
SCRYPT_MAXMEM = 128 * 1024 * 1024
MIN_PASSWORD, MAX_PASSWORD = 12, 128  # верхня межа: захист від DoS довгим паролем


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode(), salt=salt, n=n, r=r, p=p, maxmem=SCRYPT_MAXMEM, dklen=32
    )


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def hash_password(password: str, *, n: int | None = None) -> str:
    n = n or SCRYPT_N
    salt = secrets.token_bytes(16)
    digest = _scrypt(password, salt, n, SCRYPT_R, SCRYPT_P)
    return f"scrypt${n}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = _scrypt(password, base64.b64decode(salt), int(n), int(r), int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def needs_rehash(stored: str) -> bool:
    try:
        _, n, r, p, *_ = stored.split("$")
        return (int(n), int(r), int(p)) != (SCRYPT_N, SCRYPT_R, SCRYPT_P)
    except ValueError:
        return True


def password_problem(password: str) -> str | None:
    if len(password) < MIN_PASSWORD:
        return f"Password must be at least {MIN_PASSWORD} characters"
    if len(password) > MAX_PASSWORD:
        return f"Password must be at most {MAX_PASSWORD} characters"
    return None


# --- токени -------------------------------------------------------------------------------
ALGORITHM = "HS256"  # алгоритм зафіксований; з токена його не читаємо


class InvalidToken(Exception):
    pass


@dataclass(frozen=True)
class Claims:
    user_id: int
    role: str


class TokenService:
    def __init__(
        self,
        secret: str,
        *,
        issuer: str,
        audience: str,
        ttl: int,
        clock: Callable[[], float],
    ) -> None:
        self._secret = secret
        self._iss = issuer
        self._aud = audience
        self._ttl = ttl
        self._clock = clock

    @property
    def ttl(self) -> int:
        return self._ttl

    def issue_access(self, user_id: int, role: str) -> str:
        now = int(self._clock())
        payload = {
            "sub": str(user_id),
            "role": role,
            "iss": self._iss,
            "aud": self._aud,
            "iat": now,
            "exp": now + self._ttl,
            "typ": "access",
        }
        return jwt.encode(payload, self._secret, algorithm=ALGORITHM)

    def verify_access(self, token: str) -> Claims:
        try:
            data: dict[str, Any] = jwt.decode(
                token,
                self._secret,
                algorithms=[ALGORITHM],
                audience=self._aud,
                issuer=self._iss,
                # час перевіряємо самі за нашим годинником (детермінованість тестів)
                options={"verify_exp": False, "require": ["exp", "iat", "sub", "iss", "aud"]},
            )
            if data.get("typ") != "access" or data["exp"] <= self._clock():
                raise InvalidToken
            return Claims(int(data["sub"]), str(data["role"]))
        except (jwt.PyJWTError, KeyError, ValueError, TypeError) as exc:
            raise InvalidToken from exc


def new_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def hash_refresh_token(token: str) -> str:
    # токен довгий і випадковий, тож швидкий SHA-256 достатній (як у завданні №166 SkillBoost)
    return hashlib.sha256(token.encode()).hexdigest()
