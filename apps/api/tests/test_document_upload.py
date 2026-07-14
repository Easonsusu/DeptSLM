"""Unit coverage for Phase 4 upload validation and external storage."""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from starlette.datastructures import Headers
from starlette.requests import Request

from app.authorization import DepartmentScope
from app.document_storage import DocumentStorage, DocumentStorageError
from app.document_upload import (
    UploadError,
    parse_content_disposition,
    parse_upload_metadata,
    stream_upload,
)
from app.settings import (
    DEFAULT_DEPARTMENT_DOCUMENT_QUOTA_BYTES,
    DEFAULT_DOCUMENT_MAX_BYTES,
    ConfigurationError,
    Settings,
)


def _headers(
    filename: str = "notes.txt",
    content_type: str = "text/plain; charset=utf-8",
    length: int = 5,
    **extra: str,
) -> Headers:
    values = {
        "content-disposition": f'attachment; filename="{filename}"',
        "content-type": content_type,
        "content-length": str(length),
        **{name.replace("_", "-"): value for name, value in extra.items()},
    }
    return Headers(values)


@pytest.mark.parametrize(
    ("filename", "content_type", "expected"),
    [
        ("paper.pdf", "application/pdf", "application/pdf"),
        ("notes.txt", "text/plain", "text/plain"),
        ("guide.md", "text/markdown; charset=UTF-8", "text/markdown"),
        ("guide.markdown", "text/plain; charset=us-ascii", "text/markdown"),
    ],
)
def test_supported_filename_and_media_type_pairs(
    filename: str, content_type: str, expected: str
) -> None:
    metadata = parse_upload_metadata(_headers(filename, content_type), 100)
    assert metadata.media_type == expected


@pytest.mark.parametrize(
    ("filename", "content_type"),
    [
        ("report.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("archive.zip", "application/zip"),
        ("page.html", "text/html"),
        ("image.png", "image/png"),
        ("script.exe", "application/octet-stream"),
        ("paper.pdf", "text/plain"),
        ("notes.txt", "application/pdf"),
        ("notes.txt", "text/plain; charset=iso-8859-1"),
    ],
)
def test_unsupported_or_mismatched_media_is_rejected(filename: str, content_type: str) -> None:
    with pytest.raises(UploadError) as captured:
        parse_upload_metadata(_headers(filename, content_type), 100)
    assert captured.value.status_code == 415


@pytest.mark.parametrize(
    "value",
    [
        'inline; filename="notes.txt"',
        "attachment",
        "attachment; filename=notes.txt",
        'attachment; filename=""',
        'attachment; filename="   "',
        'attachment; filename="."',
        'attachment; filename="../notes.txt"',
        'attachment; filename="folder\\notes.txt"',
        'attachment; filename="bad\x00.txt"',
        "attachment; filename*=UTF-8''bad%ZZ.txt",
        "attachment; filename*=UTF-8''%FF.txt",
        "attachment; filename*=UTF-8''bad name.txt",
        'attachment; filename="notes.txt"; creation-date="today"',
        "attachment; filename=\"../bad.txt\"; filename*=UTF-8''good.txt",
    ],
)
def test_invalid_content_disposition_is_rejected(value: str) -> None:
    with pytest.raises(UploadError):
        parse_content_disposition(value)


def test_rfc5987_filename_is_decoded_and_normalized() -> None:
    assert parse_content_disposition("attachment; filename*=UTF-8'en'cafe%CC%81.md") == "café.md"


def test_filename_character_and_utf8_byte_limits_are_enforced() -> None:
    for filename in ("a" * 256, "界" * 86):
        with pytest.raises(UploadError):
            parse_content_disposition(f'attachment; filename="{filename}"')


def test_duplicate_required_header_is_rejected() -> None:
    headers = Headers(
        raw=[
            (b"content-disposition", b'attachment; filename="notes.txt"'),
            (b"content-type", b"text/plain"),
            (b"content-type", b"text/markdown"),
            (b"content-length", b"5"),
        ]
    )
    with pytest.raises(UploadError) as captured:
        parse_upload_metadata(headers, 100)
    assert captured.value.reason_code == "invalid_headers"


def test_non_identity_content_encoding_is_rejected() -> None:
    with pytest.raises(UploadError) as captured:
        parse_upload_metadata(_headers(content_encoding="gzip"), 100)
    assert captured.value.reason_code == "content_encoding_denied"


@pytest.mark.parametrize("length", (0, 101))
def test_declared_empty_or_oversize_upload_is_rejected(length: int) -> None:
    with pytest.raises(UploadError):
        parse_upload_metadata(_headers(length=length), 100)


def _request(chunks: list[bytes]) -> Request:
    messages = [
        {
            "type": "http.request",
            "body": chunk,
            "more_body": index < len(chunks) - 1,
        }
        for index, chunk in enumerate(chunks)
    ]

    async def receive():
        return messages.pop(0)

    return Request({"type": "http", "method": "POST", "headers": []}, receive)


def _disconnecting_request(first_chunk: bytes) -> Request:
    messages = [
        {"type": "http.request", "body": first_chunk, "more_body": True},
        {"type": "http.disconnect"},
    ]

    async def receive():
        return messages.pop(0)

    return Request({"type": "http", "method": "POST", "headers": []}, receive)


def _storage(tmp_path: Path) -> tuple[DocumentStorage, DepartmentScope]:
    (tmp_path / "uploads").mkdir()
    return DocumentStorage(tmp_path), DepartmentScope(uuid4())


def test_chunked_text_stream_hashes_and_finalizes_with_private_permissions(tmp_path: Path) -> None:
    storage, department = _storage(tmp_path)
    staged = storage.create_staging(department, uuid4())
    metadata = parse_upload_metadata(_headers(length=7), 100)
    result = asyncio.run(stream_upload(_request([b"hel", b"lo\n", b"!"]), staged, metadata, 100))
    document_id = uuid4()
    source = staged.finalize(document_id)
    staged.release()

    assert result.byte_size == 7
    assert source.read_bytes() == b"hello\n!"
    assert stat.S_IMODE(source.stat().st_mode) == 0o600
    assert stat.S_IMODE(source.parent.stat().st_mode) == 0o700
    assert source == tmp_path / "uploads" / str(department) / str(document_id) / "source"


def test_pdf_signature_is_checked_across_chunk_boundaries(tmp_path: Path) -> None:
    storage, department = _storage(tmp_path)
    staged = storage.create_staging(department, uuid4())
    metadata = parse_upload_metadata(_headers("paper.pdf", "application/pdf", 8), 100)
    asyncio.run(stream_upload(_request([b"%P", b"DF-1.7"]), staged, metadata, 100))
    staged.abort()


def test_us_ascii_declaration_rejects_non_ascii_bytes(tmp_path: Path) -> None:
    storage, department = _storage(tmp_path)
    staged = storage.create_staging(department, uuid4())
    body = "café".encode()
    metadata = parse_upload_metadata(
        _headers("notes.txt", "text/plain; charset=us-ascii", len(body)), 100
    )
    with pytest.raises(UploadError):
        asyncio.run(stream_upload(_request([body]), staged, metadata, 100))


@pytest.mark.parametrize("chunks", [[b"not pdf"], [b"abc\xe2", b"\x28\xa1"], [b"abc\x00def"]])
def test_invalid_content_cleans_staging(tmp_path: Path, chunks: list[bytes]) -> None:
    storage, department = _storage(tmp_path)
    staged = storage.create_staging(department, uuid4())
    staging_path = staged.staging_path
    if chunks[0] == b"not pdf":
        metadata = parse_upload_metadata(_headers("paper.pdf", "application/pdf", 7), 100)
    else:
        metadata = parse_upload_metadata(_headers(length=sum(map(len, chunks))), 100)
    with pytest.raises(UploadError):
        asyncio.run(stream_upload(_request(chunks), staged, metadata, 100))
    assert not staging_path.exists()


def test_length_mismatch_and_streaming_limit_clean_staging(tmp_path: Path) -> None:
    storage, department = _storage(tmp_path)
    for declared, maximum, body in ((6, 100, b"short"), (6, 5, b"123456")):
        staged = storage.create_staging(department, uuid4())
        path = staged.staging_path
        metadata = parse_upload_metadata(_headers(length=declared), 100)
        with pytest.raises(UploadError):
            asyncio.run(stream_upload(_request([body]), staged, metadata, maximum))
        assert not path.exists()


def test_client_disconnect_cleans_staging(tmp_path: Path) -> None:
    storage, department = _storage(tmp_path)
    staged = storage.create_staging(department, uuid4())
    path = staged.staging_path
    metadata = parse_upload_metadata(_headers(length=10), 100)
    with pytest.raises(UploadError) as captured:
        asyncio.run(stream_upload(_disconnecting_request(b"hello"), staged, metadata, 100))
    assert captured.value.reason_code == "client_disconnected"
    assert not path.exists()


def test_finalize_compensation_removes_only_created_document(tmp_path: Path) -> None:
    storage, department = _storage(tmp_path)
    staged = storage.create_staging(department, uuid4())
    metadata = parse_upload_metadata(_headers(length=5), 100)
    asyncio.run(stream_upload(_request([b"hello"]), staged, metadata, 100))
    source = staged.finalize(uuid4())
    staged.compensate()
    assert not source.exists()
    assert (tmp_path / "uploads").is_dir()


def test_exclusive_staging_and_final_destination_do_not_overwrite(tmp_path: Path) -> None:
    storage, department = _storage(tmp_path)
    upload_id = uuid4()
    staged = storage.create_staging(department, upload_id)
    with pytest.raises(DocumentStorageError):
        storage.create_staging(department, upload_id)
    staged.write(b"hello")
    staged.finish()
    destination = tmp_path / "uploads" / str(department) / str(uuid4())
    destination.mkdir()
    (destination / "sentinel").write_text("keep")
    with pytest.raises(DocumentStorageError):
        staged.finalize(UUID(destination.name))
    staged.abort()
    assert (destination / "sentinel").read_text() == "keep"


def test_staging_refuses_symlinked_department(tmp_path: Path) -> None:
    storage, department = _storage(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "uploads" / str(department)).symlink_to(outside, target_is_directory=True)
    with pytest.raises(DocumentStorageError):
        storage.create_staging(department, uuid4())


def _settings_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "uploads").mkdir()
    monkeypatch.setenv("DEPTSLM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://test:test@127.0.0.1:1/test")


def test_document_settings_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _settings_environment(monkeypatch, tmp_path)
    settings = Settings.from_environment()
    assert settings.document_max_bytes == DEFAULT_DOCUMENT_MAX_BYTES
    assert settings.department_document_quota_bytes == DEFAULT_DEPARTMENT_DOCUMENT_QUOTA_BYTES


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DEPTSLM_DOCUMENT_MAX_BYTES", "0"),
        ("DEPTSLM_DOCUMENT_MAX_BYTES", "-1"),
        ("DEPTSLM_DOCUMENT_MAX_BYTES", "１２"),
        ("DEPTSLM_DOCUMENT_MAX_BYTES", "104857601"),
        ("DEPTSLM_DEPARTMENT_DOCUMENT_QUOTA_BYTES", "abc"),
    ],
)
def test_invalid_document_settings_stop_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str, value: str
) -> None:
    _settings_environment(monkeypatch, tmp_path)
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigurationError):
        Settings.from_environment()


def test_quota_must_cover_per_file_limit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _settings_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("DEPTSLM_DOCUMENT_MAX_BYTES", "100")
    monkeypatch.setenv("DEPTSLM_DEPARTMENT_DOCUMENT_QUOTA_BYTES", "99")
    with pytest.raises(ConfigurationError, match="greater than or equal"):
        Settings.from_environment()


def test_uploads_root_must_preexist_and_not_be_a_symlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DEPTSLM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://test:test@127.0.0.1:1/test")
    with pytest.raises(ConfigurationError, match="uploads"):
        Settings.from_environment()
    target = tmp_path / "target"
    target.mkdir()
    (tmp_path / "uploads").symlink_to(target, target_is_directory=True)
    with pytest.raises(ConfigurationError, match="not a symlink"):
        Settings.from_environment()


def test_storage_does_not_use_process_temp_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage, department = _storage(tmp_path)
    monkeypatch.setattr("tempfile.gettempdir", lambda: (_ for _ in ()).throw(AssertionError()))
    staged = storage.create_staging(department, uuid4())
    assert staged.staging_path.is_relative_to(tmp_path / "uploads")
    staged.abort()
    assert not any(name.endswith(".part") for _, _, files in os.walk(tmp_path) for name in files)
