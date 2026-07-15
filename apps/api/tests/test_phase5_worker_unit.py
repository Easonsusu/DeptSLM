"""Unit coverage for Phase 5 normalization, chunking, storage, and parser isolation."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path
from uuid import uuid4

import deptslm_worker.extractor as extractor_module
import pytest
from deptslm_worker.chunking import ChunkingError, chunk_document
from deptslm_worker.extractor import ExtractorError, run_extractor
from deptslm_worker.normalization import (
    NormalizationError,
    normalize_pdf_pages,
    normalize_text_source,
)
from deptslm_worker.settings import WorkerConfigurationError, WorkerSettings
from deptslm_worker.storage import (
    ExtractionStorage,
    ExtractionStorageError,
    SourceStorage,
)
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.authorization import DepartmentScope

pytestmark = pytest.mark.unit


def _root(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "uploads").mkdir(exist_ok=True)
    (tmp_path / "extracted_text").mkdir(exist_ok=True)
    return tmp_path


def _settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> WorkerSettings:
    _root(tmp_path)
    monkeypatch.setenv("DEPTSLM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://deptslm:deptslm@127.0.0.1:1/test")
    return WorkerSettings.from_environment()


def test_worker_settings_defaults_and_bounds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _settings(monkeypatch, tmp_path)
    assert (settings.extraction_timeout_seconds, settings.max_extracted_bytes) == (
        120,
        104_857_600,
    )
    assert (settings.chunk_max_chars, settings.chunk_overlap_chars) == (1_200, 200)
    assert settings.department_extracted_quota_bytes == 4_294_967_296


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DEPTSLM_EXTRACTION_TIMEOUT_SECONDS", "0"),
        ("DEPTSLM_MAX_EXTRACTED_BYTES", "1_000"),
        ("DEPTSLM_MAX_PDF_PAGES", "5001"),
        ("DEPTSLM_CHUNK_MAX_CHARS", "255"),
        ("DEPTSLM_WORKER_POLL_SECONDS", "61"),
        ("DEPTSLM_EXTRACTION_LEASE_SECONDS", "120"),
    ],
)
def test_worker_settings_reject_malformed_or_unsafe_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str, value: str
) -> None:
    _root(tmp_path)
    monkeypatch.setenv("DEPTSLM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1/test")
    monkeypatch.setenv(name, value)
    with pytest.raises(WorkerConfigurationError):
        WorkerSettings.from_environment()


def test_worker_settings_reject_symlinked_runtime_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "target"
    _root(target)
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    monkeypatch.setenv("DEPTSLM_DATA_DIR", str(alias))
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1/test")
    with pytest.raises(WorkerConfigurationError):
        WorkerSettings.from_environment()


def test_normalization_is_deterministic_and_preserves_whitespace() -> None:
    raw = "\ufeffCafe\u0301\r\n\r  indented\ttext".encode()
    first = normalize_text_source(raw)
    second = normalize_text_source(raw)
    assert first == second
    assert first.text == "Café\n\n  indented\ttext"
    assert [span.number for span in first.spans] == [1, 2, 3]


@pytest.mark.parametrize(
    ("raw", "code"),
    [(b"\xff", "invalid_utf8"), (b"a\x00b", "invalid_utf8"), (b"  \n", "no_extractable_text")],
)
def test_normalization_rejects_invalid_or_empty_text(raw: bytes, code: str) -> None:
    with pytest.raises(NormalizationError) as captured:
        normalize_text_source(raw)
    assert captured.value.code == code


def test_pdf_normalization_retains_page_order_and_ranges() -> None:
    value = normalize_pdf_pages(["First\rpage", "Second page"])
    assert value.text == "First\npage\n\f\nSecond page"
    assert [(span.number, value.text[span.char_start : span.char_end]) for span in value.spans] == [
        (1, "First\npage"),
        (2, "Second page"),
    ]


def test_chunking_prefers_boundaries_and_is_deterministic() -> None:
    document = normalize_text_source(("alpha beta\n\n" + "gamma " * 80).encode())
    first = chunk_document(document, max_chars=64, overlap_chars=16, max_chunks=100)
    second = chunk_document(document, max_chars=64, overlap_chars=16, max_chunks=100)
    assert first == second
    assert all(0 < len(chunk.text) <= 64 and chunk.text.strip() for chunk in first)
    assert all(chunk.char_start < chunk.char_end for chunk in first)
    assert first[1].char_start < first[0].char_end
    assert first[0].line_start == 1 and first[0].line_end is not None
    assert all(
        chunk.content_sha256 == hashlib.sha256(chunk.text.encode()).hexdigest()
        and chunk.byte_size == len(chunk.text.encode())
        for chunk in first
    )


def test_chunking_hard_boundary_and_page_provenance() -> None:
    document = normalize_pdf_pages(["A" * 300, "B" * 300])
    chunks = chunk_document(document, max_chars=256, overlap_chars=0, max_chunks=10)
    assert max(len(chunk.text) for chunk in chunks) <= 256
    assert chunks[0].page_start == chunks[0].page_end == 1
    assert chunks[-1].page_start == chunks[-1].page_end == 2
    assert all(chunk.line_start is None for chunk in chunks)


def test_chunk_limit_is_fail_closed() -> None:
    document = normalize_text_source(("word " * 1_000).encode())
    with pytest.raises(ChunkingError) as captured:
        chunk_document(document, max_chars=256, overlap_chars=0, max_chunks=2)
    assert captured.value.code == "chunk_limit_exceeded"


def test_source_storage_verifies_exact_canonical_source(tmp_path: Path) -> None:
    root = _root(tmp_path)
    department = DepartmentScope(uuid4())
    document_id = uuid4()
    source = root / "uploads" / str(department) / str(document_id) / "source"
    source.parent.mkdir(parents=True)
    payload = b"private source"
    source.write_bytes(payload)
    original_mode = source.stat().st_mode
    with SourceStorage(root).open_verified(
        department, document_id, len(payload), hashlib.sha256(payload).hexdigest()
    ) as handle:
        assert os.read(handle.descriptor, len(payload)) == payload
        with pytest.raises(OSError):
            os.write(handle.descriptor, b"mutation")
    assert source.read_bytes() == payload and source.stat().st_mode == original_mode


@pytest.mark.parametrize("mismatch", ["size", "hash"])
def test_source_integrity_mismatch_is_safe(tmp_path: Path, mismatch: str) -> None:
    root = _root(tmp_path)
    department = DepartmentScope(uuid4())
    document_id = uuid4()
    source = root / "uploads" / str(department) / str(document_id) / "source"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source")
    size = 7 if mismatch == "size" else 6
    digest = "0" * 64 if mismatch == "hash" else hashlib.sha256(b"source").hexdigest()
    with pytest.raises(ExtractionStorageError) as captured:
        SourceStorage(root).open_verified(department, document_id, size, digest)
    assert captured.value.code == "source_integrity_mismatch"


def test_source_and_extracted_symlinks_are_rejected(tmp_path: Path) -> None:
    root = _root(tmp_path)
    real = tmp_path / "real"
    real.mkdir()
    department = DepartmentScope(uuid4())
    (root / "uploads" / str(department)).symlink_to(real, target_is_directory=True)
    with pytest.raises(ExtractionStorageError):
        SourceStorage(root).open_verified(department, uuid4(), 1, "0" * 64)
    (root / "extracted_text").rmdir()
    (root / "extracted_text").symlink_to(real, target_is_directory=True)
    with pytest.raises(ExtractionStorageError):
        ExtractionStorage(root)


def test_extraction_staging_is_private_exclusive_and_non_overwriting(tmp_path: Path) -> None:
    root = _root(tmp_path)
    department = DepartmentScope(uuid4())
    document_id, extraction_id, claim = uuid4(), uuid4(), uuid4()
    storage = ExtractionStorage(root)
    staging = storage.create_staging(department, document_id, extraction_id, claim)
    staging.write_file("normalized.txt", b"text")
    staging.write_file("chunks.jsonl", b"{}\n")
    staging.write_file("manifest.json", b"{}\n")
    with pytest.raises(ExtractionStorageError):
        staging.create_file("normalized.txt")
    assert stat.S_IMODE(os.fstat(staging.claim_fd).st_mode) == 0o700
    assert stat.S_IMODE(os.stat("normalized.txt", dir_fd=staging.claim_fd).st_mode) == 0o600
    staging.publish()
    final = root / "extracted_text" / str(department) / str(document_id) / str(extraction_id)
    assert sorted(path.name for path in final.iterdir()) == [
        "chunks.jsonl",
        "manifest.json",
        "normalized.txt",
    ]
    staging.close()
    second = storage.create_staging(department, document_id, extraction_id, uuid4())
    for name in ("normalized.txt", "chunks.jsonl", "manifest.json"):
        second.write_file(name, b"x")
    with pytest.raises(ExtractionStorageError):
        second.publish()
    second.cleanup()
    assert (final / "normalized.txt").read_bytes() == b"text"


def _run_subprocess(
    tmp_path: Path,
    payload: bytes,
    media_type: str,
    *,
    max_bytes: int = 10_000,
    max_pages: int = 10,
    timeout_seconds: int = 10,
    should_stop=lambda: False,
):
    root = _root(tmp_path)
    department = DepartmentScope(uuid4())
    document_id = uuid4()
    source_path = root / "uploads" / str(department) / str(document_id) / "source"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(payload)
    source = SourceStorage(root).open_verified(
        department, document_id, len(payload), hashlib.sha256(payload).hexdigest()
    )
    staging = ExtractionStorage(root).create_staging(department, document_id, uuid4(), uuid4())
    try:
        return run_extractor(
            source,
            staging,
            media_type=media_type,
            max_pages=max_pages,
            max_bytes=max_bytes,
            timeout_seconds=timeout_seconds,
            heartbeat=lambda: True,
            should_stop=should_stop,
        ), staging
    finally:
        source.close()


@pytest.mark.parametrize("media_type", ["text/plain", "text/markdown"])
def test_subprocess_extracts_utf8_without_rendering_markdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, media_type: str
) -> None:
    monkeypatch.setenv("DATABASE_URL", "must-not-reach-parser")
    monkeypatch.setenv("DEPTSLM_AUTH_SECRET", "must-not-reach-parser")
    text = b"# Heading\r\n<script>untrusted()</script>"
    result, staging = _run_subprocess(tmp_path, text, media_type)
    assert result.normalized.text == "# Heading\n<script>untrusted()</script>"
    assert result.parser_name == "python-utf8"
    assert not (tmp_path / ".runner-result.json").exists()
    staging.cleanup()


def test_subprocess_uses_fixed_isolated_process_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_popen = subprocess.Popen
    captured: dict = {}

    def inspect_popen(*args, **kwargs):
        captured.update(kwargs)
        captured["argv"] = args[0]
        return real_popen(*args, **kwargs)

    monkeypatch.setenv("DATABASE_URL", "must-not-reach-parser")
    monkeypatch.setenv("DEPTSLM_AUTH_SECRET", "must-not-reach-parser")
    monkeypatch.setattr(extractor_module.subprocess, "Popen", inspect_popen)
    result, staging = _run_subprocess(tmp_path, b"safe text", "text/plain")
    assert result.normalized.text == "safe text"
    assert captured["shell"] is False
    assert captured["start_new_session"] is True
    assert captured["close_fds"] is True
    assert captured["stdout"] == subprocess.DEVNULL
    assert captured["stderr"] == subprocess.DEVNULL
    assert "-I" in captured["argv"]
    assert "DATABASE_URL" not in captured["env"]
    assert "DEPTSLM_AUTH_SECRET" not in captured["env"]
    assert "PYTHONPATH" not in captured["env"]
    assert "notes.txt" not in captured["argv"]
    staging.cleanup()


@pytest.mark.parametrize(
    ("payload", "code"),
    [(b"\xff", "invalid_utf8"), (b"a\x00b", "invalid_utf8"), (b"   ", "no_extractable_text")],
)
def test_subprocess_revalidates_text(payload: bytes, code: str, tmp_path: Path) -> None:
    with pytest.raises(ExtractorError) as captured:
        _run_subprocess(tmp_path, payload, "text/plain")
    assert captured.value.code == code


def test_subprocess_enforces_output_limit_and_shutdown_cleanup(tmp_path: Path) -> None:
    with pytest.raises(ExtractorError) as limited:
        _run_subprocess(tmp_path / "limited", b"x" * 20, "text/plain", max_bytes=10)
    assert limited.value.code == "extraction_output_limit"
    with pytest.raises(ExtractorError) as stopped:
        _run_subprocess(
            tmp_path / "stopped",
            b"content",
            "text/plain",
            should_stop=lambda: True,
        )
    assert stopped.value.code == "worker_shutdown"


def test_subprocess_timeout_terminates_process_group(tmp_path: Path) -> None:
    with pytest.raises(ExtractorError) as captured:
        _run_subprocess(
            tmp_path,
            b"x" * (2 * 1024 * 1024),
            "text/plain",
            max_bytes=4 * 1024 * 1024,
            timeout_seconds=0,
        )
    assert captured.value.code == "extraction_timeout"


def _pdf_bytes(tmp_path: Path, texts: list[str], *, encrypted: bool = False) -> bytes:
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)
    for text in texts:
        page = writer.add_blank_page(width=612, height=792)
        resources = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
        )
        page[NameObject("/Resources")] = resources
        stream = DecodedStreamObject()
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream.set_data(f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    if encrypted:
        writer.encrypt("password")
    path = tmp_path / f"fixture-{uuid4()}.pdf"
    with path.open("wb") as output:
        writer.write(output)
    return path.read_bytes()


def test_subprocess_extracts_synthetic_pdf_in_page_order(tmp_path: Path) -> None:
    payload = _pdf_bytes(tmp_path, ["first page", "second page"])
    result, staging = _run_subprocess(tmp_path / "runtime", payload, "application/pdf")
    assert "first page" in result.normalized.text and "second page" in result.normalized.text
    assert [span.number for span in result.normalized.spans] == [1, 2]
    staging.cleanup()


def test_subprocess_rejects_encrypted_malformed_and_blank_pdf(tmp_path: Path) -> None:
    encrypted = _pdf_bytes(tmp_path, ["secret"], encrypted=True)
    blank = _pdf_bytes(tmp_path, [""])
    for index, (payload, code) in enumerate(
        (
            (encrypted, "encrypted_pdf"),
            (b"%PDF-broken", "invalid_pdf"),
            (blank, "no_extractable_text"),
        )
    ):
        with pytest.raises(ExtractorError) as captured:
            _run_subprocess(tmp_path / f"runtime-{index}", payload, "application/pdf")
        assert captured.value.code == code


def test_subprocess_enforces_pdf_page_limit(tmp_path: Path) -> None:
    payload = _pdf_bytes(tmp_path, ["first", "second"])
    with pytest.raises(ExtractorError) as captured:
        _run_subprocess(tmp_path / "page-limit", payload, "application/pdf", max_pages=1)
    assert captured.value.code == "page_limit_exceeded"


def test_chunks_json_shape_contains_text_only_in_external_artifact(tmp_path: Path) -> None:
    document = normalize_text_source(b"synthetic content")
    chunk = chunk_document(document, max_chars=256, overlap_chars=0, max_chunks=2)[0]
    payload = json.loads(json.dumps({"text": chunk.text, "ordinal": chunk.ordinal}))
    assert payload == {"text": "synthetic content", "ordinal": 0}
