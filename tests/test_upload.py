from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import Settings
from app.main import create_app
from app.models import Chunk, DeadLetter, OutboxEvent
from tests.conftest import Clock, signup
from tests.test_ingest import make_pdf


@pytest.fixture
def storage_dir(tmp_path: Path) -> Path:
    return tmp_path / "uploads"


@pytest.fixture
def app(clock: Clock, storage_dir: Path) -> FastAPI:
    settings = Settings(jwt_secret="s" * 40, storage_dir=str(storage_dir), max_upload_bytes=2000)
    return create_app(settings, clock=clock)


def new_doc(client: TestClient, headers: dict[str, str]) -> int:
    r = client.post("/api/v1/documents", json={"title": "Звіт"}, headers=headers)
    assert r.status_code == 201
    return int(r.json()["id"])


def put(
    client: TestClient, headers: dict[str, str], doc_id: int, data: object, name: str = "a.txt"
) -> httpx.Response:
    resp: httpx.Response = client.put(
        f"/api/v1/documents/{doc_id}/file", params={"filename": name}, content=data, headers=headers
    )
    return resp


def stored_files(storage_dir: Path) -> list[str]:
    return sorted(p.name for p in storage_dir.iterdir()) if storage_dir.exists() else []


def count(app: FastAPI, model: type) -> int:
    with app.state.session_factory() as s:
        return int(s.scalar(select(func.count()).select_from(model)) or 0)


TEXT = ("Резервні копії бази даних треба перевіряти відновленням. " * 5).encode()


def test_upload_text_is_indexed(app: FastAPI, client: TestClient, storage_dir: Path) -> None:
    h = signup(client, "a@x.io")
    doc_id = new_doc(client, h)
    r = put(client, h, doc_id, TEXT, "notes.txt")
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "indexed" and body["filename"] == "notes.txt"
    assert body["content_type"] == "text/plain" and body["size_bytes"] == len(TEXT)
    assert count(app, Chunk) >= 1 and count(app, OutboxEvent) == 1
    assert client.get(f"/api/v1/documents/{doc_id}", headers=h).json()["status"] == "indexed"


def test_uploaded_file_is_stored_under_random_key_only(
    client: TestClient, storage_dir: Path
) -> None:
    h = signup(client, "a@x.io")
    put(client, h, new_doc(client, h), TEXT, "../../etc/passwd")
    files = stored_files(storage_dir)
    assert len(files) == 1 and len(files[0]) == 32 and files[0] != "passwd"
    assert not (storage_dir.parent / "etc").exists()


def test_filename_is_sanitized_for_display(client: TestClient) -> None:
    h = signup(client, "a@x.io")
    r = put(client, h, new_doc(client, h), TEXT, "..\\..\\windows\\evil<>.txt")
    assert r.json()["filename"] == "evil__.txt"


def test_pdf_is_detected_and_indexed(client: TestClient) -> None:
    h = signup(client, "a@x.io")
    r = put(client, h, new_doc(client, h), make_pdf("Hello PDF world"), "doc.pdf")
    assert r.json()["content_type"] == "application/pdf" and r.json()["status"] == "indexed"


def test_type_comes_from_content_not_from_name_or_header(
    client: TestClient, storage_dir: Path
) -> None:
    h = signup(client, "a@x.io")
    exe = b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 200
    r = client.put(
        f"/api/v1/documents/{new_doc(client, h)}/file",
        params={"filename": "innocent.txt"},
        content=exe,
        headers={**h, "Content-Type": "text/plain"},
    )
    assert r.status_code == 415
    assert stored_files(storage_dir) == []


def test_empty_file_is_rejected(client: TestClient) -> None:
    h = signup(client, "a@x.io")
    assert put(client, h, new_doc(client, h), b"").status_code == 415


def test_too_large_by_content_length(client: TestClient, storage_dir: Path) -> None:
    h = signup(client, "a@x.io")
    r = put(client, h, new_doc(client, h), b"a" * 2001)
    assert r.status_code == 413
    assert stored_files(storage_dir) == []


def test_too_large_while_streaming_without_content_length(
    client: TestClient, storage_dir: Path
) -> None:
    h = signup(client, "a@x.io")

    def chunks() -> Iterator[bytes]:
        for _ in range(10):
            yield b"a" * 500  # 5000 байт, ліміт 2000

    r = put(client, h, new_doc(client, h), chunks())
    assert r.status_code == 413
    assert stored_files(storage_dir) == []  # недописаний файл прибрано


def test_exactly_at_limit_is_ok(client: TestClient) -> None:
    h = signup(client, "a@x.io")
    assert put(client, h, new_doc(client, h), b"a" * 2000).status_code == 202


def test_rejected_upload_can_be_retried(client: TestClient) -> None:
    h = signup(client, "a@x.io")
    doc_id = new_doc(client, h)
    assert put(client, h, doc_id, b"MZ\x00\x00").status_code == 415
    assert put(client, h, doc_id, TEXT).status_code == 202


def test_second_upload_is_conflict(client: TestClient) -> None:
    h = signup(client, "a@x.io")
    doc_id = new_doc(client, h)
    put(client, h, doc_id, TEXT)
    assert put(client, h, doc_id, TEXT).status_code == 409


def test_cannot_upload_to_foreign_or_missing_document(
    client: TestClient, storage_dir: Path
) -> None:
    alice, bob = signup(client, "a@x.io"), signup(client, "b@x.io")
    doc_id = new_doc(client, alice)
    assert put(client, bob, doc_id, TEXT).status_code == 404
    assert put(client, bob, 9999, TEXT).status_code == 404
    assert stored_files(storage_dir) == []


def test_filename_param_is_required(client: TestClient) -> None:
    h = signup(client, "a@x.io")
    r = client.put(f"/api/v1/documents/{new_doc(client, h)}/file", content=TEXT, headers=h)
    assert r.status_code == 422


def test_corrupt_pdf_ends_as_failed_with_dead_letter(app: FastAPI, client: TestClient) -> None:
    h = signup(client, "a@x.io")
    doc_id = new_doc(client, h)
    r = put(client, h, doc_id, b"%PDF-1.4 definitely not a pdf" * 5, "bad.pdf")
    assert r.status_code == 202 and r.json()["status"] == "failed"
    assert r.json()["error"] == "cannot read PDF"
    assert count(app, DeadLetter) == 1 and count(app, Chunk) == 0


def test_queue_failure_rolls_back_and_allows_retry(
    app: FastAPI, client: TestClient, storage_dir: Path
) -> None:
    h = signup(client, "a@x.io")
    doc_id = new_doc(client, h)

    class Down:
        async def enqueue_ingest(self, document_id: int) -> None:
            raise ConnectionError("redis is down")

    good_queue = app.state.queue
    app.state.queue = Down()
    r = put(client, h, doc_id, TEXT)
    assert r.status_code == 503 and r.headers["content-type"].startswith("application/problem+json")
    assert stored_files(storage_dir) == []
    assert client.get(f"/api/v1/documents/{doc_id}", headers=h).json()["status"] == "created"

    app.state.queue = good_queue
    assert put(client, h, doc_id, TEXT).status_code == 202


def test_presigned_upload_not_available_with_local_storage(client: TestClient) -> None:
    h = signup(client, "a@x.io")
    r = client.post(
        f"/api/v1/documents/{new_doc(client, h)}/upload-url", json={"filename": "a.txt"}, headers=h
    )
    assert r.status_code == 400


def test_complete_without_pending_upload_is_conflict(client: TestClient) -> None:
    h = signup(client, "a@x.io")
    r = client.post(f"/api/v1/documents/{new_doc(client, h)}/complete", headers=h)
    assert r.status_code == 409


def test_upload_requires_authentication(client: TestClient) -> None:
    r = client.put("/api/v1/documents/1/file", params={"filename": "a.txt"}, content=TEXT)
    assert r.status_code == 401
