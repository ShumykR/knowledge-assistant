import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.conftest import signup


def test_health(client: TestClient) -> None:
    assert client.get("/health/live").json() == {"status": "ok"}
    assert client.get("/health/ready").status_code == 200


def test_create_and_list_own_documents(client: TestClient) -> None:
    alice = signup(client, "alice@x.io")
    bob = signup(client, "bob@x.io")
    r = client.post("/api/v1/documents", json={"title": " Договір "}, headers=alice)
    assert r.status_code == 201
    assert (
        r.json()["id"] == 1 and r.json()["title"] == "Договір" and r.json()["status"] == "created"
    )
    assert client.get("/api/v1/documents", headers=alice).json() == [r.json()]
    assert client.get("/api/v1/documents", headers=bob).json() == []


def test_foreign_document_looks_like_missing(client: TestClient) -> None:
    alice = signup(client, "alice@x.io")
    bob = signup(client, "bob@x.io")
    doc_id = client.post("/api/v1/documents", json={"title": "x"}, headers=alice).json()["id"]
    foreign = client.get(f"/api/v1/documents/{doc_id}", headers=bob)
    missing = client.get("/api/v1/documents/999", headers=bob)
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()


def test_errors_are_problem_json(client: TestClient) -> None:
    alice = signup(client, "alice@x.io")
    r = client.get("/api/v1/documents/999", headers=alice)
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["status"] == 404 and r.json()["title"] == "Not found"


def test_validation_error_does_not_echo_input(client: TestClient) -> None:
    alice = signup(client, "alice@x.io")
    r = client.post("/api/v1/documents", json={"title": ""}, headers=alice)
    assert r.status_code == 422
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["errors"][0]["loc"] == ["body", "title"]


def test_extra_fields_are_rejected(client: TestClient) -> None:
    alice = signup(client, "alice@x.io")
    r = client.post(
        "/api/v1/documents", json={"title": "x", "owner_id": 999, "role": "admin"}, headers=alice
    )
    assert r.status_code == 422


def test_unhandled_error_is_hidden(app: FastAPI) -> None:
    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("secret internals")

    r = TestClient(app, raise_server_exceptions=False).get("/boom")
    assert r.status_code == 500
    assert "secret" not in r.text
    assert r.headers["content-type"].startswith("application/problem+json")


def test_docs_disabled_in_prod() -> None:
    c = TestClient(create_app(Settings(env="prod", docs_enabled=False, jwt_secret="s" * 40)))
    assert c.get("/docs").status_code == 404
    assert c.get("/openapi.json").status_code == 404


def test_ready_reports_failed_dependencies(app: FastAPI) -> None:
    def broken() -> None:
        raise ConnectionError("down")

    app.state.ready_checks["redis"] = broken
    r = TestClient(app).get("/health/ready")
    assert r.status_code == 503
    assert r.json() == {"status": "unavailable", "failed": ["redis"]}


@pytest.mark.parametrize("secret", ["", "short", "x" * 31])
def test_prod_requires_strong_jwt_secret(secret: str) -> None:
    with pytest.raises(RuntimeError):
        create_app(Settings(env="prod", jwt_secret=secret))
