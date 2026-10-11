"""Наскрізний сценарій на справжніх PostgreSQL і Redis: реєстрація, вхід, документи, ідемпотентність."""

import os

import pytest
import redis
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.config import Settings
from app.main import create_app

DB = os.environ.get("KA_TEST_DATABASE_URL", "")
REDIS = os.environ.get("KA_TEST_REDIS_URL", "")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (DB and REDIS), reason="потрібні KA_TEST_DATABASE_URL і KA_TEST_REDIS_URL"
    ),
]


def test_full_flow_on_postgres_and_redis() -> None:
    # схема вже створена міграціями (див. test_postgres.py); чистимо дані
    with create_engine(DB).begin() as c:
        c.execute(text("TRUNCATE users, documents, refresh_tokens RESTART IDENTITY CASCADE"))
    redis.Redis.from_url(REDIS).flushdb()

    app = create_app(Settings(database_url=DB, redis_url=REDIS, jwt_secret="s" * 40))
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/health/ready").status_code == 200

    pw = "correct horse battery"
    assert (
        client.post("/api/v1/auth/register", json={"email": "a@x.io", "password": pw}).status_code
        == 201
    )
    assert (
        client.post("/api/v1/auth/register", json={"email": "A@x.io", "password": pw}).status_code
        == 409
    )
    tokens = client.post("/api/v1/auth/login", json={"email": "a@x.io", "password": pw}).json()
    headers = {"Authorization": f"Bearer {tokens['access_token']}", "Idempotency-Key": "k1"}

    first = client.post("/api/v1/documents", json={"title": "Звіт"}, headers=headers)
    second = client.post("/api/v1/documents", json={"title": "Звіт"}, headers=headers)
    assert first.status_code == second.status_code == 201 and first.json() == second.json()
    assert second.headers["idempotency-replayed"] == "true"
    assert len(client.get("/api/v1/documents", headers=headers).json()) == 1

    rotated = client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert rotated.status_code == 200
    again = client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert again.status_code == 401
    still = client.post(
        "/api/v1/auth/refresh", json={"refresh_token": rotated.json()["refresh_token"]}
    )
    assert still.status_code == 401  # родину закрито після повторного використання
