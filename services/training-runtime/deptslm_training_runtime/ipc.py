"""Authenticated Unix-domain protocol with exact SCM_RIGHTS capability transfer."""

from __future__ import annotations

import hmac
import json
import os
import select
import socket
import stat
import struct
import threading
from collections.abc import Callable
from pathlib import Path

from .contract import MAX_HANDLES, MAX_IPC_FRAME_BYTES, canonical_json_bytes

_HEADER = struct.Struct("!I")


class RuntimeIpcError(RuntimeError):
    def __init__(self, code: str = "runtime_protocol_invalid") -> None:
        self.code = code
        super().__init__(code)


def authenticate_frame(
    payload: bytes,
    ancillary: list[tuple[int, int, bytes]],
    token: bytes,
    *,
    expected_uid: int | None = None,
) -> tuple[dict[str, object], tuple[int, int, int, int]]:
    if len(payload) < _HEADER.size:
        _reject_received(ancillary)
    size = _HEADER.unpack(payload[: _HEADER.size])[0]
    if not 1 <= size <= MAX_IPC_FRAME_BYTES or len(payload) != _HEADER.size + size:
        _reject_received(ancillary)
    try:
        envelope = json.loads(payload[_HEADER.size :].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        _close_received_fds(ancillary)
        raise RuntimeIpcError() from error
    if not isinstance(envelope, dict) or set(envelope) != {"nonce", "request", "mac"}:
        _reject_received(ancillary)
    nonce_hex, mac_hex, request = (
        envelope["nonce"],
        envelope["mac"],
        envelope["request"],
    )
    if (
        not isinstance(nonce_hex, str)
        or len(nonce_hex) != 64
        or not isinstance(mac_hex, str)
        or len(mac_hex) != 64
        or not isinstance(request, dict)
    ):
        _reject_received(ancillary, "runtime_auth_failed")
    try:
        nonce = bytes.fromhex(nonce_hex)
    except ValueError as error:
        _close_received_fds(ancillary)
        raise RuntimeIpcError("runtime_auth_failed") from error
    expected_mac = hmac.new(token, canonical_json_bytes(request) + nonce, "sha256").hexdigest()
    if not hmac.compare_digest(expected_mac, mac_hex):
        _reject_received(ancillary, "runtime_auth_failed")
    fds: list[int] = []
    for level, kind, data in ancillary:
        if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
            _reject_received(ancillary)
        if len(data) % struct.calcsize("=i"):
            _reject_received(ancillary)
        fds.extend(struct.unpack(f"={len(data) // struct.calcsize('=i')}i", data))
    if len(fds) != MAX_HANDLES or len(set(fds)) != MAX_HANDLES:
        _close_received_fds(ancillary)
        raise RuntimeIpcError()
    try:
        for descriptor in fds:
            metadata = os.fstat(descriptor)
            if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700:
                raise RuntimeIpcError()
            if expected_uid is not None and metadata.st_uid != expected_uid:
                raise RuntimeIpcError()
    except (OSError, RuntimeIpcError):
        _close_fds(fds)
        raise
    return request, (fds[0], fds[1], fds[2], fds[3])


class TrainingRuntimeServer:
    def __init__(
        self,
        socket_path: Path,
        token: str,
        handler: Callable[
            [dict[str, object], tuple[int, int, int, int], Callable[[], bool]],
            dict[str, object],
        ],
    ) -> None:
        if not socket_path.is_absolute() or len(token) < 32:
            raise RuntimeIpcError("runtime_auth_failed")
        self.socket_path = socket_path
        self.token = token.encode("utf-8")
        self.handler = handler

    def serve_once(self) -> None:
        _prepare_socket(self.socket_path)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            os.umask(0o077)
            server.bind(str(self.socket_path))
            os.chmod(self.socket_path, 0o600)
            server.listen(1)
            connection, _ = server.accept()
            with connection:
                self._serve_connection(connection)
        finally:
            server.close()
            try:
                self.socket_path.unlink()
            except FileNotFoundError:
                pass

    def _serve_connection(self, connection: socket.socket) -> None:
        connection.settimeout(120.0)
        maximum_ancillary = socket.CMSG_SPACE(MAX_HANDLES * struct.calcsize("=i")) + 64
        try:
            payload, ancillary = _receive_request_frame(connection, maximum_ancillary)
            _verify_peer_uid(connection)
            request, fds = authenticate_frame(
                payload, ancillary, self.token, expected_uid=os.getuid()
            )
            disconnected = threading.Event()
            watcher = threading.Thread(
                target=_watch_disconnect,
                args=(connection, disconnected),
                daemon=True,
            )
            watcher.start()
            try:
                result = self.handler(request, fds, disconnected.is_set)
                response = _encode_response(result)
                connection.sendall(response)
            finally:
                disconnected.set()
                watcher.join(timeout=1)
                _close_fds(fds)
        except RuntimeIpcError as error:
            try:
                connection.sendall(_encode_response({"error_code": error.code}))
            except OSError:
                pass
        except (OSError, ValueError, TypeError):
            try:
                connection.sendall(_encode_response({"error_code": "runtime_protocol_invalid"}))
            except OSError:
                pass


def _prepare_socket(path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = path.parent.stat()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or stat.S_IMODE(parent.st_mode) != 0o700
        or parent.st_uid != os.getuid()
    ):
        raise RuntimeIpcError("runtime_auth_failed")
    if path.exists() or path.is_symlink():
        metadata = path.lstat()
        if (
            not stat.S_ISSOCK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise RuntimeIpcError("runtime_auth_failed")
        path.unlink()


def _receive_request_frame(
    connection: socket.socket, maximum_ancillary: int
) -> tuple[bytes, list[tuple[int, int, bytes]]]:
    payload = bytearray()
    ancillary: list[tuple[int, int, bytes]] = []
    while len(payload) < _HEADER.size:
        chunk, received, flags, _address = connection.recvmsg(
            _HEADER.size - len(payload), maximum_ancillary
        )
        if flags & socket.MSG_CTRUNC:
            raise RuntimeIpcError()
        if not chunk:
            raise RuntimeIpcError("runtime_disconnected")
        payload.extend(chunk)
        ancillary.extend(received)
    length = _HEADER.unpack(payload[: _HEADER.size])[0]
    if not 1 <= length <= MAX_IPC_FRAME_BYTES:
        _close_received_fds(ancillary)
        raise RuntimeIpcError()
    while len(payload) < _HEADER.size + length:
        chunk, received, flags, _address = connection.recvmsg(
            _HEADER.size + length - len(payload), maximum_ancillary
        )
        if flags & socket.MSG_CTRUNC:
            _close_received_fds(ancillary + list(received))
            raise RuntimeIpcError()
        ancillary.extend(received)
        if not chunk:
            _close_received_fds(ancillary)
            raise RuntimeIpcError("runtime_disconnected")
        payload.extend(chunk)
    if len(payload) != _HEADER.size + length:
        _close_received_fds(ancillary)
        raise RuntimeIpcError()
    return bytes(payload), ancillary


def _verify_peer_uid(connection: socket.socket) -> None:
    option = getattr(socket, "SO_PEERCRED", None)
    if option is None:
        return
    try:
        raw = connection.getsockopt(socket.SOL_SOCKET, option, struct.calcsize("=3i"))
        _pid, uid, _gid = struct.unpack("=3i", raw)
    except (OSError, struct.error) as error:
        raise RuntimeIpcError("runtime_auth_failed") from error
    if uid != os.getuid():
        raise RuntimeIpcError("runtime_auth_failed")


def _close_received_fds(ancillary: list[tuple[int, int, bytes]]) -> None:
    size = struct.calcsize("=i")
    for level, kind, data in ancillary:
        if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS or len(data) % size:
            continue
        for descriptor in struct.unpack(f"={len(data) // size}i", data):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _reject_received(
    ancillary: list[tuple[int, int, bytes]], code: str = "runtime_protocol_invalid"
) -> None:
    _close_received_fds(ancillary)
    raise RuntimeIpcError(code)


def _watch_disconnect(connection: socket.socket, disconnected: threading.Event) -> None:
    while not disconnected.is_set():
        try:
            readable, _, _ = select.select([connection], [], [], 0.2)
            if readable:
                data = connection.recv(1, socket.MSG_PEEK)
                if data == b"":
                    disconnected.set()
                    return
        except OSError:
            disconnected.set()
            return


def _encode_response(value: dict[str, object]) -> bytes:
    payload = canonical_json_bytes(value)
    if not 1 <= len(payload) <= MAX_IPC_FRAME_BYTES:
        raise RuntimeIpcError()
    return _HEADER.pack(len(payload)) + payload


def _close_fds(fds: list[int] | tuple[int, ...]) -> None:
    for descriptor in fds:
        try:
            os.close(descriptor)
        except OSError:
            pass
