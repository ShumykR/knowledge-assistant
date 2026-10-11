"""Перевірка завантажених файлів: тип за вмістом, безпечне імʼя (CWE-434, CWE-22).

Заявленому клієнтом `Content-Type` і розширенню файлу не довіряємо: тип визначається за першими
байтами. У сховище файл потрапляє під випадковим ключем, а оригінальне імʼя зберігається лише
як очищений рядок для показу.
"""

import re
import unicodedata

ALLOWED = {"application/pdf", "text/plain"}
SNIFF_BYTES = 8192
_WINDOWS_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


_CONTROL = re.compile(rb"[\x00-\x08\x0b\x0e-\x1f\x7f]")  # дозволені лише \t \n \f \r


class UnsupportedFile(Exception):
    pass


def sniff(head: bytes) -> str:
    """Тип за вмістом. Підтримуються PDF і текст (UTF-8, включно з Markdown)."""
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if not head:
        raise UnsupportedFile("empty file")
    if _CONTROL.search(head):  # керувальні байти бувають у виконуваних файлах, не в тексті
        raise UnsupportedFile("binary content")
    try:
        # обрізане посередині багатобайтове слово наприкінці вибірки це не помилка
        head.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        if exc.start < len(head) - 3:
            raise UnsupportedFile("not valid UTF-8 text") from exc
    return "text/plain"


def safe_filename(name: str, max_len: int = 100) -> str:
    """Імʼя для показу: без шляху, керувальних і небезпечних символів. Ніколи не порожнє."""
    name = unicodedata.normalize("NFKC", name)
    name = re.split(r"[/\\]", name)[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    name = re.sub(r"[^\w.\- ]", "_", name, flags=re.UNICODE).strip(" .")
    stem = name.split(".")[0].lower()
    if not name or stem in _WINDOWS_RESERVED:
        return "file"
    if len(name) > max_len:
        base, dot, ext = name.rpartition(".")
        ext = ext[:10]
        name = (base[: max_len - len(ext) - 1] + "." + ext) if dot and base else name[:max_len]
    return name
