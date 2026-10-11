"""Маскування PII і секретів у логах (CWE-532): фільтр застосовується до всіх записів."""

import logging
import re

_EMAIL = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*")
_SECRET_KV = re.compile(
    r"""(?ix)\b(password|passwd|secret|token|refresh_token|api[_-]?key)(["']?\s*[:=]\s*["']?)[^\s,;"'&}]+"""
)


def mask(text: str) -> str:
    text = _BEARER.sub("Bearer ***", text)
    text = _JWT.sub("***", text)
    text = _SECRET_KV.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)
    return _EMAIL.sub(lambda m: f"{m.group(1)}***@{m.group(2)}", text)


class PiiFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = mask(record.getMessage())
        record.args = None
        return True


def install_pii_filter() -> None:
    """Фільтр вішається на handler'и кореневого логера (фільтри логера не діють на дочірні)."""
    root = logging.getLogger()
    for handler in root.handlers:
        if not any(isinstance(f, PiiFilter) for f in handler.filters):
            handler.addFilter(PiiFilter())
