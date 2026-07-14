"""Strict metadata parsing and incremental validation for raw document uploads."""

from __future__ import annotations

import asyncio
import codecs
import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import unquote_to_bytes

from fastapi import Request
from starlette.requests import ClientDisconnect


class UploadError(RuntimeError):
    def __init__(self, status_code: int, detail: str, reason_code: str) -> None:
        self.status_code = status_code
        self.detail = detail
        self.reason_code = reason_code
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class UploadMetadata:
    original_filename: str
    media_type: str
    content_length: int
    ascii_only: bool = False


@dataclass(frozen=True, slots=True)
class StreamResult:
    byte_size: int
    sha256: str


class UploadWriter(Protocol):
    def write(self, chunk: bytes) -> None: ...

    def finish(self) -> None: ...

    def abort(self) -> None: ...


_PARAMETER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_HEX_PAIR = re.compile(r"^[0-9A-Fa-f]{2}$")
_RFC5987_ATTR_CHAR = re.compile(r"^[!#$&+.^_`|~0-9A-Za-z-]$")
_RFC5987_LANGUAGE = re.compile(r"^[0-9A-Za-z-]*$")


def parse_upload_metadata(headers, max_bytes: int) -> UploadMetadata:
    """Validate upload headers before any request-body bytes are consumed."""

    _require_single_header(headers, "content-disposition")
    _require_single_header(headers, "content-type")
    _require_single_header(headers, "content-length")
    if len(headers.getlist("content-encoding")) > 1:
        raise UploadError(400, "Content-Encoding is duplicated", "invalid_headers")
    content_encoding = headers.get("content-encoding")
    if content_encoding is not None and content_encoding.strip().lower() != "identity":
        raise UploadError(415, "Content encoding is not supported", "content_encoding_denied")

    filename = parse_content_disposition(headers["content-disposition"])
    media_type, ascii_only = _validate_media_type(filename, headers["content-type"])
    raw_length = headers["content-length"]
    if not raw_length or not raw_length.isascii() or not raw_length.isdecimal():
        raise UploadError(400, "Content-Length must be a positive decimal", "invalid_length")
    content_length = int(raw_length)
    if content_length <= 0:
        raise UploadError(400, "Document must not be empty", "empty_document")
    if content_length > max_bytes:
        raise UploadError(413, "Document exceeds the configured size limit", "size_limit")
    return UploadMetadata(filename, media_type, content_length, ascii_only)


def _require_single_header(headers, name: str) -> None:
    values = headers.getlist(name)
    if len(values) != 1:
        raise UploadError(400, f"Exactly one {name.title()} header is required", "invalid_headers")


def parse_content_disposition(value: str) -> str:
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise UploadError(
            400, "Content-Disposition contains control characters", "invalid_filename"
        )
    parts = _split_parameters(value)
    if not parts or parts[0].strip().lower() != "attachment":
        raise UploadError(400, "Content-Disposition must be attachment", "invalid_filename")
    parameters: dict[str, str] = {}
    for raw_parameter in parts[1:]:
        if "=" not in raw_parameter:
            raise UploadError(400, "Content-Disposition is malformed", "invalid_filename")
        raw_name, raw_value = raw_parameter.split("=", 1)
        name = raw_name.strip().lower()
        if (
            not _PARAMETER_NAME.fullmatch(name)
            or name not in {"filename", "filename*"}
            or name in parameters
        ):
            raise UploadError(400, "Content-Disposition is malformed", "invalid_filename")
        parameters[name] = raw_value.strip()

    plain_filename = (
        _validate_filename(_decode_quoted_filename(parameters["filename"]))
        if "filename" in parameters
        else None
    )
    extended_filename = (
        _validate_filename(_decode_extended_filename(parameters["filename*"]))
        if "filename*" in parameters
        else None
    )
    if "filename*" in parameters:
        filename = extended_filename
    elif "filename" in parameters:
        filename = plain_filename
    else:
        raise UploadError(400, "Content-Disposition filename is required", "invalid_filename")
    assert filename is not None
    return filename


def _split_parameters(value: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    quoted = False
    escaped = False
    for character in value:
        if escaped:
            current.append(character)
            escaped = False
        elif quoted and character == "\\":
            current.append(character)
            escaped = True
        elif character == '"':
            current.append(character)
            quoted = not quoted
        elif character == ";" and not quoted:
            parts.append("".join(current))
            current = []
        else:
            current.append(character)
    if quoted or escaped:
        raise UploadError(400, "Content-Disposition is malformed", "invalid_filename")
    parts.append("".join(current))
    return parts


def _decode_quoted_filename(value: str) -> str:
    if len(value) < 2 or value[0] != '"' or value[-1] != '"':
        raise UploadError(400, "filename must be a quoted string", "invalid_filename")
    result: list[str] = []
    index = 1
    while index < len(value) - 1:
        character = value[index]
        if character == "\\":
            index += 1
            if index >= len(value) - 1 or value[index] not in {'"', "\\"}:
                raise UploadError(400, "filename contains an invalid escape", "invalid_filename")
            character = value[index]
        result.append(character)
        index += 1
    return "".join(result)


def _decode_extended_filename(value: str) -> str:
    if value.startswith('"'):
        raise UploadError(400, "filename* must use RFC 5987 UTF-8 encoding", "invalid_filename")
    parts = value.split("'", 2)
    if len(parts) != 3 or parts[0].lower() != "utf-8" or not _RFC5987_LANGUAGE.fullmatch(parts[1]):
        raise UploadError(400, "filename* must use RFC 5987 UTF-8 encoding", "invalid_filename")
    encoded = parts[2]
    index = 0
    while index < len(encoded):
        if encoded[index] == "%":
            if index + 2 >= len(encoded) or not _HEX_PAIR.fullmatch(encoded[index + 1 : index + 3]):
                raise UploadError(
                    400, "filename* has malformed percent encoding", "invalid_filename"
                )
            index += 3
        else:
            if not _RFC5987_ATTR_CHAR.fullmatch(encoded[index]):
                raise UploadError(
                    400, "filename* contains an invalid character", "invalid_filename"
                )
            index += 1
    try:
        return unquote_to_bytes(encoded).decode("utf-8", "strict")
    except UnicodeDecodeError as error:
        raise UploadError(400, "filename* is not valid UTF-8", "invalid_filename") from error


def _validate_filename(value: str) -> str:
    filename = unicodedata.normalize("NFC", value)
    if not filename or not filename.strip() or filename in {".", ".."}:
        raise UploadError(400, "Filename is invalid", "invalid_filename")
    if "/" in filename or "\\" in filename:
        raise UploadError(400, "Filename must not contain path separators", "invalid_filename")
    if any(
        character == "\x00" or unicodedata.category(character) == "Cc" for character in filename
    ):
        raise UploadError(400, "Filename contains a control character", "invalid_filename")
    if len(filename) > 255 or len(filename.encode("utf-8")) > 255:
        raise UploadError(400, "Filename is too long", "invalid_filename")
    return filename


def _validate_media_type(filename: str, value: str) -> tuple[str, bool]:
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise UploadError(415, "Content-Type contains control characters", "media_type_denied")
    parts = [part.strip() for part in value.split(";")]
    declared = parts[0].lower()
    parameters: dict[str, str] = {}
    for parameter in parts[1:]:
        if "=" not in parameter:
            raise UploadError(415, "Content-Type is malformed", "media_type_denied")
        name, parameter_value = parameter.split("=", 1)
        name = name.strip().lower()
        if name in parameters:
            raise UploadError(415, "Content-Type is malformed", "media_type_denied")
        parameters[name] = parameter_value.strip().strip('"').lower()

    extension = filename.rsplit(".", 1)[-1].casefold() if "." in filename else ""
    if extension == "pdf" and declared == "application/pdf" and not parameters:
        return "application/pdf", False
    if extension == "txt" and declared == "text/plain" and _valid_text_parameters(parameters):
        return "text/plain", parameters.get("charset") == "us-ascii"
    if extension in {"md", "markdown"} and declared in {"text/markdown", "text/plain"}:
        if _valid_text_parameters(parameters):
            return "text/markdown", parameters.get("charset") == "us-ascii"
    raise UploadError(
        415, "Filename extension and Content-Type are not supported", "media_type_denied"
    )


def _valid_text_parameters(parameters: dict[str, str]) -> bool:
    return not parameters or (
        set(parameters) == {"charset"} and parameters["charset"] in {"utf-8", "us-ascii"}
    )


class _ContentValidator:
    def __init__(self, media_type: str, ascii_only: bool) -> None:
        self.media_type = media_type
        self.ascii_only = ascii_only
        self.prefix = bytearray()
        self.decoder = (
            codecs.getincrementaldecoder("utf-8")("strict")
            if media_type in {"text/plain", "text/markdown"}
            else None
        )

    def feed(self, chunk: bytes) -> None:
        if len(self.prefix) < 5:
            self.prefix.extend(chunk[: 5 - len(self.prefix)])
        if self.decoder is not None:
            if self.ascii_only and any(byte > 0x7F for byte in chunk):
                raise UploadError(
                    415,
                    "Text document does not match the declared US-ASCII charset",
                    "content_invalid",
                )
            try:
                decoded = self.decoder.decode(chunk, final=False)
            except UnicodeDecodeError as error:
                raise UploadError(
                    415, "Text document is not valid UTF-8", "content_invalid"
                ) from error
            if "\x00" in decoded:
                raise UploadError(415, "Text document contains NUL bytes", "content_invalid")

    def finish(self) -> None:
        if self.media_type == "application/pdf" and bytes(self.prefix) != b"%PDF-":
            raise UploadError(415, "PDF signature does not match Content-Type", "content_invalid")
        if self.decoder is not None:
            try:
                decoded = self.decoder.decode(b"", final=True)
            except UnicodeDecodeError as error:
                raise UploadError(
                    415, "Text document is not valid UTF-8", "content_invalid"
                ) from error
            if "\x00" in decoded:
                raise UploadError(415, "Text document contains NUL bytes", "content_invalid")


async def stream_upload(
    request: Request,
    writer: UploadWriter,
    metadata: UploadMetadata,
    max_bytes: int,
) -> StreamResult:
    """Stream, validate, hash, and durably close one request body."""

    validator = _ContentValidator(metadata.media_type, metadata.ascii_only)
    digest = hashlib.sha256()
    byte_size = 0
    try:
        async for chunk in request.stream():
            if not chunk:
                continue
            byte_size += len(chunk)
            if byte_size > max_bytes or byte_size > metadata.content_length:
                raise UploadError(
                    413, "Document exceeds the declared or configured size", "size_limit"
                )
            validator.feed(chunk)
            digest.update(chunk)
            await asyncio.to_thread(writer.write, chunk)
        if byte_size == 0:
            raise UploadError(400, "Document must not be empty", "empty_document")
        if byte_size != metadata.content_length:
            raise UploadError(
                400, "Content-Length does not match the request body", "length_mismatch"
            )
        validator.finish()
        await asyncio.to_thread(writer.finish)
    except ClientDisconnect as error:
        await asyncio.to_thread(writer.abort)
        raise UploadError(400, "Upload was interrupted", "client_disconnected") from error
    except BaseException:
        await asyncio.to_thread(writer.abort)
        raise
    return StreamResult(byte_size=byte_size, sha256=digest.hexdigest())
