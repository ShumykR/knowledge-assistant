"""Rate limiter з фіксованим вікном на KV (Redis).

Ключ містить номер вікна, тож лічильники самі спливають. Підходить для захисту від перебору
паролів (CWE-307) і від надмірних запитів. Межа: на стику вікон можливий сплеск до 2x ліміту;
якщо це критично, переходь на sliding window (скрипт Lua або sorted set).
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from app.kv import KV

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    retry_after: int  # секунд до нового вікна (0, якщо дозволено)


class RateLimiter:
    def __init__(self, kv: KV, clock: Callable[[], float] = time.time) -> None:
        self._kv = kv
        self._clock = clock

    def hit(self, scope: str, subject: str, *, limit: int, window: int) -> Decision:
        """Реєструє спробу. `scope`: що обмежуємо, `subject`: кого (ip+email, id користувача)."""
        now = self._clock()
        slot = int(now // window)
        key = f"rl:{scope}:{subject}:{slot}"
        try:
            count = self._kv.incr(key)
            if count == 1:
                self._kv.expire(key, window)
        except Exception:
            # fail-open: збій Redis не має валити весь API; для логіну можна обрати fail-closed
            log.exception("rate limiter unavailable, allowing request")
            return Decision(True, 0)
        if count > limit:
            return Decision(False, max(1, int((slot + 1) * window - now)))
        return Decision(True, 0)
