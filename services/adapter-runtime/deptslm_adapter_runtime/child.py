"""Secret-free child that verifies, loads, and serves one adapter target."""

from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

from app.adapter_contract import BASE_MODEL_ID, BASE_MODEL_REVISION
from app.adapter_runtime_contract import RuntimeTarget
from app.generation_contract import (
    GENERATION_DO_SAMPLE,
    GENERATION_MIN_P,
    GENERATION_TEMPERATURE,
    GENERATION_TOP_K,
    GENERATION_TOP_P,
    GenerationContractError,
    tokenize_generation_input,
)
from app.model_store import validate_generation_model_store
from app.rag_domain import (
    ANSWER_CONTRACT_VERSION,
    GENERATION_NEW_TOKEN_RESERVE,
    MAX_CHILD_FRAME_BYTES,
    PROMPT_VERSION,
    build_generation_messages,
    normalize_question,
    validate_generation_response,
    validate_safe_text,
)
from deptslm_adapter_runtime.loader import (
    AdapterRuntimeError,
    VerifiedAdapterCopy,
    load_adapter_model,
    verify_and_copy_adapter,
)
from deptslm_adapter_runtime.settings import AdapterRuntimeSettings

_HEADER = struct.Struct(">I")
_SHA = re.compile(r"^[0-9a-f]{64}$")
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


class ChildError(RuntimeError):
    def __init__(self, code: str = "adapter_runtime_unavailable") -> None:
        self.code = code
        super().__init__(code)


class Session:
    def __init__(self, settings: AdapterRuntimeSettings) -> None:
        self.settings = settings
        self.key: tuple[object, ...] | None = None
        self.loaded_target_fingerprint: str | None = None
        self.copy: VerifiedAdapterCopy | None = None
        self.model: Any = None
        self.tokenizer: Any = None

    def close(self) -> None:
        self.model = None
        self.tokenizer = None
        if self.copy is not None:
            try:
                self.copy.close()
            except OSError:
                pass
        self.copy = None
        self.key = None
        self.loaded_target_fingerprint = None

    def load_target(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Verify and load one target before the child accepts generation."""

        target = _validate_target(payload)
        key = _session_key(target)
        if key == self.key and self.loaded_target_fingerprint == target["target_fingerprint"]:
            return {
                "status": "target_ready",
                "loaded_target_fingerprint": self.loaded_target_fingerprint,
            }
        self.close()
        try:
            self.copy = verify_and_copy_adapter(
                self.settings.registry,
                department_id=UUID(target["department_id"]),
                adapter_id=UUID(target["adapter_id"]),
                adapter_version=target["adapter_version"],
                registry_publication_attempt_id=UUID(target["registry_publication_attempt_id"]),
                registry_attempt_number=target["registry_attempt_number"],
                expected_manifest_sha256=target["registry_manifest_sha256"],
                expected_config_sha256=target["adapter_config_sha256"],
                expected_config_byte_size=target["adapter_config_byte_size"],
                expected_model_sha256=target["adapter_model_sha256"],
                expected_model_byte_size=target["adapter_model_byte_size"],
            )
            if self.settings.provider == "real":
                # Resolve the reviewed generation store once.  The exact
                # validated directory is passed to both tokenizer and model
                # loaders; the shared model-cache root is never a load target.
                generation = validate_generation_model_store(self.settings.data_dir)
                self.tokenizer = _load_tokenizer(generation.path)
                self.model = load_adapter_model(
                    self.copy,
                    generation.path,
                    tokenizer_limit=self.tokenizer.model_max_length,
                )
            self.key = key
            self.loaded_target_fingerprint = target["target_fingerprint"]
            return {
                "status": "target_ready",
                "loaded_target_fingerprint": self.loaded_target_fingerprint,
            }
        except AdapterRuntimeError as error:
            self.close()
            raise ChildError(error.code) from error
        except Exception as error:
            self.close()
            raise ChildError("adapter_load_failed") from error

    def generate(self, payload: dict[str, Any]) -> dict[str, Any]:
        target = _validate_target(payload)
        key = _session_key(target)
        if key != self.key or self.loaded_target_fingerprint != target["target_fingerprint"]:
            raise ChildError("adapter_runtime_target_mismatch")
        question, evidence = _validate_generation(payload)
        if self.settings.provider == "fake":
            label = evidence[0]["source_id"]
            digest = hashlib.sha256(
                (payload["target_fingerprint"] + question).encode()
            ).hexdigest()[:8]
            value = {
                "status": "answered",
                "answer": f"The deployed adapter supports this answer {digest} [{label}].",
                "citations": [label],
            }
        else:
            value = self._real_generate(question, evidence)
        try:
            result = validate_generation_response(
                value, tuple(item["source_id"] for item in evidence)
            )
        except Exception as error:
            raise ChildError("adapter_runtime_target_mismatch") from error
        return {
            "status": result.status,
            "answer": result.answer,
            "citations": list(result.citations),
            "served_target_fingerprint": self.loaded_target_fingerprint,
        }

    def _real_generate(self, question: str, evidence: list[dict[str, str]]) -> dict[str, Any]:
        if self.model is None or self.tokenizer is None:
            raise ChildError("adapter_load_failed")
        try:
            messages = build_generation_messages(question, evidence)
            inputs = tokenize_generation_input(self.tokenizer, messages).to(self.model.device)
            outputs = self.model.generate(
                **inputs,
                max_new_tokens=GENERATION_NEW_TOKEN_RESERVE,
                do_sample=GENERATION_DO_SAMPLE,
                temperature=GENERATION_TEMPERATURE,
                top_p=GENERATION_TOP_P,
                top_k=GENERATION_TOP_K,
                min_p=GENERATION_MIN_P,
                pad_token_id=self.tokenizer.eos_token_id,
            )
            raw = self.tokenizer.decode(
                outputs[0][inputs["input_ids"].shape[-1] :], skip_special_tokens=True
            ).strip()
            if "<think" in raw.casefold() or "</think" in raw.casefold():
                raise ChildError("adapter_runtime_target_mismatch")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except ChildError:
            raise
        except GenerationContractError as error:
            raise ChildError("adapter_runtime_target_mismatch") from error
        except Exception as error:
            raise ChildError("adapter_runtime_unavailable") from error


def _validate_target(value: dict[str, Any]) -> dict[str, Any]:
    if (
        value.get("target") != "adapter"
        or value.get("runtime_contract_version") != "phase12-adapter-runtime-routing-v1"
    ):
        raise ChildError("adapter_runtime_target_mismatch")
    for field in _TARGET_FIELDS:
        if field not in value:
            raise ChildError("adapter_runtime_target_mismatch")
    if (
        value["base_model_id"] != BASE_MODEL_ID
        or value["base_model_revision"] != BASE_MODEL_REVISION
    ):
        raise ChildError("adapter_runtime_target_mismatch")
    if any(
        not isinstance(value[name], str)
        or (name.endswith("sha256") and _SHA.fullmatch(value[name]) is None)
        for name in (
            "registry_manifest_sha256",
            "adapter_config_sha256",
            "adapter_model_sha256",
        )
    ):
        raise ChildError("adapter_runtime_target_mismatch")
    try:
        uuid_fields = {
            "department_id",
            "deployment_id",
            "adapter_id",
            "review_id",
            "evaluation_id",
            "suite_id",
            "registry_attempt_id",
            "registry_publication_attempt_id",
            "registry_execution_scope_id",
            "dependency_id",
        }
        for name in uuid_fields:
            if UUID(value[name]).int == 0:
                raise ValueError
    except (TypeError, ValueError) as error:
        raise ChildError("adapter_runtime_target_mismatch") from error
    if any(
        type(value[name]) is not int or value[name] <= 0
        for name in (
            "adapter_version",
            "deployment_version",
            "deployment_row_version",
            "review_version",
            "evaluation_version",
            "suite_version",
            "registry_attempt_version",
            "registry_attempt_number",
            "adapter_config_byte_size",
            "adapter_model_byte_size",
            "dependency_version",
        )
    ):
        raise ChildError("adapter_runtime_target_mismatch")
    try:
        uuid_fields = {
            "department_id",
            "deployment_id",
            "adapter_id",
            "review_id",
            "evaluation_id",
            "suite_id",
            "registry_attempt_id",
            "registry_publication_attempt_id",
            "registry_execution_scope_id",
            "dependency_id",
        }
        target = RuntimeTarget(
            **{
                name: UUID(value[name])
                if name in uuid_fields and value[name] is not None
                else value[name]
                for name in _TARGET_FIELDS
            }
        )
    except (TypeError, ValueError) as error:
        raise ChildError("adapter_runtime_target_mismatch") from error
    if target.fingerprint != value.get("target_fingerprint"):
        raise ChildError("adapter_runtime_target_mismatch")
    return value


def _validate_generation(value: dict[str, Any]) -> tuple[str, list[dict[str, str]]]:
    if set(value) != {
        "operation",
        "target",
        "runtime_contract_version",
        "target_fingerprint",
        *_TARGET_FIELDS,
        "question",
        "evidence",
        "prompt_version",
        "answer_contract_version",
    }:
        raise ChildError("adapter_runtime_target_mismatch")
    try:
        question = normalize_question(value["question"])
        evidence = value["evidence"]
        if (
            value["prompt_version"] != PROMPT_VERSION
            or value["answer_contract_version"] != ANSWER_CONTRACT_VERSION
        ):
            raise ValueError
        if not isinstance(evidence, list) or not 1 <= len(evidence) <= 8:
            raise ValueError
        labels: list[str] = []
        total = 0
        for item in evidence:
            if not isinstance(item, dict) or set(item) != {"source_id", "text"}:
                raise ValueError
            label, text = item["source_id"], item["text"]
            validate_safe_text(text, field="evidence", max_chars=1200)
            if not isinstance(label, str) or label in labels or label != f"S{len(labels) + 1}":
                raise ValueError
            labels.append(label)
            total += len(text)
        if total > 6000:
            raise ValueError
        return question, evidence
    except (TypeError, ValueError) as error:
        raise ChildError("adapter_runtime_target_mismatch") from error


def _session_key(target: dict[str, Any]) -> tuple[object, ...]:
    return tuple(target[field] for field in _TARGET_FIELDS) + (target["target_fingerprint"],)


def _load_tokenizer(generation_path: Path):
    from transformers import AutoTokenizer

    return AutoTokenizer.from_pretrained(
        str(generation_path), local_files_only=True, trust_remote_code=False
    )


def _read_frame(stream, maximum: int):
    header = stream.read(_HEADER.size)
    if not header:
        return None
    if len(header) != _HEADER.size:
        raise ChildError()
    size = _HEADER.unpack(header)[0]
    if not 1 <= size <= maximum:
        raise ChildError("adapter_runtime_target_mismatch")
    try:
        value = json.loads(stream.read(size).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ChildError("adapter_runtime_target_mismatch") from error
    return value


def _write_frame(stream, value: object, maximum: int) -> None:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if not 1 <= len(raw) <= maximum:
        raise ChildError("adapter_runtime_target_mismatch")
    stream.write(_HEADER.pack(len(raw)) + raw)
    stream.flush()


def main() -> int:
    settings = AdapterRuntimeSettings.from_environment(require_token=False)
    if set(os.environ) - set(settings.child_environment()) - {"PATH", "PWD", "SHLVL", "_"}:
        return 2
    session = Session(settings)
    try:
        _write_frame(sys.stdout.buffer, {"ready": True}, 4096)
        while True:
            request = _read_frame(sys.stdin.buffer, MAX_CHILD_FRAME_BYTES)
            if request is None:
                return 0
            try:
                if (
                    not isinstance(request, dict)
                    or set(request) != {"operation", "target"}
                    or not isinstance(request["target"], dict)
                    or request["operation"] not in {"load_target", "generate"}
                ):
                    raise ChildError("adapter_runtime_target_mismatch")
                response = (
                    session.load_target(request["target"])
                    if request["operation"] == "load_target"
                    else session.generate(request["target"])
                )
                _write_frame(sys.stdout.buffer, response, 256 * 1024)
            except ChildError as error:
                _write_frame(sys.stdout.buffer, {"error": error.code}, 4096)
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
