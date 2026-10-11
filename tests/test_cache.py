from collections.abc import Callable

import fakeredis
import pytest

from app.cache import Cache


@pytest.fixture
def r() -> fakeredis.FakeRedis:
    return fakeredis.FakeRedis(decode_responses=True)


def cache(
    r: fakeredis.FakeRedis,
    sleep: Callable[[float], None] = lambda _s: None,
    **kw: float,
) -> Cache:
    return Cache(r, sleep=sleep, rand=lambda: 0.0, **kw)  # type: ignore[arg-type]


def test_computes_once_then_hits_cache(r: fakeredis.FakeRedis) -> None:
    calls: list[int] = []

    def compute() -> dict[str, int]:
        calls.append(1)
        return {"answer": 42}

    c = cache(r)
    assert c.get_or_compute("k", compute) == {"answer": 42}
    assert c.get_or_compute("k", compute) == {"answer": 42}
    assert len(calls) == 1


def test_ttl_is_set_with_jitter(r: fakeredis.FakeRedis) -> None:
    Cache(r, ttl=100, jitter=0.5, rand=lambda: 1.0).get_or_compute("k", lambda: 1)
    assert r.ttl("k") == 150


def test_lock_is_released_after_compute(r: fakeredis.FakeRedis) -> None:
    cache(r).get_or_compute("k", lambda: 1)
    assert r.get("lock:k") is None


def test_lock_released_when_compute_fails(r: fakeredis.FakeRedis) -> None:
    def boom() -> int:
        raise ValueError("x")

    with pytest.raises(ValueError):
        cache(r).get_or_compute("k", boom)
    assert r.get("lock:k") is None
    assert r.get("k") is None


def test_waiter_gets_value_computed_by_lock_owner(r: fakeredis.FakeRedis) -> None:
    r.set("lock:k", "1", ex=10)  # інший запит уже рахує
    sleeps: list[float] = []

    def sleep(step: float) -> None:
        sleeps.append(step)
        if len(sleeps) == 3:  # власник замка закінчив
            r.set("k", '"from-owner"')

    calls: list[int] = []

    def mine() -> str:
        calls.append(1)
        return "mine"

    got = cache(r, sleep=sleep).get_or_compute("k", mine)
    assert got == "from-owner"
    assert calls == [] and len(sleeps) == 3


def test_waiter_computes_itself_after_timeout(r: fakeredis.FakeRedis) -> None:
    r.set("lock:k", "1", ex=10)  # власник замка завис
    got = cache(r, wait_step=1.0, wait_max=3.0).get_or_compute("k", lambda: "mine")
    assert got == "mine"


def test_redis_down_degrades_to_compute() -> None:
    class Dead:
        def get(self, name: str) -> object:
            raise ConnectionError

        def set(self, *a: object, **k: object) -> object:
            raise ConnectionError

        def delete(self, *a: object) -> object:
            raise ConnectionError

    assert Cache(Dead()).get_or_compute("k", lambda: 7) == 7


def test_values_roundtrip_json(r: fakeredis.FakeRedis) -> None:
    c = cache(r)
    c.get_or_compute("k", lambda: {"a": [1, 2], "b": "ї"})
    assert c.get_or_compute("k", lambda: "other") == {"a": [1, 2], "b": "ї"}
