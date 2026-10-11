import math
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import make_memory_engine, make_session_factory
from app.ingest import (
    HashEmbedder,
    Ingestor,
    JobOutcome,
    OpenAIEmbedder,
    PermanentError,
    chunk_text,
    extract_text,
    process,
    publish_pending,
    retry_delay,
)
from app.models import EMBEDDING_DIM, Chunk, DeadLetter, OutboxEvent, User
from app.models import Document as DocumentRow
from app.storage import LocalStorage, new_key

# ---------- chunk_text ----------


def test_chunk_empty_and_whitespace() -> None:
    assert chunk_text("") == [] and chunk_text("  \n\n  \t ") == []


def test_chunk_small_paragraphs_are_packed() -> None:
    text = "Перший абзац.\n\nДругий абзац.\n\nТретій."
    assert chunk_text(text, max_chars=100, overlap=10) == ["Перший абзац.\nДругий абзац.\nТретій."]


def test_chunk_respects_max_chars() -> None:
    text = "\n\n".join(f"Абзац номер {i} " + "слово " * 10 for i in range(30))
    chunks = chunk_text(text, max_chars=200, overlap=20)
    assert len(chunks) > 1 and all(len(c) <= 200 for c in chunks)
    assert "".join(chunks).count("Абзац номер") == 30  # нічого не загубили


def test_chunk_splits_long_paragraph_with_overlap() -> None:
    para = "".join(chr(ord("a") + i % 26) for i in range(250))
    chunks = chunk_text(para, max_chars=100, overlap=20)
    assert [len(c) for c in chunks] == [100, 100, 90]
    assert chunks[0][-20:] == chunks[1][:20] and chunks[1][-20:] == chunks[2][:20]
    assert chunks[0] + chunks[1][20:] + chunks[2][20:] == para


def test_chunk_drops_tail_fully_covered_by_overlap() -> None:
    para = "x" * 180  # вікна 0..100, 80..180, 160..180: останнє вже повністю в попередньому
    chunks = chunk_text(para, max_chars=100, overlap=20)
    assert [len(c) for c in chunks] == [100, 100]


def test_chunk_normalizes_whitespace() -> None:
    assert chunk_text("a   b\n c\t d") == ["a b c d"]


@pytest.mark.parametrize("max_chars, overlap", [(0, 0), (10, 10), (10, -1), (10, 11)])
def test_chunk_validates_params(max_chars: int, overlap: int) -> None:
    with pytest.raises(ValueError):
        chunk_text("text", max_chars=max_chars, overlap=overlap)


def test_chunk_is_deterministic() -> None:
    text = "Речення. " * 500
    assert chunk_text(text) == chunk_text(text)


# ---------- extract_text ----------


def test_extract_text_plain_and_replacement() -> None:
    assert extract_text("Привіт".encode(), "text/plain") == "Привіт"
    assert extract_text(b"ok \xff end", "text/plain").startswith("ok ")


def test_extract_text_unsupported_type() -> None:
    with pytest.raises(PermanentError):
        extract_text(b"x", "application/zip")


def test_extract_text_broken_pdf_is_permanent() -> None:
    with pytest.raises(PermanentError):
        extract_text(b"%PDF-1.4 this is not a pdf", "application/pdf")


def test_extract_text_pdf_roundtrip() -> None:
    assert "Hello PDF world" in extract_text(make_pdf("Hello PDF world"), "application/pdf")


def make_pdf(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return bytes(out)


# ---------- embedders ----------


def cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def test_hash_embedder_shape_norm_and_determinism() -> None:
    e = HashEmbedder()
    v1, v2 = e.embed(["кіт сидить на дивані", "кіт сидить на дивані"])
    assert v1 == v2 and len(v1) == EMBEDDING_DIM
    assert math.isclose(math.sqrt(sum(x * x for x in v1)), 1.0)


def test_hash_embedder_similar_texts_are_closer() -> None:
    e = HashEmbedder()
    base, near, far = e.embed(
        [
            "як налаштувати резервні копії бази",
            "налаштувати резервні копії",
            "рецепт борщу з буряком",
        ]
    )
    assert cos(base, near) > cos(base, far) + 0.3


def test_hash_embedder_empty_text_is_zero_vector() -> None:
    assert not any(HashEmbedder().embed(["   "])[0])


def openai_client(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_openai_embedder_orders_by_index_and_batches() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        assert request.headers["authorization"] == "Bearer key"
        assert body["model"] == "emb"
        calls.append(len(body["input"]))
        # навмисно у зворотному порядку: клієнт має відсортувати за index
        data = [
            {"index": i, "embedding": [float(int(t[1:]))] + [0.0] * (EMBEDDING_DIM - 1)}
            for i, t in enumerate(body["input"])
        ]
        return httpx.Response(200, json={"data": list(reversed(data))})

    e = OpenAIEmbedder("http://x/v1/", "key", "emb", client=openai_client(handler), batch=2)
    out = e.embed([f"t{i}" for i in range(5)])
    assert calls == [2, 2, 1]
    assert [v[0] for v in out] == [0.0, 1.0, 2.0, 3.0, 4.0]


def test_openai_embedder_wrong_dimension_is_permanent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]})

    with pytest.raises(PermanentError):
        OpenAIEmbedder("http://x", "k", "m", client=openai_client(handler)).embed(["a"])


def test_openai_embedder_server_error_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    with pytest.raises(httpx.HTTPStatusError):
        OpenAIEmbedder("http://x", "k", "m", client=openai_client(handler)).embed(["a"])


# ---------- Ingestor, retries, DLQ ----------


class Env:
    def __init__(self, tmp_path: Path, embedder: Any = None) -> None:
        self.sf: sessionmaker[Session] = make_session_factory(make_memory_engine())
        self.storage = LocalStorage(tmp_path)
        self.ingestor = Ingestor(
            self.sf, self.storage, embedder or HashEmbedder(), max_bytes=1_000_000
        )
        with self.sf.begin() as s:
            s.add(User(email="a@x.io"))

    def add_document(self, content: bytes, ctype: str = "text/plain", file: bool = True) -> int:
        key = new_key()
        if file:
            up = self.storage.begin_upload(key, 1_000_000)
            up.write(content)
            up.finish()
        with self.sf.begin() as s:
            row = DocumentRow(
                owner_id=1,
                title="t",
                status="queued",
                storage_key=key if file else None,
                content_type=ctype,
            )
            s.add(row)
            s.flush()
            return row.id

    def chunks(self, doc_id: int) -> list[str]:
        with self.sf() as s:
            return list(
                s.scalars(
                    select(Chunk.content)
                    .where(Chunk.document_id == doc_id)
                    .order_by(Chunk.position)
                )
            )

    def doc(self, doc_id: int) -> DocumentRow:
        with self.sf() as s:
            row = s.get(DocumentRow, doc_id)
            assert row is not None
            return row

    def count(self, model: Any) -> int:
        with self.sf() as s:
            return int(s.scalar(select(func.count()).select_from(model)) or 0)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def test_ingest_creates_chunks_status_and_outbox_event(env: Env) -> None:
    text = "\n\n".join(f"Абзац {i}. " + "речення " * 40 for i in range(6))
    doc_id = env.add_document(text.encode())
    n = env.ingestor.run(doc_id)
    assert n == len(env.chunks(doc_id)) > 1
    assert env.doc(doc_id).status == "indexed" and env.doc(doc_id).error is None
    with env.sf() as s:
        event = s.scalars(select(OutboxEvent)).one()
    assert event.event_type == "document.indexed"
    assert event.payload == {"document_id": doc_id, "chunks": n}
    assert event.published_at is None


def test_ingest_is_idempotent(env: Env) -> None:
    doc_id = env.add_document(b"Hello world text for chunks")
    assert env.ingestor.run(doc_id) == 1
    assert env.ingestor.run(doc_id) == 0  # вже indexed: нічого не робимо
    assert env.count(Chunk) == 1 and env.count(OutboxEvent) == 1


def test_reingest_replaces_old_chunks(env: Env) -> None:
    doc_id = env.add_document(b"first version")
    env.ingestor.run(doc_id)
    with env.sf.begin() as s:
        row = s.get(DocumentRow, doc_id)
        assert row is not None
        row.status = "queued"  # повторна індексація (наприклад, після зміни моделі)
    env.ingestor.run(doc_id)
    assert env.chunks(doc_id) == ["first version"]


def test_missing_document_or_file_is_permanent(env: Env) -> None:
    with pytest.raises(PermanentError):
        env.ingestor.run(999)
    with pytest.raises(PermanentError):
        env.ingestor.run(env.add_document(b"", file=False))


def test_document_without_text_is_permanent(env: Env) -> None:
    with pytest.raises(PermanentError):
        env.ingestor.run(env.add_document(b"  \n\n \t "))


class FlakyEmbedder:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.calls <= self.failures:
            raise ConnectionError("embeddings API is down")
        return HashEmbedder().embed(texts)


def test_process_retries_then_succeeds(tmp_path: Path) -> None:
    flaky = FlakyEmbedder(failures=2)
    env = Env(tmp_path, flaky)
    doc_id = env.add_document(b"some text to index")
    assert process(env.ingestor, doc_id, 1, 4) == JobOutcome.RETRY
    assert process(env.ingestor, doc_id, 2, 4) == JobOutcome.RETRY
    assert env.doc(doc_id).status == "queued" and env.count(DeadLetter) == 0
    assert process(env.ingestor, doc_id, 3, 4) == JobOutcome.DONE
    assert env.doc(doc_id).status == "indexed" and env.count(Chunk) == 1


def test_process_goes_to_dead_letters_after_last_try(tmp_path: Path) -> None:
    env = Env(tmp_path, FlakyEmbedder(failures=99))
    doc_id = env.add_document(b"some text")
    assert process(env.ingestor, doc_id, 3, 4) == JobOutcome.RETRY
    assert process(env.ingestor, doc_id, 4, 4) == JobOutcome.DEAD
    doc = env.doc(doc_id)
    assert doc.status == "failed" and "ConnectionError" in (doc.error or "")
    with env.sf() as s:
        dead = s.scalars(select(DeadLetter)).one()
    assert dead.job == "ingest_document" and dead.payload == {"document_id": doc_id}
    assert dead.attempts == 4 and "embeddings API is down" in dead.error
    assert env.count(Chunk) == 0 and env.count(OutboxEvent) == 0


def test_process_permanent_error_skips_retries(env: Env) -> None:
    doc_id = env.add_document(b"%PDF-1.4 garbage", ctype="application/pdf")
    assert process(env.ingestor, doc_id, 1, 4) == JobOutcome.DEAD
    assert env.doc(doc_id).status == "failed"
    assert env.count(DeadLetter) == 1


def test_retry_delay_grows_and_is_capped() -> None:
    assert [retry_delay(n) for n in (0, 1, 2, 3, 4)] == [5, 5, 10, 20, 40]
    assert retry_delay(50) == 300


# ---------- outbox ----------


def add_events(env: Env, n: int) -> None:
    with env.sf.begin() as s:
        s.add_all(OutboxEvent(event_type="e", payload={"i": i}) for i in range(n))


def test_publish_pending_in_order_and_only_once(env: Env) -> None:
    add_events(env, 3)
    seen: list[int] = []
    assert publish_pending(env.sf, lambda t, p: seen.append(p["i"])) == 3
    assert seen == [0, 1, 2]
    assert publish_pending(env.sf, lambda t, p: seen.append(p["i"])) == 0
    assert seen == [0, 1, 2]


def test_publish_stops_at_first_failure_and_keeps_order(env: Env) -> None:
    add_events(env, 4)
    seen: list[int] = []

    def flaky(_t: str, p: dict[str, Any]) -> None:
        if p["i"] == 2 and not seen.count(2):
            seen.append(2)
            raise ConnectionError
        seen.append(p["i"])

    assert publish_pending(env.sf, flaky) == 2  # 0 і 1 відправлено, на 2 зупинка
    assert publish_pending(env.sf, flaky) == 2  # повтор довіряє 2 і 3
    assert seen == [0, 1, 2, 2, 3]


def test_publish_batches(env: Env) -> None:
    add_events(env, 5)
    assert publish_pending(env.sf, lambda t, p: None, batch=2) == 2
    assert publish_pending(env.sf, lambda t, p: None, batch=10) == 3
