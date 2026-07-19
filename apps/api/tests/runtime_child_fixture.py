"""Controlled model-child fixture for supervisor failure-path tests."""

from __future__ import annotations

import json
import os
import struct
import sys
import time

HEADER = struct.Struct(">I")


def _read():
    header = sys.stdin.buffer.read(HEADER.size)
    if not header:
        return None
    size = HEADER.unpack(header)[0]
    payload = sys.stdin.buffer.read(size)
    return json.loads(payload.decode("utf-8"))


def _write(value) -> None:
    payload = json.dumps(value, separators=(",", ":")).encode()
    sys.stdout.buffer.write(HEADER.pack(len(payload)) + payload)
    sys.stdout.buffer.flush()


def main() -> int:
    mode = sys.argv[1]
    if mode == "startup_exit":
        return 3
    if mode == "startup_hang":
        time.sleep(float(sys.argv[2]))
    if mode == "startup_context_mismatch":
        _write({"ready": False, "code": "model_context_mismatch"})
        return 3
    _write({"ready": True})
    operation_count = 0
    while True:
        request = _read()
        if request is None:
            return 0
        operation = request.get("operation")
        operation_count += 1
        if mode == "exit":
            return 3
        if mode == "malformed":
            sys.stdout.buffer.write(HEADER.pack(1) + b"{")
            sys.stdout.buffer.flush()
            continue
        if mode == "oversized":
            sys.stdout.buffer.write(HEADER.pack(300_000))
            sys.stdout.buffer.flush()
            continue
        if mode == "hang" or mode == f"hang_{operation}":
            while True:
                time.sleep(1)
        if mode == "recoverable_once" and operation_count == 1:
            _write({"ok": False, "code": "model_input_too_large"})
            continue
        if mode == "fatal_once" and operation_count == 1:
            _write({"ok": False, "code": "model_operation_failed"})
            continue
        if mode == "environment":
            result = {"names": sorted(os.environ), "values": dict(os.environ)}
        elif operation == "query_embedding":
            result = {"vector": [1.0]}
        else:
            result = {
                "status": "answered",
                "answer": "Synthetic [S1].",
                "citations": ["S1"],
            }
        _write({"ok": True, "result": result})


if __name__ == "__main__":
    raise SystemExit(main())
