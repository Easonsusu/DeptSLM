"""Single-target, killable production adapter child supervisor."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import stat
import struct
import sys
from pathlib import Path
from typing import Any

from app.rag_domain import MAX_CHILD_FRAME_BYTES
from deptslm_adapter_runtime.settings import AdapterRuntimeSettings

_HEADER = struct.Struct(">I")
_MAX_RESPONSE_BYTES = 256 * 1024
STARTUP_TIMEOUT_SECONDS = 30
TARGET_LOAD_TIMEOUT_SECONDS = 300
GENERATION_TIMEOUT_SECONDS = 120
_TARGET_FIELDS = (
    "department_id",
    "target_kind",
    "deployment_id",
    "deployment_version",
    "deployment_row_version",
    "base_model_id",
    "base_model_revision",
    "adapter_id",
    "adapter_version",
    "review_id",
    "review_version",
    "evaluation_id",
    "evaluation_version",
    "suite_id",
    "suite_version",
    "registry_attempt_id",
    "registry_attempt_version",
    "registry_publication_attempt_id",
    "registry_attempt_number",
    "registry_execution_scope_id",
    "registry_manifest_sha256",
    "adapter_config_sha256",
    "adapter_config_byte_size",
    "adapter_model_sha256",
    "adapter_model_byte_size",
    "dependency_id",
    "dependency_version",
)
_KEY_FIELDS = (
    "department_id",
    "adapter_id",
    "adapter_version",
    "base_model_revision",
    *(
        field
        for field in _TARGET_FIELDS
        if field
        not in {
            "department_id",
            "adapter_id",
            "adapter_version",
            "base_model_revision",
        }
    ),
    "runtime_contract_version",
)
_SAFE_CHILD_ERRORS = frozenset(
    {
        "adapter_runtime_unavailable",
        "adapter_runtime_timeout",
        "adapter_load_failed",
        "adapter_runtime_target_mismatch",
    }
)


class AdapterRuntimeSupervisorError(RuntimeError):
    def __init__(self, code: str = "adapter_runtime_unavailable") -> None:
        self.code = code
        super().__init__(code)


class AdapterRuntimeSupervisor:
    def __init__(
        self,
        settings: AdapterRuntimeSettings,
        *,
        timeout_seconds: float = GENERATION_TIMEOUT_SECONDS,
        startup_timeout_seconds: float = STARTUP_TIMEOUT_SECONDS,
        target_load_timeout_seconds: float = TARGET_LOAD_TIMEOUT_SECONDS,
        generation_timeout_seconds: float | None = None,
    ) -> None:
        self._settings = settings
        self._startup_timeout = startup_timeout_seconds
        self._target_load_timeout = target_load_timeout_seconds
        self._generation_timeout = (
            timeout_seconds if generation_timeout_seconds is None else generation_timeout_seconds
        )
        self._process: asyncio.subprocess.Process | None = None
        self._target_key: tuple[object, ...] | None = None
        self._target_ready = False
        self._capacity = asyncio.Lock()
        self._lifecycle = asyncio.Lock()
        self._closed = False

    @property
    def ready(self) -> bool:
        return not self._closed and self._process is not None and self._process.returncode is None

    @property
    def target_ready(self) -> bool:
        return self.ready and self._target_ready

    async def start(self) -> None:
        async with self._lifecycle:
            if not self.ready:
                await self._start()

    async def close(self) -> None:
        self._closed = True
        async with self._lifecycle:
            await self._terminate()

    async def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._closed or self._capacity.locked():
            raise AdapterRuntimeSupervisorError()
        await self._capacity.acquire()
        try:
            requested = payload.get("target")
            if not isinstance(requested, dict):
                raise AdapterRuntimeSupervisorError("adapter_runtime_target_mismatch")
            authority = _target_authority(requested)
            supplied_authority = payload.get("target_authority")
            if supplied_authority is not None and supplied_authority != authority:
                raise AdapterRuntimeSupervisorError("adapter_runtime_target_mismatch")
            key = _target_key({"target": authority})
            async with self._lifecycle:
                if not self.ready or (
                    self._target_key is not None
                    and (key != self._target_key or not self._target_ready)
                ):
                    await self._terminate()
                    await self._start()
                process = self._process
            if process is None or process.stdin is None or process.stdout is None:
                raise AdapterRuntimeSupervisorError()
            if key != self._target_key or not self._target_ready:
                await self._load_target(process, authority)
                async with self._lifecycle:
                    if self._closed or self._process is not process:
                        raise AdapterRuntimeSupervisorError()
                    self._target_key = key
                    self._target_ready = True
            frame = _encode_frame({"operation": "generate", "target": requested})
            try:
                response = await self._exchange(
                    process,
                    frame,
                    timeout_seconds=self._generation_timeout,
                )
                if isinstance(response, dict) and set(response) == {"error"}:
                    code = response.get("error")
                    if code in _SAFE_CHILD_ERRORS:
                        raise AdapterRuntimeSupervisorError(code)
                expected = authority.get("target_fingerprint")
                if (
                    not isinstance(response, dict)
                    or set(response)
                    != {
                        "status",
                        "answer",
                        "citations",
                        "served_target_fingerprint",
                    }
                    or response.get("served_target_fingerprint") != expected
                ):
                    raise AdapterRuntimeSupervisorError("adapter_runtime_target_mismatch")
                return response
            except asyncio.CancelledError:
                async with self._lifecycle:
                    await self._terminate()
                raise
            except AdapterRuntimeSupervisorError:
                async with self._lifecycle:
                    await self._terminate()
                raise
            except Exception as error:
                async with self._lifecycle:
                    await self._terminate()
                raise AdapterRuntimeSupervisorError() from error
        except AdapterRuntimeSupervisorError:
            async with self._lifecycle:
                await self._terminate()
            raise
        except asyncio.CancelledError:
            async with self._lifecycle:
                await self._terminate()
            raise
        finally:
            self._capacity.release()

    async def _start(self) -> None:
        if self._closed:
            raise AdapterRuntimeSupervisorError()
        try:
            self._process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "deptslm_adapter_runtime.child",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=self._settings.child_environment(),
                close_fds=True,
                start_new_session=True,
            )
            if self._process.stdout is None:
                raise AdapterRuntimeSupervisorError("adapter_load_failed")
            async with asyncio.timeout(self._startup_timeout):
                ready = await _read_frame(self._process.stdout, 4096)
            if ready != {"ready": True}:
                raise AdapterRuntimeSupervisorError("adapter_load_failed")
            self._target_key = None
            self._target_ready = False
        except TimeoutError as error:
            await self._terminate()
            raise AdapterRuntimeSupervisorError("adapter_runtime_timeout") from error
        except Exception:
            await self._terminate()
            raise

    async def _load_target(
        self, process: asyncio.subprocess.Process, authority: dict[str, Any]
    ) -> None:
        response = await self._exchange(
            process,
            _encode_frame({"operation": "load_target", "target": authority}),
            timeout_seconds=self._target_load_timeout,
        )
        if isinstance(response, dict) and set(response) == {"error"}:
            code = response.get("error")
            if code in _SAFE_CHILD_ERRORS:
                async with self._lifecycle:
                    await self._terminate()
                raise AdapterRuntimeSupervisorError(code)
        if (
            not isinstance(response, dict)
            or set(response)
            != {
                "status",
                "loaded_target_fingerprint",
            }
            or response.get("status") != "target_ready"
        ):
            async with self._lifecycle:
                await self._terminate()
            raise AdapterRuntimeSupervisorError("adapter_runtime_target_mismatch")
        if response.get("loaded_target_fingerprint") != authority.get("target_fingerprint"):
            async with self._lifecycle:
                await self._terminate()
            raise AdapterRuntimeSupervisorError("adapter_runtime_target_mismatch")

    async def _exchange(
        self,
        process: asyncio.subprocess.Process,
        frame: bytes,
        *,
        timeout_seconds: float,
    ) -> Any:
        if process.stdin is None or process.stdout is None:
            raise AdapterRuntimeSupervisorError()
        try:
            async with asyncio.timeout(timeout_seconds):
                process.stdin.write(frame)
                await process.stdin.drain()
                return await _read_frame(process.stdout, _MAX_RESPONSE_BYTES)
        except TimeoutError as error:
            async with self._lifecycle:
                await self._terminate()
            raise AdapterRuntimeSupervisorError("adapter_runtime_timeout") from error

    async def _terminate(self) -> None:
        process, self._process = self._process, None
        self._target_key = None
        self._target_ready = False
        if process is None:
            return
        if process.stdin is not None:
            process.stdin.close()
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except TimeoutError:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
        else:
            await process.wait()
        _cleanup_private_copies(process.pid)


def _cleanup_private_copies(pid: int) -> None:
    """Remove only this child's private copies after forced termination."""

    scratch = Path("/tmp/adapter-runtime")
    try:
        root = scratch.lstat()
        if (
            stat.S_ISLNK(root.st_mode)
            or not stat.S_ISDIR(root.st_mode)
            or root.st_uid != os.geteuid()
            or stat.S_IMODE(root.st_mode) != 0o700
        ):
            return
        entries = tuple(scratch.iterdir())
    except OSError:
        return
    prefix = f"target-{pid}-"
    for entry in entries:
        if not entry.name.startswith(prefix):
            continue
        try:
            metadata = entry.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o700
            ):
                continue
            shutil.rmtree(entry)
        except OSError:
            continue


def _target_key(payload: dict[str, Any]) -> tuple[object, ...]:
    authority = payload.get("target")
    if not isinstance(authority, dict):
        authority = payload
    return tuple(authority.get(field) for field in _KEY_FIELDS) + (
        authority.get("target_fingerprint"),
    )


def _target_authority(value: dict[str, Any]) -> dict[str, Any]:
    """Strip generation content so target loading receives authority only."""

    return (
        {"target": "adapter"}
        | {field: value.get(field) for field in _TARGET_FIELDS}
        | {
            "runtime_contract_version": value.get("runtime_contract_version"),
            "target_fingerprint": value.get("target_fingerprint"),
        }
    )


def _encode_frame(value: dict[str, Any]) -> bytes:
    try:
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, UnicodeEncodeError) as error:
        raise AdapterRuntimeSupervisorError("adapter_runtime_target_mismatch") from error
    if not 1 <= len(raw) <= MAX_CHILD_FRAME_BYTES:
        raise AdapterRuntimeSupervisorError("adapter_runtime_target_mismatch")
    return _HEADER.pack(len(raw)) + raw


async def _read_frame(reader: asyncio.StreamReader, maximum: int) -> Any:
    try:
        header = await reader.readexactly(_HEADER.size)
        size = _HEADER.unpack(header)[0]
        if not 1 <= size <= maximum:
            raise AdapterRuntimeSupervisorError()
        return json.loads((await reader.readexactly(size)).decode("utf-8"))
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        struct.error,
        asyncio.IncompleteReadError,
    ) as error:
        raise AdapterRuntimeSupervisorError() from error
