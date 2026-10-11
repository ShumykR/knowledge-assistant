"""Мінімальний інтерфейс key-value сховища (його задовольняє клієнт Redis) і версія в памʼяті.

Кеш, ліміти запитів та ідемпотентність залежать лише від цього інтерфейсу: у production це Redis,
у dev і в швидких тестах `MemoryKV` з керованим годинником.
"""

import time
from collections.abc import Callable
from typing import Any, Protocol


class KV(Protocol):
    def get(self, name: str) -> Any: ...
    def set(self, name: str, value: str, *, ex: int | None = None, nx: bool = False) -> Any: ...
    def delete(self, *names: str) -> Any: ...
    def incr(self, name: str) -> int: ...
    def expire(self, name: str, seconds: int) -> Any: ...


class MemoryKV:
    """Один процес, без потоків-гонок поза GIL. Для багатьох інстансів потрібен Redis."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._data: dict[str, tuple[str, float | None]] = {}

    def _alive(self, name: str) -> tuple[str, float | None] | None:
        item = self._data.get(name)
        if item is None:
            return None
        if item[1] is not None and item[1] <= self._clock():
            del self._data[name]
            return None
        return item

    def get(self, name: str) -> str | None:
        item = self._alive(name)
        return None if item is None else item[0]

    def set(self, name: str, value: str, *, ex: int | None = None, nx: bool = False) -> bool:
        if nx and self._alive(name) is not None:
            return False
        self._data[name] = (value, None if ex is None else self._clock() + ex)
        return True

    def delete(self, *names: str) -> int:
        return sum(self._data.pop(n, None) is not None for n in names)

    def incr(self, name: str) -> int:
        item = self._alive(name)
        n = int(item[0]) + 1 if item else 1
        self._data[name] = (str(n), item[1] if item else None)
        return n

    def expire(self, name: str, seconds: int) -> bool:
        item = self._alive(name)
        if item is None:
            return False
        self._data[name] = (item[0], self._clock() + seconds)
        return True
