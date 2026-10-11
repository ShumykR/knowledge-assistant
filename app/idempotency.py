"""Idempotency-Key для небезпечних до повтору POST (клієнт натиснув двічі, мережа повторила запит).

Схема: перший запит захоплює ключ (`SET NX`), виконується й зберігає відповідь; повтор із тим самим
ключем і тим самим тілом віддає збережену відповідь БЕЗ повторного виконання; той самий ключ із
іншим тілом це помилка клієнта; ключ, що ще виконується, дає 409.
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.errors import DomainError
from app.kv import KV

TTL = 24 * 3600


class IdempotencyConflict(DomainError):
    status = 409
    title = "Conflict"


class IdempotencyMismatch(DomainError):
    status = 422
    title = "Idempotency key reused"


@dataclass(frozen=True)
class Stored:
    status: int
    body: Any


def fingerprint(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()


def run_once(
    kv: KV,
    *,
    user_id: int,
    key: str,
    payload: Any,
    execute: Callable[[], Stored],
    in_progress_ttl: int = 60,
) -> tuple[Stored, bool]:
    """Повертає (відповідь, replayed). `replayed=True` означає, що `execute` не викликався."""
    slot = f"idem:{user_id}:{key}"  # ключ належить користувачу: чужий ключ не зачепити
    fp = fingerprint(payload)
    if kv.set(slot, json.dumps({"fp": fp, "done": False}), ex=in_progress_ttl, nx=True):
        try:
            result = execute()
        except Exception:
            kv.delete(slot)  # невдалий запит можна повторити з тим самим ключем
            raise
        kv.set(
            slot,
            json.dumps({"fp": fp, "done": True, "status": result.status, "body": result.body}),
            ex=TTL,
        )
        return result, False
    raw = kv.get(slot)
    if raw is None:  # замок щойно спливнув: просимо повторити
        raise IdempotencyConflict("Request is being processed, retry")
    saved = json.loads(raw)
    if saved["fp"] != fp:
        raise IdempotencyMismatch("Idempotency-Key was already used with a different request")
    if not saved["done"]:
        raise IdempotencyConflict("Request is being processed, retry")
    return Stored(saved["status"], saved["body"]), True
