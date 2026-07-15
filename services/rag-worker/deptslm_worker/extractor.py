"""Parent-side constrained subprocess orchestration."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass

from app.extraction_domain import SAFE_EXTRACTION_ERROR_CODES
from deptslm_worker.normalization import NormalizedDocument, ProvenanceSpan
from deptslm_worker.storage import ExtractionStaging, SourceHandle

RESULT_LIMIT = 1_048_576


class ExtractorError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code if code in SAFE_EXTRACTION_ERROR_CODES else "parser_failed"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class ExtractorResult:
    parser_name: str
    parser_version: str
    normalized: NormalizedDocument


def run_extractor(
    source: SourceHandle,
    staging: ExtractionStaging,
    *,
    media_type: str,
    max_pages: int,
    max_bytes: int,
    timeout_seconds: int,
    heartbeat: Callable[[], bool],
    should_stop: Callable[[], bool],
) -> ExtractorResult:
    if os.name != "posix":
        raise ExtractorError("parser_failed")
    output_fd = staging.create_file("normalized.txt")
    result_fd = staging.create_file(".runner-result.json")
    argv = (
        sys.executable,
        "-I",
        "-m",
        "deptslm_worker.extraction_runner",
        "--source-fd",
        str(source.descriptor),
        "--output-fd",
        str(output_fd),
        "--result-fd",
        str(result_fd),
        "--media-type",
        media_type,
        "--max-pages",
        str(max_pages),
        "--max-bytes",
        str(max_bytes),
    )
    environment = {
        "PATH": os.defpath,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TMPDIR": staging.temporary_directory,
    }
    process = None
    try:
        process = subprocess.Popen(
            argv,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            pass_fds=(source.descriptor, output_fd, result_fd, staging.claim_fd),
            start_new_session=True,
            env=environment,
            preexec_fn=lambda: _set_resource_limits(timeout_seconds, max_bytes),
        )
        deadline = time.monotonic() + timeout_seconds
        next_heartbeat = time.monotonic()
        while process.poll() is None:
            now = time.monotonic()
            if should_stop():
                _terminate_group(process)
                raise ExtractorError("worker_shutdown")
            if now >= deadline:
                _terminate_group(process)
                raise ExtractorError("extraction_timeout")
            if now >= next_heartbeat:
                if not heartbeat():
                    _terminate_group(process)
                    raise ExtractorError("claim_lost")
                next_heartbeat = now + max(1, min(10, timeout_seconds // 4))
            time.sleep(0.05)
    except OSError as error:
        if process is not None:
            _terminate_group(process)
        raise ExtractorError("parser_failed") from error
    finally:
        os.close(output_fd)
        os.close(result_fd)

    payload = staging.read_file(".runner-result.json", RESULT_LIMIT)
    staging.remove_file(".runner-result.json")
    try:
        result = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ExtractorError("parser_failed") from error
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise ExtractorError(str(result.get("error_code", "parser_failed")))
    if process.returncode != 0:
        raise ExtractorError("parser_failed")
    normalized_bytes = staging.read_file("normalized.txt", max_bytes)
    try:
        text = normalized_bytes.decode("utf-8")
        spans = tuple(ProvenanceSpan(*span) for span in result["spans"])
        normalized = NormalizedDocument(text, result["provenance_kind"], spans)
        parser_name = str(result["parser_name"])
        parser_version = str(result["parser_version"])
    except (KeyError, TypeError, ValueError, UnicodeDecodeError) as error:
        raise ExtractorError("parser_failed") from error
    if not text.strip() or normalized.provenance_kind not in {"page", "line"}:
        raise ExtractorError("no_extractable_text")
    if not _safe_identifier(parser_name) or not _safe_version(parser_version):
        raise ExtractorError("parser_failed")
    if any(
        span.char_start < 0
        or span.char_end < span.char_start
        or span.char_end > len(text)
        or span.number <= 0
        for span in spans
    ):
        raise ExtractorError("parser_failed")
    return ExtractorResult(parser_name, parser_version, normalized)


def _set_resource_limits(timeout_seconds: int, max_bytes: int) -> None:
    import resource

    limits = (
        (resource.RLIMIT_CORE, 0),
        (resource.RLIMIT_CPU, max(1, timeout_seconds)),
        (resource.RLIMIT_FSIZE, max(max_bytes, RESULT_LIMIT)),
        (resource.RLIMIT_NOFILE, 32),
    )
    for resource_id, value in limits:
        try:
            resource.setrlimit(resource_id, (value, value))
        except (OSError, ValueError):
            pass
    for name, value in (
        ("RLIMIT_AS", max(max_bytes * 6, 268_435_456)),
        ("RLIMIT_NPROC", 0),
    ):
        resource_id = getattr(resource, name, None)
        if resource_id is not None:
            try:
                resource.setrlimit(resource_id, (value, value))
            except (OSError, ValueError):
                pass


def _terminate_group(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=2)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def _safe_identifier(value: str) -> bool:
    return (
        bool(value)
        and len(value) <= 100
        and all(
            character.isascii() and (character.isalnum() or character in "._-")
            for character in value
        )
    )


def _safe_version(value: str) -> bool:
    return (
        bool(value)
        and len(value) <= 100
        and all(
            character.isascii() and (character.isalnum() or character in "._+-")
            for character in value
        )
    )
