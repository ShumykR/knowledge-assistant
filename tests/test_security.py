import base64
import json

import pytest

from app import security
from app.security import (
    InvalidToken,
    TokenService,
    hash_password,
    needs_rehash,
    password_problem,
    verify_password,
)
from tests.conftest import Clock

PW = "correct horse battery"


def test_hash_verify_roundtrip() -> None:
    h = hash_password(PW)
    assert h.startswith("scrypt$")
    assert verify_password(PW, h)
    assert not verify_password(PW + "x", h)


def test_salt_makes_hashes_differ() -> None:
    assert hash_password(PW) != hash_password(PW)


def test_password_not_in_hash() -> None:
    assert PW not in hash_password(PW)


@pytest.mark.parametrize("garbage", ["", "x", "scrypt$1$2", "md5$1$1$1$a$b", "scrypt$a$b$c$d$e"])
def test_garbage_hash_never_verifies(garbage: str) -> None:
    assert verify_password(PW, garbage) is False


def test_old_params_still_verify_and_need_rehash() -> None:
    old = hash_password(PW, n=2**9)
    assert verify_password(PW, old)
    assert needs_rehash(old)
    assert not needs_rehash(hash_password(PW))


def test_password_policy() -> None:
    assert password_problem("short") is not None
    assert password_problem("x" * 12) is None
    assert password_problem("x" * 129) is not None


def make_tokens(clock: Clock, **kw: object) -> TokenService:
    args: dict[str, object] = {"issuer": "iss", "audience": "aud", "ttl": 900, "clock": clock}
    args.update(kw)
    return TokenService("s" * 40, **args)  # type: ignore[arg-type]


def b64(data: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def test_access_token_roundtrip(clock: Clock) -> None:
    t = make_tokens(clock)
    claims = t.verify_access(t.issue_access(7, "admin"))
    assert (claims.user_id, claims.role) == (7, "admin")


def test_access_token_expires(clock: Clock) -> None:
    t = make_tokens(clock)
    token = t.issue_access(1, "user")
    clock.advance(899)
    t.verify_access(token)
    clock.advance(1)
    with pytest.raises(InvalidToken):
        t.verify_access(token)


def test_wrong_secret_issuer_audience_rejected(clock: Clock) -> None:
    token = make_tokens(clock).issue_access(1, "user")
    other = TokenService("t" * 40, issuer="iss", audience="aud", ttl=900, clock=clock)
    with pytest.raises(InvalidToken):
        other.verify_access(token)
    with pytest.raises(InvalidToken):
        make_tokens(clock, issuer="evil").verify_access(token)
    with pytest.raises(InvalidToken):
        make_tokens(clock, audience="other").verify_access(token)


def test_alg_none_and_garbage_rejected(clock: Clock) -> None:
    forged = f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64({'sub': '1', 'role': 'admin'})}."
    t = make_tokens(clock)
    for bad in (forged, "", "a.b.c", "not-a-jwt"):
        with pytest.raises(InvalidToken):
            t.verify_access(bad)


def test_tampered_payload_rejected(clock: Clock) -> None:
    t = make_tokens(clock)
    head, payload, sig = t.issue_access(1, "user").split(".")
    data = json.loads(base64.urlsafe_b64decode(payload + "=="))
    data["role"] = "admin"
    with pytest.raises(InvalidToken):
        t.verify_access(f"{head}.{b64(data)}.{sig}")


def test_scrypt_defaults_are_strong() -> None:
    assert security.SCRYPT_R == 8 and security.MIN_PASSWORD >= 12
