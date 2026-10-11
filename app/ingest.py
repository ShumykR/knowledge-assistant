"""Інгест документа: текст -> чанки -> embeddings -> запис у БД. Виконується фоновим воркером."""

import hashlib
import io
import logging
import math
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx
from pypdf import PdfReader
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import EMBEDDING_DIM, Chunk, DeadLetter, OutboxEvent
from app.models import Document as DocumentRow
from app.storage import Storage

log = logging.getLogger(__name__)

MAX_PDF_PAGES = 300
MAX_TEXT_CHARS = 2_000_000  # захист від «бомб»: дуже великий текст з малого файлу


class PermanentError(Exception):
    """Повтор не допоможе (битий файл, немає тексту): одразу в dead letters, без ретраїв."""


# --- текст і чанки --------------------------------------------------------------------------
def extract_text(data: bytes, content_type: str) -> str:
    if content_type == "text/plain":
        text = data.decode("utf-8", errors="replace")
    elif content_type == "application/pdf":
        try:
            reader = PdfReader(io.BytesIO(data))
            if len(reader.pages) > MAX_PDF_PAGES:
                raise PermanentError("too many pages")
            text = "\n\n".join((page.extract_text() or "") for page in reader.pages)
        except PermanentError:
            raise
        except Exception as exc:
            raise PermanentError("cannot read PDF") from exc
    else:
        raise PermanentError(f"unsupported type {content_type}")
    if len(text) > MAX_TEXT_CHARS:
        raise PermanentError("text is too large")
    return text


def chunk_text(text: str, max_chars: int = 1000, overlap: int = 100) -> list[str]:
    """Абзаци пакуються в чанки до `max_chars`; задовгий абзац ріжеться з перекриттям `overlap`."""
    if max_chars < 1 or not 0 <= overlap < max_chars:
        raise ValueError("need 0 <= overlap < max_chars")
    paragraphs = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", text)]
    pieces: list[str] = []
    for para in filter(None, paragraphs):
        if len(para) <= max_chars:
            pieces.append(para)
            continue
        step = max_chars - overlap
        pieces.extend(para[i : i + max_chars] for i in range(0, len(para), step) if para[i:])
        # хвіст, що повністю лежить у попередньому вікні, не потрібен
        if len(pieces) >= 2 and len(pieces[-1]) <= overlap:
            pieces.pop()
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if not current:
            current = piece
        elif len(current) + 1 + len(piece) <= max_chars:
            current += "\n" + piece
        else:
            chunks.append(current)
            current = piece
    if current:
        chunks.append(current)
    return chunks


# --- embeddings -----------------------------------------------------------------------------
class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashEmbedder:
    """Детермінований «мішок слів» через хешування: без мережі, схожі тексти дають близькі вектори.

    Це не семантика, а лексичний збіг: годиться для dev і тестів, у production береться модель.
    """

    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        self._dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        for word in re.findall(r"\w+", text.lower()):
            h = int.from_bytes(hashlib.blake2b(word.encode(), digest_size=8).digest(), "big")
            vec[h % self._dim] += 1.0 if (h >> 63) & 1 else -1.0
        norm = math.sqrt(sum(x * x for x in vec))
        return [x / norm for x in vec] if norm else vec


class OpenAIEmbedder:
    """Embeddings через OpenAI-сумісний проксі (`POST {base_url}/embeddings`)."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        client: httpx.Client | None = None,
        batch: int = 64,
    ) -> None:
        self._url = base_url.rstrip("/") + "/embeddings"
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._model = model
        self._client = client or httpx.Client(timeout=30)
        self._batch = batch

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self._batch):
            part = texts[i : i + self._batch]
            r = self._client.post(
                self._url, headers=self._headers, json={"model": self._model, "input": part}
            )
            r.raise_for_status()  # 5xx/429 це тимчасова помилка: воркер повторить
            data = sorted(r.json()["data"], key=lambda d: d["index"])
            vectors = [d["embedding"] for d in data]
            if len(vectors) != len(part) or any(len(v) != EMBEDDING_DIM for v in vectors):
                raise PermanentError(f"embeddings must have {EMBEDDING_DIM} dimensions")
            out.extend(vectors)
        return out


# --- конвеєр --------------------------------------------------------------------------------
class Ingestor:
    def __init__(
        self,
        sf: "sessionmaker[Session]",
        storage: Storage,
        embedder: Embedder,
        *,
        max_bytes: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sf = sf
        self._storage = storage
        self._embedder = embedder
        self._max = max_bytes
        self._clock = clock

    def run(self, document_id: int) -> int:
        """Індексує документ. Ідемпотентно: повтор після збою не дублює чанки. Повертає їх число."""
        with self._sf() as s:
            doc = s.get(DocumentRow, document_id)
            if doc is None or doc.storage_key is None or doc.content_type is None:
                raise PermanentError("document or file not found")
            if doc.status == "indexed":
                return 0
            key, ctype = doc.storage_key, doc.content_type
        text = extract_text(self._storage.read_all(key, self._max), ctype)
        pieces = chunk_text(text)
        if not pieces:
            raise PermanentError("no text in document")
        vectors = self._embedder.embed(pieces)
        with self._sf.begin() as s:
            s.execute(delete(Chunk).where(Chunk.document_id == document_id))
            s.add_all(
                Chunk(document_id=document_id, position=i, content=c, embedding=v)
                for i, (c, v) in enumerate(zip(pieces, vectors, strict=True))
            )
            row = s.get(DocumentRow, document_id)
            assert row is not None  # noqa: S101 (щойно читали)
            row.status, row.error = "indexed", None
            # подія пишеться в ТІЙ САМІЙ транзакції: або обидва, або жодного (outbox)
            s.add(
                OutboxEvent(
                    event_type="document.indexed",
                    payload={"document_id": document_id, "chunks": len(pieces)},
                )
            )
        return len(pieces)

    def mark_failed(self, document_id: int, error: str, attempts: int) -> None:
        """Остаточна невдача: статус failed + запис у dead_letters для розбору."""
        with self._sf.begin() as s:
            row = s.get(DocumentRow, document_id)
            if row is not None:
                row.status, row.error = "failed", error[:500]
            s.add(
                DeadLetter(
                    job="ingest_document",
                    payload={"document_id": document_id},
                    error=error[:1000],
                    attempts=attempts,
                )
            )


def retry_delay(attempt: int) -> int:
    """Пауза перед наступною спробою: 5, 10, 20, ... секунд, не більше 5 хвилин."""
    return int(min(5 * 2 ** max(0, attempt - 1), 300))


class JobOutcome:
    DONE = "done"
    RETRY = "retry"
    DEAD = "dead"


def process(ingestor: Ingestor, document_id: int, attempt: int, max_tries: int) -> str:
    """Один запуск завдання: DONE, RETRY (спробувати пізніше) або DEAD (спроби вичерпано)."""
    try:
        ingestor.run(document_id)
        return JobOutcome.DONE
    except PermanentError as exc:
        ingestor.mark_failed(document_id, str(exc), attempt)
        return JobOutcome.DEAD
    except Exception as exc:
        log.exception(
            "ingest attempt %d/%d failed for document %s", attempt, max_tries, document_id
        )
        if attempt >= max_tries:
            ingestor.mark_failed(document_id, f"{type(exc).__name__}: {exc}", attempt)
            return JobOutcome.DEAD
        return JobOutcome.RETRY


# --- outbox ---------------------------------------------------------------------------------
def publish_pending(
    sf: "sessionmaker[Session]",
    publish: Callable[[str, dict[str, Any]], None],
    *,
    batch: int = 100,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    """Публікує події по порядку. Гарантія at-least-once: споживач має бути ідемпотентним."""
    sent = 0
    with sf.begin() as s:
        rows = s.scalars(
            select(OutboxEvent)
            .where(OutboxEvent.published_at.is_(None))
            .order_by(OutboxEvent.id)
            .limit(batch)
            .with_for_update(skip_locked=True)  # кілька публікаторів не беруть одні й ті самі
        ).all()
        for row in rows:
            try:
                publish(row.event_type, dict(row.payload))
            except Exception:
                log.exception("outbox publish failed for event %s, will retry", row.id)
                break  # порядок зберігаємо: далі не йдемо
            row.published_at = clock()
            sent += 1
    return sent
