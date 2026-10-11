"""Захист від cache stampede на справжньому Redis: багато потоків, `compute` викликається один раз.

Запуск: KA_TEST_REDIS_URL=redis://127.0.0.1:6379/0 pytest -m integration
"""

import os
import threading
import time
import uuid

import pytest
import redis

from app.cache import Cache

URL = os.environ.get("KA_TEST_REDIS_URL", "")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not URL, reason="KA_TEST_REDIS_URL не задано"),
]


def test_parallel_requests_compute_once() -> None:
    client = redis.Redis.from_url(URL, decode_responses=True)
    key = f"test:{uuid.uuid4()}"
    calls: list[int] = []
    results: list[object] = []

    def compute() -> dict[str, int]:
        calls.append(1)
        time.sleep(0.3)  # «дорогий» запит
        return {"n": 1}

    def worker() -> None:
        results.append(Cache(client, ttl=30).get_or_compute(key, compute))

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    client.delete(key)
    assert len(calls) == 1
    assert results == [{"n": 1}] * 20
