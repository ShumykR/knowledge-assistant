"""Сховище файлів: локальна тека (dev) або S3 (production)."""

import re
import uuid
from pathlib import Path
from typing import Any, Protocol

KEY = re.compile(r"[0-9a-f]{32}")


class TooLarge(Exception):
    pass


class StorageError(Exception):
    pass


def new_key() -> str:
    return uuid.uuid4().hex


class Upload(Protocol):
    """Потоковий запис: `write` по шматках, `finish` фіксує файл, `abort` прибирає недописане."""

    def write(self, chunk: bytes) -> None: ...
    def finish(self) -> int: ...
    def abort(self) -> None: ...


class Storage(Protocol):
    def begin_upload(self, key: str, max_bytes: int) -> "Upload": ...
    def read_head(self, key: str, n: int) -> bytes: ...
    def read_all(self, key: str, max_bytes: int) -> bytes: ...
    def size(self, key: str) -> int | None: ...
    def delete(self, key: str) -> None: ...
    def presigned_upload(self, key: str, max_bytes: int, expires: int) -> dict[str, Any]: ...


class _LocalUpload:
    def __init__(self, path: Path, max_bytes: int) -> None:
        self._path = path
        self._max = max_bytes
        self._written = 0
        self._f = path.open("xb")  # "x": не перезаписуємо наявний файл

    def write(self, chunk: bytes) -> None:
        self._written += len(chunk)
        if self._written > self._max:
            raise TooLarge
        self._f.write(chunk)

    def finish(self) -> int:
        self._f.close()
        return self._written

    def abort(self) -> None:
        self._f.close()
        self._path.unlink(missing_ok=True)  # неповний файл не лишаємо


class _S3Upload:
    """Буферизує в памʼяті (ліміт невеликий) і одним PUT кладе в S3 при finish."""

    def __init__(self, client: Any, bucket: str, key: str, max_bytes: int) -> None:
        self._args = (client, bucket, key)
        self._max = max_bytes
        self._buf = bytearray()

    def write(self, chunk: bytes) -> None:
        self._buf += chunk
        if len(self._buf) > self._max:
            raise TooLarge

    def finish(self) -> int:
        client, bucket, key = self._args
        client.put_object(Bucket=bucket, Key=key, Body=bytes(self._buf))
        return len(self._buf)

    def abort(self) -> None:
        self._buf.clear()


class LocalStorage:
    """Файли лише всередині `root`; ключ має суворий формат, тож `../` у нього не потрапить."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        if not KEY.fullmatch(key):
            raise StorageError("invalid key")
        path = (self._root / key).resolve()
        if path.parent != self._root:  # друга лінія захисту (симлінки)
            raise StorageError("path escapes storage root")
        return path

    def begin_upload(self, key: str, max_bytes: int) -> Upload:
        return _LocalUpload(self._path(key), max_bytes)

    def read_head(self, key: str, n: int) -> bytes:
        with self._path(key).open("rb") as f:
            return f.read(n)

    def read_all(self, key: str, max_bytes: int) -> bytes:
        data = self._path(key).read_bytes()
        if len(data) > max_bytes:
            raise TooLarge
        return data

    def size(self, key: str) -> int | None:
        path = self._path(key)
        return path.stat().st_size if path.exists() else None

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def presigned_upload(self, key: str, max_bytes: int, expires: int) -> dict[str, Any]:
        raise StorageError("presigned upload requires S3 storage")


class S3Storage:
    def __init__(self, client: Any, bucket: str) -> None:
        self._s3 = client
        self._bucket = bucket

    def _check(self, key: str) -> None:
        if not KEY.fullmatch(key):
            raise StorageError("invalid key")

    def begin_upload(self, key: str, max_bytes: int) -> Upload:
        self._check(key)
        return _S3Upload(self._s3, self._bucket, key, max_bytes)

    def read_head(self, key: str, n: int) -> bytes:
        self._check(key)
        r = self._s3.get_object(Bucket=self._bucket, Key=key, Range=f"bytes=0-{n - 1}")
        body: bytes = r["Body"].read()
        return body

    def read_all(self, key: str, max_bytes: int) -> bytes:
        self._check(key)
        if (size := self.size(key)) is not None and size > max_bytes:
            raise TooLarge
        r = self._s3.get_object(Bucket=self._bucket, Key=key)
        body: bytes = r["Body"].read()
        return body

    def size(self, key: str) -> int | None:
        self._check(key)
        try:
            head = self._s3.head_object(Bucket=self._bucket, Key=key)
        except self._s3.exceptions.ClientError:
            return None
        size: int = head["ContentLength"]
        return size

    def delete(self, key: str) -> None:
        self._check(key)
        self._s3.delete_object(Bucket=self._bucket, Key=key)

    def presigned_upload(self, key: str, max_bytes: int, expires: int) -> dict[str, Any]:
        """Presigned POST: S3 сам відхилить файл більший за ліміт (умова content-length-range)."""
        self._check(key)
        post: dict[str, Any] = self._s3.generate_presigned_post(
            Bucket=self._bucket,
            Key=key,
            Conditions=[["content-length-range", 1, max_bytes]],
            ExpiresIn=expires,
        )
        return post
