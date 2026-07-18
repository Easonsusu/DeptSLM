"""Bounded embedding protocol and vector validation."""

from __future__ import annotations

import json
import math
import os
import select
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from app.vector_index_domain import EMBEDDING_DIMENSION

NORMALIZED_TOLERANCE = 1e-3
MAX_ABSOLUTE_VALUE = 10.0
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_SEQUENCE = (1 << 63) - 1
MAX_JSON_BYTES_PER_CHARACTER = 6


class EmbeddingError(RuntimeError):
    def __init__(self, code: str = "embedding_failed") -> None:
        self.code = code
        super().__init__(code)


def validate_vector(values: Sequence[float]) -> tuple[float, ...]:
    if len(values) != EMBEDDING_DIMENSION:
        raise EmbeddingError("invalid_embedding")
    vector: list[float] = []
    squared = 0.0
    for raw in values:
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise EmbeddingError("invalid_embedding")
        value = float(raw)
        if not math.isfinite(value) or abs(value) > MAX_ABSOLUTE_VALUE:
            raise EmbeddingError("invalid_embedding")
        vector.append(value)
        squared += value * value
    norm = math.sqrt(squared)
    if norm <= 1e-12 or abs(norm - 1.0) > NORMALIZED_TOLERANCE:
        raise EmbeddingError("invalid_embedding")
    return tuple(vector)


class EmbeddingProcess:
    def __init__(
        self,
        model_root: Path,
        *,
        provider: str,
        environment: str,
        timeout_seconds: int,
        max_batch_size: int,
        max_batch_characters: int,
        heartbeat: Callable[[], bool],
        should_stop: Callable[[], bool],
    ) -> None:
        if provider == "fake" and environment != "test":
            raise EmbeddingError("embedding_model_unavailable")
        request_limit = maximum_request_bytes(max_batch_size, max_batch_characters)
        child_environment = {
            "PATH": os.defpath,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1",
            "TOKENIZERS_PARALLELISM": "false",
            "HOME": "/nonexistent",
            "DEPTSLM_EMBEDDING_PROVIDER": provider,
            "ENVIRONMENT": environment,
        }
        self.process = subprocess.Popen(
            (
                sys.executable,
                "-I",
                str(Path(__file__).with_name("embedding_runner.py")),
                "--model-root",
                str(model_root),
            ),
            shell=False,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
            env=child_environment,
        )
        self.timeout_seconds = timeout_seconds
        self.max_batch_size = max_batch_size
        self.max_batch_characters = max_batch_characters
        self.max_request_bytes = request_limit
        self.heartbeat = heartbeat
        self.should_stop = should_stop
        self.sequence = 0
        self.buffer = bytearray()
        if self.process.stdin is None:
            self.close()
            raise EmbeddingError()
        os.set_blocking(self.process.stdin.fileno(), False)

    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        if not texts or self.process.stdin is None or self.process.stdout is None:
            raise EmbeddingError()
        if (
            len(texts) > self.max_batch_size
            or any(not isinstance(text, str) or not text for text in texts)
            or sum(len(text) for text in texts) > self.max_batch_characters
            or self.sequence > MAX_SEQUENCE
        ):
            raise EmbeddingError()
        sequence = self.sequence
        self.sequence += 1
        payload = (
            json.dumps(
                {"sequence": sequence, "texts": list(texts)},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        try:
            if len(payload) > self.max_request_bytes:
                raise EmbeddingError()
            deadline = time.monotonic() + self.timeout_seconds
            next_heartbeat = time.monotonic()
            next_heartbeat = self._write_request(payload, deadline, next_heartbeat)
            response = self._read_response(deadline, next_heartbeat)
            value = json.loads(response)
            if value.get("sequence") != sequence or set(value) != {
                "sequence",
                "vectors",
            }:
                raise ValueError
            vectors = value["vectors"]
            if not isinstance(vectors, list) or len(vectors) != len(texts):
                raise ValueError
            return [validate_vector(vector) for vector in vectors]
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            self.close()
            raise EmbeddingError("invalid_embedding") from error
        except BaseException:
            self.close()
            raise

    def _write_request(
        self, payload: bytes, deadline: float, next_heartbeat: float
    ) -> float:
        assert self.process.stdin is not None
        remaining = memoryview(payload)
        descriptor = self.process.stdin.fileno()
        while remaining:
            next_heartbeat = self._checkpoint(deadline, next_heartbeat)
            _, writable, _ = select.select([], [descriptor], [], 0.1)
            if not writable:
                continue
            try:
                written = os.write(descriptor, remaining)
            except BlockingIOError:
                continue
            except (BrokenPipeError, OSError) as error:
                raise EmbeddingError() from error
            if written <= 0:
                raise EmbeddingError()
            remaining = remaining[written:]
        return next_heartbeat

    def _read_response(self, deadline: float, next_heartbeat: float) -> bytes:
        assert self.process.stdout is not None
        while True:
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                response = bytes(self.buffer[:newline])
                del self.buffer[: newline + 1]
                return response
            if len(self.buffer) > MAX_RESPONSE_BYTES:
                raise EmbeddingError("invalid_embedding")
            next_heartbeat = self._checkpoint(deadline, next_heartbeat)
            readable, _, _ = select.select([self.process.stdout], [], [], 0.1)
            if not readable:
                if self.process.poll() is not None:
                    raise EmbeddingError()
                continue
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise EmbeddingError()
            self.buffer.extend(chunk)

    def _checkpoint(self, deadline: float, next_heartbeat: float) -> float:
        if self.should_stop():
            raise EmbeddingError("worker_shutdown")
        now = time.monotonic()
        if now >= deadline:
            raise EmbeddingError("embedding_timeout")
        if self.process.poll() is not None:
            raise EmbeddingError()
        if now >= next_heartbeat:
            if not self.heartbeat():
                raise EmbeddingError("claim_lost")
            return now + min(10, max(1, self.timeout_seconds // 4))
        return next_heartbeat

    def close(self) -> None:
        if self.process.poll() is not None:
            return
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
            self.process.wait(timeout=2)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass

    def __enter__(self) -> EmbeddingProcess:
        return self

    def __exit__(self, *_args) -> None:
        self.close()


def maximum_request_bytes(max_batch_size: int, max_batch_characters: int) -> int:
    """Worst-case UTF-8 JSON line size for the reviewed batch constraints."""
    if (
        isinstance(max_batch_size, bool)
        or not isinstance(max_batch_size, int)
        or max_batch_size < 1
        or isinstance(max_batch_characters, bool)
        or not isinstance(max_batch_characters, int)
        or max_batch_characters < 1
    ):
        raise EmbeddingError()
    empty_request = (
        json.dumps(
            {"sequence": MAX_SEQUENCE, "texts": [""] * max_batch_size},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    return len(empty_request) + max_batch_characters * MAX_JSON_BYTES_PER_CHARACTER
