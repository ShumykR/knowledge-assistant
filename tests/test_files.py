import pytest

from app.files import UnsupportedFile, safe_filename, sniff
from app.storage import LocalStorage, StorageError, TooLarge, new_key

# ---------- тип за вмістом ----------


def test_sniff_pdf_and_text() -> None:
    assert sniff(b"%PDF-1.7\n...") == "application/pdf"
    assert sniff("Привіт, світе\n# Markdown".encode()) == "text/plain"


@pytest.mark.parametrize(
    "head",
    [
        b"",
        b"MZ\x90\x00\x03\x00",  # виконуваний файл
        b"\x7fELF\x02\x01",
        b"\x89PNG\r\n\x1a\n\x00\x00",
        b"PK\x03\x04\x00\x00",  # zip/docx
        b"\xff\xfe\x00\x01\x80\x81",
        b"text with a NUL\x00 byte",
    ],
)
def test_sniff_rejects_non_text(head: bytes) -> None:
    with pytest.raises(UnsupportedFile):
        sniff(head)


def test_sniff_allows_multibyte_char_cut_at_the_end() -> None:
    cut = "Привіт".encode()[:-1]  # обірвана остання літера
    assert sniff(cut) == "text/plain"


def test_sniff_rejects_invalid_utf8_in_the_middle() -> None:
    with pytest.raises(UnsupportedFile):
        sniff(b"abc \xff\xfe\xfd def ghi jkl")


# ---------- імʼя файлу ----------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("report.pdf", "report.pdf"),
        ("../../etc/passwd", "passwd"),
        ("..\\..\\windows\\system32\\cmd.exe", "cmd.exe"),
        ("/abs/path/file.txt", "file.txt"),
        ("звіт 2026.pdf", "звіт 2026.pdf"),
        ("a\r\nSet-Cookie: x=1.txt", "aSet-Cookie_ x_1.txt"),
        ('bad<>:"|?*name.txt', "bad_______name.txt"),
        ("   ...   ", "file"),
        ("", "file"),
        ("..", "file"),
        ("CON", "file"),
        ("nul.txt", "file"),
        (".hidden", "hidden"),
        ("trailing. ", "trailing"),
    ],
)
def test_safe_filename(raw: str, expected: str) -> None:
    assert safe_filename(raw) == expected


def test_safe_filename_limits_length_and_keeps_extension() -> None:
    name = safe_filename("a" * 500 + ".pdf")
    assert len(name) <= 100 and name.endswith(".pdf")


def test_safe_filename_never_contains_separators_or_control_chars() -> None:
    for raw in ["a/b", "a\\b", "a\x00b", "a\nb", "%2e%2e%2f", "..%5c..%5cx"]:
        out = safe_filename(raw)
        assert "/" not in out and "\\" not in out and "\x00" not in out and "\n" not in out


# ---------- локальне сховище ----------


def test_local_storage_roundtrip(tmp_path) -> None:  # type: ignore[no-untyped-def]
    st = LocalStorage(tmp_path)
    key = new_key()
    up = st.begin_upload(key, 100)
    up.write(b"hello ")
    up.write(b"world")
    assert up.finish() == 11
    assert st.read_all(key, 100) == b"hello world"
    assert st.read_head(key, 5) == b"hello"
    assert st.size(key) == 11
    st.delete(key)
    assert st.size(key) is None


@pytest.mark.parametrize(
    "key", ["../x", "..\\x", "a/b", "", "x" * 32, "G" * 32, "0" * 31, "0" * 33, "/etc/passwd"]
)
def test_local_storage_rejects_bad_keys(tmp_path, key: str) -> None:  # type: ignore[no-untyped-def]
    st = LocalStorage(tmp_path)
    with pytest.raises(StorageError):
        st.size(key)
    with pytest.raises(StorageError):
        st.begin_upload(key, 10)


def test_upload_over_limit_leaves_no_file(tmp_path) -> None:  # type: ignore[no-untyped-def]
    st = LocalStorage(tmp_path)
    key = new_key()
    up = st.begin_upload(key, 10)
    up.write(b"123456")
    with pytest.raises(TooLarge):
        up.write(b"123456")
    up.abort()
    assert list(tmp_path.iterdir()) == []


def test_upload_does_not_overwrite_existing_file(tmp_path) -> None:  # type: ignore[no-untyped-def]
    st = LocalStorage(tmp_path)
    key = new_key()
    up = st.begin_upload(key, 10)
    up.write(b"first")
    up.finish()
    with pytest.raises(FileExistsError):
        st.begin_upload(key, 10)
    assert st.read_all(key, 10) == b"first"


def test_read_all_enforces_limit(tmp_path) -> None:  # type: ignore[no-untyped-def]
    st = LocalStorage(tmp_path)
    key = new_key()
    up = st.begin_upload(key, 100)
    up.write(b"x" * 50)
    up.finish()
    with pytest.raises(TooLarge):
        st.read_all(key, 10)


def test_presigned_upload_is_unavailable_locally(tmp_path) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(StorageError):
        LocalStorage(tmp_path).presigned_upload(new_key(), 10, 60)
