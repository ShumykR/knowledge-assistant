"""S3 (MinIO): сховище, presigned POST з лімітом і повний потік через API.

Запуск: KA_TEST_S3_ENDPOINT=http://127.0.0.1:9000 pytest -m integration
"""

import os
import uuid

import boto3
import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.storage import S3Storage, TooLarge, new_key
from tests.conftest import PASSWORD

ENDPOINT = os.environ.get("KA_TEST_S3_ENDPOINT", "")
ACCESS, SECRET = "ka-dev", "ka-dev-only-secret"
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not ENDPOINT, reason="KA_TEST_S3_ENDPOINT не задано"),
]


@pytest.fixture
def bucket() -> str:
    name = f"ka-test-{uuid.uuid4().hex[:12]}"
    client = boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        aws_access_key_id=ACCESS,
        aws_secret_access_key=SECRET,
        region_name="us-east-1",
    )
    client.create_bucket(Bucket=name)
    return name


def storage(bucket: str) -> S3Storage:
    client = boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        aws_access_key_id=ACCESS,
        aws_secret_access_key=SECRET,
        region_name="us-east-1",
    )
    return S3Storage(client, bucket)


def test_roundtrip_and_limits(bucket: str) -> None:
    st = storage(bucket)
    key = new_key()
    up = st.begin_upload(key, 100)
    up.write(b"hello ")
    up.write(b"world")
    assert up.finish() == 11
    assert st.size(key) == 11 and st.read_head(key, 5) == b"hello"
    assert st.read_all(key, 100) == b"hello world"
    with pytest.raises(TooLarge):
        st.read_all(key, 5)
    st.delete(key)
    assert st.size(key) is None


def test_upload_over_limit_stores_nothing(bucket: str) -> None:
    st = storage(bucket)
    key = new_key()
    up = st.begin_upload(key, 5)
    with pytest.raises(TooLarge):
        up.write(b"123456")
    up.abort()
    assert st.size(key) is None


def test_presigned_post_enforces_size_limit(bucket: str) -> None:
    st = storage(bucket)
    key = new_key()
    post = st.presigned_upload(key, 100, 60)
    ok = httpx.post(post["url"], data=post["fields"], files={"file": ("x", b"a" * 50)})
    assert ok.status_code in (200, 201, 204)
    assert st.size(key) == 50
    key2 = new_key()
    post2 = st.presigned_upload(key2, 100, 60)
    big = httpx.post(post2["url"], data=post2["fields"], files={"file": ("x", b"a" * 500)})
    assert big.status_code == 400  # S3 сам відхиляє: content-length-range
    assert st.size(key2) is None


def test_direct_upload_flow_through_api(bucket: str) -> None:
    settings = Settings(
        jwt_secret="s" * 40,
        storage_backend="s3",
        s3_bucket=bucket,
        s3_endpoint_url=ENDPOINT,
        s3_region="us-east-1",
        s3_access_key=ACCESS,
        s3_secret_key=SECRET,
        max_upload_bytes=1000,
    )
    client = TestClient(create_app(settings), raise_server_exceptions=False)
    body = {"email": "a@x.io", "password": PASSWORD}
    client.post("/api/v1/auth/register", json=body)
    token = client.post("/api/v1/auth/login", json=body).json()["access_token"]
    h = {"Authorization": f"Bearer {token}"}
    doc_id = client.post("/api/v1/documents", json={"title": "t"}, headers=h).json()["id"]

    # файл ще не завантажено
    assert client.post(f"/api/v1/documents/{doc_id}/complete", headers=h).status_code == 409
    r = client.post(f"/api/v1/documents/{doc_id}/upload-url", json={"filename": "n.txt"}, headers=h)
    assert r.status_code == 200
    post = r.json()
    httpx.post(
        post["url"], data=post["fields"], files={"file": ("n.txt", b"Direct upload to S3 works.")}
    )
    done = client.post(f"/api/v1/documents/{doc_id}/complete", headers=h)
    assert done.status_code == 202 and done.json()["status"] == "indexed"
    assert done.json()["content_type"] == "text/plain" and done.json()["filename"] == "n.txt"


def test_complete_rejects_binary_and_removes_it(bucket: str) -> None:
    settings = Settings(
        jwt_secret="s" * 40,
        storage_backend="s3",
        s3_bucket=bucket,
        s3_endpoint_url=ENDPOINT,
        s3_region="us-east-1",
        s3_access_key=ACCESS,
        s3_secret_key=SECRET,
    )
    client = TestClient(create_app(settings), raise_server_exceptions=False)
    body = {"email": "b@x.io", "password": PASSWORD}
    client.post("/api/v1/auth/register", json=body)
    token = client.post("/api/v1/auth/login", json=body).json()["access_token"]
    h = {"Authorization": f"Bearer {token}"}
    doc_id = client.post("/api/v1/documents", json={"title": "t"}, headers=h).json()["id"]
    post = client.post(
        f"/api/v1/documents/{doc_id}/upload-url", json={"filename": "evil.txt"}, headers=h
    ).json()
    httpx.post(
        post["url"],
        data=post["fields"],
        files={"file": ("evil.txt", b"MZ\x90\x00\x03" + b"\x00" * 50)},
    )
    assert client.post(f"/api/v1/documents/{doc_id}/complete", headers=h).status_code == 415
    assert storage(bucket).size(post["fields"]["key"]) is None
