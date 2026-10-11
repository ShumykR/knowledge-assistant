from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import security
from app.config import Settings
from app.main import create_app

PASSWORD = "correct horse battery"


class Clock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def fast_scrypt(monkeypatch: pytest.MonkeyPatch) -> None:
    # у тестах слабкі параметри scrypt (швидко); у production діють SCRYPT_N з app/security.py
    monkeypatch.setattr(security, "SCRYPT_N", 2**10)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def app(clock: Clock) -> FastAPI:
    return create_app(Settings(jwt_secret="s" * 40), clock=clock)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def register(client: TestClient, email: str, password: str = PASSWORD) -> None:
    r = client.post("/api/v1/auth/register", json={"email": email, "password": password})
    assert r.status_code == 201, r.text


def login(client: TestClient, email: str, password: str = PASSWORD) -> dict[str, Any]:
    r = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


def signup(client: TestClient, email: str) -> dict[str, str]:
    """Реєструє користувача й повертає заголовки з його access-токеном."""
    register(client, email)
    return {"Authorization": f"Bearer {login(client, email)['access_token']}"}
