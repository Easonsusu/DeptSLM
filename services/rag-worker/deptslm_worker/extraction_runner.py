"""Installed constrained parser process; invoked only with inherited descriptors."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
from importlib.metadata import version

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from deptslm_worker.normalization import (
    NormalizationError,
    normalize_pdf_pages,
    normalize_text_source,
)

MAX_RESULT_BYTES = 1_048_576


def _disabled(*_args, **_kwargs):
    raise RuntimeError("operation disabled in extraction runner")


def _disable_capabilities() -> None:
    socket.socket = _disabled  # type: ignore[assignment]
    socket.create_connection = _disabled  # type: ignore[assignment]
    subprocess.Popen = _disabled  # type: ignore[assignment]
    subprocess.run = _disabled  # type: ignore[assignment]
    subprocess.call = _disabled  # type: ignore[assignment]
    subprocess.check_call = _disabled  # type: ignore[assignment]
    subprocess.check_output = _disabled  # type: ignore[assignment]


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--source-fd", type=int, required=True)
    parser.add_argument("--output-fd", type=int, required=True)
    parser.add_argument("--result-fd", type=int, required=True)
    parser.add_argument(
        "--media-type",
        choices=("application/pdf", "text/plain", "text/markdown"),
        required=True,
    )
    parser.add_argument("--max-pages", type=int, required=True)
    parser.add_argument("--max-bytes", type=int, required=True)
    return parser.parse_args()


def _extract(args: argparse.Namespace) -> dict:
    source = os.fdopen(args.source_fd, "rb", closefd=False)
    if args.media_type == "application/pdf":
        try:
            reader = PdfReader(source, strict=True)
            if reader.is_encrypted:
                return {"ok": False, "error_code": "encrypted_pdf"}
            if len(reader.pages) > args.max_pages:
                return {"ok": False, "error_code": "page_limit_exceeded"}
            pages = [page.extract_text() or "" for page in reader.pages]
            normalized = normalize_pdf_pages(pages)
        except PdfReadError:
            return {"ok": False, "error_code": "invalid_pdf"}
        parser_name = "pypdf"
        parser_version = version("pypdf")
    else:
        raw = source.read(args.max_bytes + 1)
        if len(raw) > args.max_bytes:
            return {"ok": False, "error_code": "extraction_output_limit"}
        normalized = normalize_text_source(raw)
        parser_name = "python-utf8"
        parser_version = f"{os.sys.version_info.major}.{os.sys.version_info.minor}"

    encoded = normalized.text.encode("utf-8")
    if not encoded or len(encoded) > args.max_bytes:
        return {
            "ok": False,
            "error_code": "extraction_output_limit" if encoded else "no_extractable_text",
        }
    _write_all(args.output_fd, encoded)
    os.fsync(args.output_fd)
    return {
        "ok": True,
        "parser_name": parser_name,
        "parser_version": parser_version,
        "provenance_kind": normalized.provenance_kind,
        "spans": [[span.char_start, span.char_end, span.number] for span in normalized.spans],
    }


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _write_result(descriptor: int, result: dict) -> None:
    payload = json.dumps(result, sort_keys=True, separators=(",", ":")).encode("ascii")
    if len(payload) > MAX_RESULT_BYTES:
        payload = b'{"error_code":"parser_failed","ok":false}'
    _write_all(descriptor, payload)
    os.fsync(descriptor)


def main() -> int:
    args = _arguments()
    _disable_capabilities()
    try:
        result = _extract(args)
    except NormalizationError as error:
        result = {"ok": False, "error_code": error.code}
    except (UnicodeError, ValueError):
        result = {"ok": False, "error_code": "invalid_pdf"}
    except BaseException:
        result = {"ok": False, "error_code": "parser_failed"}
    try:
        _write_result(args.result_fd, result)
    except BaseException:
        return 2
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
