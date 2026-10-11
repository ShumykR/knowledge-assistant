import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.idempotency import IdempotencyConflict, Stored, run_once
from app.kv import MemoryKV
from app.logging_utils import PiiFilter, mask
from app.ratelimit import RateLimiter
from tests.conftest import Clock

# ---------- MemoryKV ----------


def test_kv_set_nx_and_expiry(clock: Clock) -> None:
    kv = MemoryKV(clock)
    assert kv.set("k", "1", ex=10, nx=True) is True
    assert kv.set("k", "2", ex=10, nx=True) is False
    assert kv.get("k") == "1"
    clock.advance(10)
    assert kv.get("k") is None
    assert kv.set("k", "3", nx=True) is True


def test_kv_incr_expire_delete(clock: Clock) -> None:
    kv = MemoryKV(clock)
    assert [kv.incr("c"), kv.incr("c"), kv.incr("c")] == [1, 2, 3]
    kv.expire("c", 5)
    clock.advance(5)
    assert kv.incr("c") == 1
    assert kv.delete("c", "missing") == 1


# ---------- rate limiter ----------


def test_limiter_blocks_over_limit_and_resets(clock: Clock) -> None:
    clock.now = 1000.0  # початок вікна, кратний 60 (1000 // 60 = 16, межа 1020)
    limiter = RateLimiter(MemoryKV(clock), clock)
    results = [limiter.hit("s", "u", limit=2, window=60) for _ in range(3)]
    assert [r.allowed for r in results] == [True, True, False]
    assert results[2].retry_after == 20
    clock.advance(20)
    assert limiter.hit("s", "u", limit=2, window=60).allowed


def test_limiter_scopes_and_subjects_are_separate(clock: Clock) -> None:
    limiter = RateLimiter(MemoryKV(clock), clock)
    limiter.hit("a", "u", limit=1, window=60)
    assert limiter.hit("a", "other", limit=1, window=60).allowed
    assert limiter.hit("b", "u", limit=1, window=60).allowed
    assert not limiter.hit("a", "u", limit=1, window=60).allowed


def test_limiter_fails_open_when_store_is_down(clock: Clock) -> None:
    class Dead(MemoryKV):
        def incr(self, name: str) -> int:
            raise ConnectionError

    assert RateLimiter(Dead(clock), clock).hit("s", "u", limit=1, window=60).allowed


# ---------- idempotency (unit) ----------


def test_run_once_does_not_store_failures(clock: Clock) -> None:
    kv = MemoryKV(clock)
    calls: list[int] = []

    def flaky() -> Stored:
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("boom")
        return Stored(201, {"ok": True})

    with pytest.raises(ValueError):
        run_once(kv, user_id=1, key="k", payload={}, execute=flaky)
    result, replayed = run_once(kv, user_id=1, key="k", payload={}, execute=flaky)
    assert (result.status, replayed, len(calls)) == (201, False, 2)


def test_run_once_in_progress_is_conflict(clock: Clock) -> None:
    kv = MemoryKV(clock)

    def nested() -> Stored:
        with pytest.raises(IdempotencyConflict):  # той самий ключ, поки перший ще виконується
            run_once(kv, user_id=1, key="k", payload={"a": 1}, execute=lambda: Stored(200, {}))
        return Stored(201, {})

    run_once(kv, user_id=1, key="k", payload={"a": 1}, execute=nested)


# ---------- маскування в логах ----------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("user alice@example.com logged in", "user a***@example.com logged in"),
        ("Authorization: Bearer abc.def-ghi_123", "Authorization: Bearer ***"),
        ("password=hunter2 ok", "password=*** ok"),
        ('{"password": "hunter2", "email": "b@x.io"}', '{"password": "***", "email": "b***@x.io"}'),
        ("refresh_token: abcDEF123", "refresh_token: ***"),
        ("jwt eyJhbGciOi.eyJzdWIiOjF9.sig_part end", "jwt *** end"),
        ("nothing sensitive here", "nothing sensitive here"),
    ],
)
def test_mask(raw: str, expected: str) -> None:
    assert mask(raw) == expected


def test_filter_masks_formatted_message() -> None:
    record = logging.LogRecord(
        "x", logging.INFO, "f", 1, "login %s token=%s", ("a@b.io", "s3cr3t"), None
    )
    assert PiiFilter().filter(record) is True
    assert record.getMessage() == "login a***@b.io token=***"


def test_passwords_never_reach_logs(client: TestClient, caplog: Any) -> None:
    caplog.set_level(logging.DEBUG)
    client.post(
        "/api/v1/auth/register", json={"email": "leak@x.io", "password": "super-secret-pass"}
    )
    assert "super-secret-pass" not in caplog.text


# ---------- аудит маршрутів (за публічним OpenAPI, як у завданні №163 SkillBoost) ----------

PUBLIC = {
    ("get", "/health/live"),
    ("get", "/health/ready"),
    ("post", "/api/v1/auth/register"),
    ("post", "/api/v1/auth/login"),
    ("post", "/api/v1/auth/refresh"),
    ("post", "/api/v1/auth/logout"),
}


def test_every_route_is_protected_or_explicitly_public(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    unprotected = {
        (method, path)
        for path, ops in spec["paths"].items()
        for method, op in ops.items()
        if not op.get("security")
    }
    assert unprotected == PUBLIC, (
        f"маршрути без автентифікації поза списком: {unprotected - PUBLIC}"
    )


def test_public_list_has_no_stale_entries(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    existing = {(m, p) for p, ops in spec["paths"].items() for m in ops}
    assert existing >= PUBLIC


def test_protected_routes_reject_anonymous_requests(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            if op.get("security"):
                url = path.replace("{doc_id}", "1")
                r = client.request(method, url, json={} if method == "post" else None)
                assert r.status_code == 401, (method, path, r.status_code)
