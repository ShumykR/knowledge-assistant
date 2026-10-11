"""Кеш відповідей у Redis із захистом від cache stampede.

Коли популярний ключ спливає, сотні запитів одночасно йдуть рахувати те саме. Тут:
- на ключ береться короткий замок (`SET NX EX`): рахує один, решта чекають результат;
- TTL має випадковий розкид (jitter), щоб ключі не спливали одночасно;
- якщо власник замка помер, замок спливає сам (EX), і чекачі беруть його по черзі;
- збій Redis не валить запит: просто рахуємо без кешу.
"""

import json
import logging
import random
import time
from collections.abc import Callable
from typing import Any, Protocol

log = logging.getLogger(__name__)
DOWN = object()  # маркер «Redis недоступний»


class RedisLike(Protocol):
    def get(self, name: str) -> Any: ...
    def set(self, name: str, value: str, *, ex: int | None = None, nx: bool = False) -> Any: ...
    def delete(self, *names: str) -> Any: ...


class Cache:
    def __init__(
        self,
        redis: RedisLike,
        *,
        ttl: int = 300,
        jitter: float = 0.1,
        lock_ttl: int = 10,
        wait_step: float = 0.05,
        wait_max: float = 5.0,
        sleep: Callable[[float], None] = time.sleep,
        rand: Callable[[], float] = random.random,
    ) -> None:
        self._r = redis
        self._ttl = ttl
        self._jitter = jitter
        self._lock_ttl = lock_ttl
        self._step = wait_step
        self._max_steps = int(wait_max / wait_step)
        self._sleep = sleep
        self._rand = rand

    def _ttl_with_jitter(self) -> int:
        return max(1, int(self._ttl * (1 + self._jitter * self._rand())))

    def _read(self, key: str) -> Any | None:
        raw = self._r.get(key)
        return None if raw is None else json.loads(raw)

    def _redis(self, op: Callable[[], Any]) -> Any:
        """Виконує операцію Redis; при збої повертає DOWN, щоб викликач міг деградувати."""
        try:
            return op()
        except Exception:
            log.exception("cache unavailable, computing without it")
            return DOWN

    def get_or_compute(self, key: str, compute: Callable[[], Any]) -> Any:
        """Повертає значення з кешу або рахує його один раз на всі паралельні запити.

        `None` у кеш не кладеться (відрізнити від «немає»), помилки `compute` йдуть нагору.
        """
        hit = self._redis(lambda: self._read(key))
        if hit is DOWN:
            return compute()
        if hit is not None:
            return hit
        lock = f"lock:{key}"
        got = self._redis(lambda: self._r.set(lock, "1", ex=self._lock_ttl, nx=True))
        if got is DOWN:
            return compute()
        if got:
            try:
                value = compute()
                self._redis(lambda: self._r.set(key, json.dumps(value), ex=self._ttl_with_jitter()))
                return value
            finally:
                self._redis(lambda: self._r.delete(lock))
        for _ in range(self._max_steps):  # хтось інший рахує: чекаємо результат
            self._sleep(self._step)
            hit = self._redis(lambda: self._read(key))
            if hit is DOWN:
                break
            if hit is not None:
                return hit
        else:
            log.warning("cache wait timed out for %s, computing locally", key)
        return compute()
