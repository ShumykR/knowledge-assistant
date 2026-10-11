import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import security
from app.config import Settings
from app.main import create_app
from app.security import hash_password
from tests.conftest import PASSWORD, Clock, login, register, signup

DAY = 24 * 3600


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def refresh(client: TestClient, token: str) -> tuple[int, dict[str, str]]:
    r = client.post("/api/v1/auth/refresh", json={"refresh_token": token})
    return r.status_code, r.json()


# ---------- реєстрація ----------


def test_register_returns_user_without_secrets(client: TestClient) -> None:
    r = client.post("/api/v1/auth/register", json={"email": " Alice@X.io ", "password": PASSWORD})
    assert r.status_code == 201
    assert r.json() == {"id": 1, "email": "alice@x.io", "role": "user"}
    assert "password" not in r.text


def test_register_duplicate_email_is_conflict(client: TestClient) -> None:
    register(client, "alice@x.io")
    r = client.post("/api/v1/auth/register", json={"email": "ALICE@x.io", "password": PASSWORD})
    assert r.status_code == 409


@pytest.mark.parametrize("password", ["short", "x" * 129])
def test_register_rejects_bad_password(client: TestClient, password: str) -> None:
    r = client.post("/api/v1/auth/register", json={"email": "a@x.io", "password": password})
    assert r.status_code == 400


def test_register_cannot_choose_role(client: TestClient) -> None:
    r = client.post(
        "/api/v1/auth/register", json={"email": "a@x.io", "password": PASSWORD, "role": "admin"}
    )
    assert r.status_code == 422


def test_register_invalid_email(client: TestClient) -> None:
    r = client.post("/api/v1/auth/register", json={"email": "nope", "password": PASSWORD})
    assert r.status_code == 422


# ---------- вхід ----------


def test_login_returns_tokens(client: TestClient) -> None:
    register(client, "a@x.io")
    tokens = login(client, "a@x.io")
    assert tokens["token_type"] == "bearer" and tokens["expires_in"] == 900
    assert tokens["access_token"] != tokens["refresh_token"]


def test_wrong_password_and_unknown_email_look_the_same(client: TestClient) -> None:
    register(client, "a@x.io")
    wrong = client.post(
        "/api/v1/auth/login", json={"email": "a@x.io", "password": "wrong-password"}
    )
    unknown = client.post(
        "/api/v1/auth/login", json={"email": "z@x.io", "password": "wrong-password"}
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


def test_login_rehashes_weak_password_hash(app: FastAPI, client: TestClient) -> None:
    users = app.state.auth._users
    users.create("old@x.io", hash_password(PASSWORD, n=2**9))
    assert security.needs_rehash(users.by_email("old@x.io").password_hash)
    login(client, "old@x.io")
    new_hash = users.by_email("old@x.io").password_hash
    assert not security.needs_rehash(new_hash) and security.verify_password(PASSWORD, new_hash)


# ---------- доступ ----------


def test_protected_requires_token(client: TestClient) -> None:
    r = client.get("/api/v1/me")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"
    assert r.headers["content-type"].startswith("application/problem+json")


@pytest.mark.parametrize("header", ["Bearer garbage", "Bearer ", "Basic abc", "garbage"])
def test_garbage_authorization_header(client: TestClient, header: str) -> None:
    assert client.get("/api/v1/me", headers={"Authorization": header}).status_code == 401


def test_me(client: TestClient) -> None:
    headers = signup(client, "a@x.io")
    assert client.get("/api/v1/me", headers=headers).json()["email"] == "a@x.io"


def test_access_token_expires(client: TestClient, clock: Clock) -> None:
    headers = signup(client, "a@x.io")
    clock.advance(899)
    assert client.get("/api/v1/me", headers=headers).status_code == 200
    clock.advance(1)
    assert client.get("/api/v1/me", headers=headers).status_code == 401


def test_refresh_token_is_not_an_access_token(client: TestClient) -> None:
    register(client, "a@x.io")
    tokens = login(client, "a@x.io")
    assert client.get("/api/v1/me", headers=bearer(tokens["refresh_token"])).status_code == 401


def test_admin_endpoint_roles(app: FastAPI, client: TestClient) -> None:
    user = signup(client, "user@x.io")
    app.state.auth._users.create("boss@x.io", hash_password(PASSWORD), role="admin")
    admin = {"Authorization": f"Bearer {login(client, 'boss@x.io')['access_token']}"}
    assert client.get("/api/v1/admin/users", headers=user).status_code == 403
    assert client.get("/api/v1/admin/users").status_code == 401
    r = client.get("/api/v1/admin/users", headers=admin)
    assert r.status_code == 200 and {u["email"] for u in r.json()} == {"user@x.io", "boss@x.io"}


# ---------- refresh: ротація, повторне використання, терміни ----------


def test_refresh_rotates_tokens(client: TestClient) -> None:
    register(client, "a@x.io")
    first = login(client, "a@x.io")
    status, second = refresh(client, first["refresh_token"])
    assert status == 200
    assert second["refresh_token"] != first["refresh_token"]
    assert client.get("/api/v1/me", headers=bearer(second["access_token"])).status_code == 200


def test_reusing_old_refresh_token_kills_the_whole_family(client: TestClient) -> None:
    register(client, "a@x.io")
    first = login(client, "a@x.io")
    _, second = refresh(client, first["refresh_token"])
    assert refresh(client, first["refresh_token"])[0] == 401  # старий токен: ознака крадіжки
    assert refresh(client, second["refresh_token"])[0] == 401  # законний наступник теж закритий


def test_two_logins_are_independent_families(client: TestClient) -> None:
    register(client, "a@x.io")
    laptop, phone = login(client, "a@x.io"), login(client, "a@x.io")
    refresh(client, laptop["refresh_token"])
    assert refresh(client, laptop["refresh_token"])[0] == 401
    assert refresh(client, phone["refresh_token"])[0] == 200


def test_unknown_refresh_token(client: TestClient) -> None:
    assert refresh(client, "nope")[0] == 401


def test_refresh_expires_after_idle_period(client: TestClient, clock: Clock) -> None:
    register(client, "a@x.io")
    tokens = login(client, "a@x.io")
    clock.advance(7 * DAY - 1)
    status, tokens = refresh(client, tokens["refresh_token"])
    assert status == 200
    clock.advance(7 * DAY)
    assert refresh(client, tokens["refresh_token"])[0] == 401


def test_sliding_window_is_capped_by_absolute_lifetime(client: TestClient, clock: Clock) -> None:
    register(client, "a@x.io")
    tokens = login(client, "a@x.io")
    for _ in range(4):  # 4 * 6 днів = 24 дні: ще в межах 30 днів
        clock.advance(6 * DAY)
        status, tokens = refresh(client, tokens["refresh_token"])
        assert status == 200
    clock.advance(6 * DAY)  # 30 днів від входу: абсолютна межа
    assert refresh(client, tokens["refresh_token"])[0] == 401


def test_logout_revokes_refresh(client: TestClient) -> None:
    register(client, "a@x.io")
    tokens = login(client, "a@x.io")
    r = client.post("/api/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 204
    assert refresh(client, tokens["refresh_token"])[0] == 401


def test_logout_with_unknown_token_is_quiet(client: TestClient) -> None:
    assert client.post("/api/v1/auth/logout", json={"refresh_token": "nope"}).status_code == 204


# ---------- rate limit ----------


def test_login_is_rate_limited_per_ip_and_email(client: TestClient, clock: Clock) -> None:
    register(client, "a@x.io")
    bad = {"email": "a@x.io", "password": "wrong-password"}
    codes = [client.post("/api/v1/auth/login", json=bad).status_code for _ in range(6)]
    assert codes == [401] * 5 + [429]
    r = client.post("/api/v1/auth/login", json=bad)
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    # навіть правильний пароль не пробиває обмеження, інакше перебір не стримується
    assert (
        client.post(
            "/api/v1/auth/login", json={"email": "a@x.io", "password": PASSWORD}
        ).status_code
        == 429
    )
    clock.advance(60)
    assert (
        client.post(
            "/api/v1/auth/login", json={"email": "a@x.io", "password": PASSWORD}
        ).status_code
        == 200
    )


def test_other_email_is_not_blocked(client: TestClient) -> None:
    register(client, "a@x.io")
    register(client, "b@x.io")
    for _ in range(6):
        client.post("/api/v1/auth/login", json={"email": "a@x.io", "password": "wrong-password"})
    assert (
        client.post(
            "/api/v1/auth/login", json={"email": "b@x.io", "password": PASSWORD}
        ).status_code
        == 200
    )


def test_distributed_guessing_of_one_email_is_limited(clock: Clock) -> None:
    app = create_app(
        Settings(jwt_secret="s" * 40, login_limit=100, login_email_limit=3), clock=clock
    )
    ips = [TestClient(app, client=(f"10.0.0.{i}", 5000)) for i in range(1, 6)]
    bad = {"email": "a@x.io", "password": "wrong-password"}
    codes = [c.post("/api/v1/auth/login", json=bad).status_code for c in ips]
    assert codes == [401, 401, 401, 429, 429]


def test_api_is_rate_limited_per_user(clock: Clock) -> None:
    app = create_app(Settings(jwt_secret="s" * 40, api_limit=3), clock=clock)
    client = TestClient(app, raise_server_exceptions=False)
    a, b = signup(client, "a@x.io"), signup(client, "b@x.io")
    assert [client.get("/api/v1/me", headers=a).status_code for _ in range(4)] == [
        200,
        200,
        200,
        429,
    ]
    assert client.get("/api/v1/me", headers=b).status_code == 200
    clock.advance(60)
    assert client.get("/api/v1/me", headers=a).status_code == 200


# ---------- idempotency ----------


def post_doc(client: TestClient, headers: dict[str, str], key: str | None, title: str = "Звіт"):  # type: ignore[no-untyped-def]
    h = dict(headers)
    if key:
        h["Idempotency-Key"] = key
    return client.post("/api/v1/documents", json={"title": title}, headers=h)


def test_same_key_creates_one_document(client: TestClient) -> None:
    h = signup(client, "a@x.io")
    first = post_doc(client, h, "key-1")
    second = post_doc(client, h, "key-1")
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert "idempotency-replayed" not in first.headers
    assert second.headers["idempotency-replayed"] == "true"
    assert len(client.get("/api/v1/documents", headers=h).json()) == 1


def test_without_key_each_post_creates_a_document(client: TestClient) -> None:
    h = signup(client, "a@x.io")
    post_doc(client, h, None)
    post_doc(client, h, None)
    assert len(client.get("/api/v1/documents", headers=h).json()) == 2


def test_same_key_other_body_is_rejected(client: TestClient) -> None:
    h = signup(client, "a@x.io")
    post_doc(client, h, "key-1", "A")
    r = post_doc(client, h, "key-1", "B")
    assert r.status_code == 422
    assert len(client.get("/api/v1/documents", headers=h).json()) == 1


def test_key_is_scoped_to_user(client: TestClient) -> None:
    a, b = signup(client, "a@x.io"), signup(client, "b@x.io")
    ra, rb = post_doc(client, a, "shared"), post_doc(client, b, "shared")
    assert ra.status_code == rb.status_code == 201
    assert ra.json()["id"] != rb.json()["id"]
    assert len(client.get("/api/v1/documents", headers=b).json()) == 1


@pytest.mark.parametrize("key", ["a b", "x" * 129, "a;b", "a/b"])
def test_invalid_idempotency_key(client: TestClient, key: str) -> None:
    h = signup(client, "a@x.io")
    assert post_doc(client, h, key).status_code == 400
