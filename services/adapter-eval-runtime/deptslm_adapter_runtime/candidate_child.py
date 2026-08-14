"""Bounded candidate model child for the private adapter-evaluation runtime."""

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

from app.adapter_contract import BASE_MODEL_REVISION
from app.rag_domain import (
    ANSWER_CONTRACT_VERSION,
    MAX_CHILD_FRAME_BYTES,
    PROMPT_VERSION,
    SOURCE_LABEL,
    build_generation_messages,
    normalize_question,
    validate_generation_response,
    validate_safe_text,
)
from deptslm_adapter_runtime.loader import (
    AdapterRuntimeError,
    VerifiedAdapterCopy,
    load_candidate_model,
    verify_and_copy_adapter,
)
from deptslm_adapter_runtime.settings import CHILD_ENVIRONMENT_NAMES

_HEADER = struct.Struct(">I")
_SHA = re.compile(r"\A[0-9a-f]{64}\Z")


class CandidateChildError(RuntimeError):
    def __init__(self, code: str = "candidate_adapter_load_failed") -> None:
        self.code = code
        super().__init__(code)


class CandidateSession:
    def __init__(self, data_dir: Path, provider: str) -> None:
        self.data_dir = data_dir
        self.provider = provider
        self.key: tuple[object, ...] | None = None
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

    def generate(self, payload: dict[str, Any]) -> dict[str, Any]:
        target = _target(payload, generation=True)
        key = tuple(target[name] for name in _TARGET_FIELDS)
        if key != self.key:
            self.close()
            self.key = key
            if self.provider == "real":
                try:
                    self.copy = verify_and_copy_adapter(
                        self.data_dir / "adapters" / "registry",
                        department_id=target["department_id"],
                        adapter_id=target["adapter_id"],
                        adapter_version=target["adapter_version"],
                        registry_publication_attempt_id=target["registry_publication_attempt_id"],
                        registry_attempt_number=target["registry_attempt_number"],
                        expected_manifest_sha256=target["registry_manifest_sha256"],
                        expected_config_sha256=target["adapter_config_sha256"],
                        expected_config_byte_size=target["adapter_config_byte_size"],
                        expected_model_sha256=target["adapter_model_sha256"],
                        expected_model_byte_size=target["adapter_model_byte_size"],
                    )
                    self.model = load_candidate_model(self.copy, self.data_dir / "model_cache")
                    self.tokenizer = _load_tokenizer(self.data_dir / "model_cache")
                except AdapterRuntimeError as error:
                    self.close()
                    raise CandidateChildError(error.code) from error
                except Exception as error:
                    self.close()
                    raise CandidateChildError() from error
        question = _question(payload.get("question"))
        evidence = _evidence(payload.get("evidence"))
        seed = payload.get("seed")
        if seed is not None:
            if isinstance(seed, bool) or not isinstance(seed, int):
                raise CandidateChildError("invalid_request")
            if not 0 <= seed < 1 << 63:
                raise CandidateChildError("invalid_request")
        if self.provider == "fake":
            labels = [item["source_id"] for item in evidence]
            if not labels:
                return {
                    "status": "insufficient_information",
                    "answer": "",
                    "citations": [],
                }
            digest = hashlib.sha256(f"candidate:{key}:{seed}:{question}".encode()).hexdigest()[:8]
            value = {
                "status": "answered",
                "answer": f"The candidate adapter supports this answer {digest} [{labels[0]}].",
                "citations": [labels[0]],
            }
        else:
            value = self._real_generate(question, evidence, seed)
        try:
            result = validate_generation_response(
                value, tuple(item["source_id"] for item in evidence)
            )
        except Exception as error:
            raise CandidateChildError("invalid_generation_response") from error
        return {
            "status": result.status,
            "answer": result.answer,
            "citations": list(result.citations),
        }

    def verify(self, payload: dict[str, Any]) -> dict[str, bool]:
        target = _target(payload, generation=False)
        if self.provider == "fake":
            return {"verified": True}
        try:
            verified = verify_and_copy_adapter(
                self.data_dir / "adapters" / "registry",
                department_id=target["department_id"],
                adapter_id=target["adapter_id"],
                adapter_version=target["adapter_version"],
                registry_publication_attempt_id=target["registry_publication_attempt_id"],
                registry_attempt_number=target["registry_attempt_number"],
                expected_manifest_sha256=target["registry_manifest_sha256"],
                expected_config_sha256=target["adapter_config_sha256"],
                expected_config_byte_size=target["adapter_config_byte_size"],
                expected_model_sha256=target["adapter_model_sha256"],
                expected_model_byte_size=target["adapter_model_byte_size"],
            )
            verified.close()
            return {"verified": True}
        except AdapterRuntimeError as error:
            raise CandidateChildError(error.code) from error

    def _real_generate(
        self, question: str, evidence: list[dict[str, str]], seed: int | None
    ) -> dict[str, Any]:
        if self.model is None or self.tokenizer is None:
            raise CandidateChildError("candidate_adapter_load_failed")
        try:
            import torch

            if seed is not None:
                torch.manual_seed(seed)
            messages = build_generation_messages(question, evidence)
            inputs = self.tokenizer.apply_chat_template(
                messages,
                tokenize=True,
                add_generation_prompt=True,
                return_dict=True,
                return_tensors="pt",
                enable_thinking=False,
                truncation=False,
            )
            inputs = inputs.to(self.model.device)
            outputs = self.model.generate(
                **inputs,
                max_new_tokens=512,
                do_sample=True,
                temperature=0.7,
                top_p=0.8,
                top_k=20,
                min_p=0.0,
                pad_token_id=self.tokenizer.eos_token_id,
            )
            generated = outputs[0][inputs["input_ids"].shape[-1] :]
            raw = self.tokenizer.decode(generated, skip_special_tokens=True).strip()
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("generation response is not an object")
            return value
        except CandidateChildError:
            raise
        except Exception as error:
            raise CandidateChildError("candidate_runtime_unavailable") from error


_TARGET_FIELDS = (
    "base_model_id",
    "base_model_revision",
    "department_id",
    "adapter_id",
    "adapter_version",
    "registry_publication_attempt_id",
    "registry_attempt_number",
    "registry_manifest_sha256",
    "adapter_config_sha256",
    "adapter_config_byte_size",
    "adapter_model_sha256",
    "adapter_model_byte_size",
)


def main() -> int:
    if not set(os.environ) <= CHILD_ENVIRONMENT_NAMES:
        return 2
    provider = os.getenv("DEPTSLM_ADAPTER_EVAL_PROVIDER", "")
    environment = os.getenv("ENVIRONMENT", "")
    if provider not in {"real", "fake"} or (provider == "fake" and environment != "test"):
        return 2
    if os.getenv("DEPTSLM_ADAPTER_EVAL_BASE_REVISION") != BASE_MODEL_REVISION:
        return 2
    root = Path(os.getenv("DEPTSLM_DATA_DIR", ""))
    session = CandidateSession(root, provider)
    try:
        _write_frame(sys.stdout.buffer, {"ready": True}, 4096)
        while True:
            request = _read_frame(sys.stdin.buffer, MAX_CHILD_FRAME_BYTES)
            if request is None:
                return 0
            try:
                if (
                    not isinstance(request, dict)
                    or set(request) != {"operation", "payload"}
                    or request["operation"] not in {"generate", "verify"}
                    or not isinstance(request["payload"], dict)
                ):
                    raise CandidateChildError("invalid_request")
                result = (
                    session.generate(request["payload"])
                    if request["operation"] == "generate"
                    else session.verify(request["payload"])
                )
                response = {"ok": True, "result": result}
            except CandidateChildError as error:
                response = {"ok": False, "code": error.code}
            except Exception:
                response = {"ok": False, "code": "candidate_runtime_unavailable"}
            _write_frame(sys.stdout.buffer, response, 256 * 1024)
    finally:
        session.close()


_GENERATION_FIELDS = {
    "question",
    "evidence",
    "prompt_version",
    "answer_contract_version",
    "seed",
}


def _target(payload: dict[str, Any], *, generation: bool) -> dict[str, Any]:
    target_fields = {"target", *_TARGET_FIELDS}
    allowed = target_fields | _GENERATION_FIELDS if generation else target_fields
    if (
        not target_fields.issubset(payload)
        or not set(payload) <= allowed
        or (not generation and set(payload) != target_fields)
        or payload.get("target") != "candidate"
    ):
        raise CandidateChildError("invalid_request")
    required_generation_fields = {
        "question",
        "evidence",
        "prompt_version",
        "answer_contract_version",
    }
    if generation and (
        not required_generation_fields <= set(payload)
        or payload["prompt_version"] != PROMPT_VERSION
        or payload["answer_contract_version"] != ANSWER_CONTRACT_VERSION
    ):
        raise CandidateChildError("invalid_request")
    result: dict[str, Any] = {}
    for name in _TARGET_FIELDS:
        value = payload.get(name)
        if name == "base_model_id" and value != "Qwen/Qwen3-0.6B":
            raise CandidateChildError("invalid_request")
        if name == "base_model_revision" and value != BASE_MODEL_REVISION:
            raise CandidateChildError("invalid_request")
        if name.endswith("_sha256"):
            if not isinstance(value, str) or _SHA.fullmatch(value) is None:
                raise CandidateChildError("invalid_request")
        elif name.endswith("_byte_size"):
            if type(value) is not int or value <= 0:
                raise CandidateChildError("invalid_request")
        elif name in {"department_id", "adapter_id", "registry_publication_attempt_id"}:
            if not isinstance(value, str):
                raise CandidateChildError("invalid_request")
            try:
                value = UUID(value)
            except ValueError as error:
                raise CandidateChildError("invalid_request") from error
            if value.int == 0:
                raise CandidateChildError("invalid_request")
        elif name == "adapter_version" and (type(value) is not int or value <= 0):
            raise CandidateChildError("invalid_request")
        elif name == "registry_attempt_number" and (type(value) is not int or value <= 0):
            raise CandidateChildError("invalid_request")
        result[name] = value
    return result


def _question(value: Any) -> str:
    try:
        result = normalize_question(value)
    except (TypeError, ValueError) as error:
        raise CandidateChildError("invalid_request") from error
    if result != value:
        raise CandidateChildError("invalid_request")
    return result


def _evidence(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        raise CandidateChildError("invalid_request")
    result: list[dict[str, str]] = []
    total = 0
    for item in value:
        if not isinstance(item, dict) or set(item) != {"source_id", "text"}:
            raise CandidateChildError("invalid_request")
        if (
            not isinstance(item["source_id"], str)
            or SOURCE_LABEL.fullmatch(item["source_id"]) is None
            or not isinstance(item["text"], str)
        ):
            raise CandidateChildError("invalid_request")
        try:
            validate_safe_text(item["text"], field="evidence", max_chars=1200)
        except (TypeError, ValueError):
            raise CandidateChildError("invalid_request") from None
        total += len(item["text"])
        result.append({"source_id": item["source_id"], "text": item["text"]})
    if [item["source_id"] for item in result] != [
        f"S{index}" for index in range(1, len(result) + 1)
    ] or total > 6000:
        raise CandidateChildError("invalid_request")
    return result


def _load_tokenizer(model_cache: Path) -> Any:
    from transformers import AutoTokenizer

    return AutoTokenizer.from_pretrained(
        "Qwen/Qwen3-0.6B",
        revision=BASE_MODEL_REVISION,
        cache_dir=str(model_cache),
        local_files_only=True,
        trust_remote_code=False,
    )


def _read_frame(stream: Any, maximum: int) -> Any | None:
    header = stream.read(_HEADER.size)
    if not header:
        return None
    if len(header) != _HEADER.size:
        raise CandidateChildError("invalid_request")
    size = _HEADER.unpack(header)[0]
    if not 1 <= size <= maximum:
        raise CandidateChildError("invalid_request")
    payload = stream.read(size)
    if len(payload) != size:
        raise CandidateChildError("invalid_request")
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CandidateChildError("invalid_request") from error


def _write_frame(stream: Any, value: Any, maximum: int) -> None:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if not 1 <= len(payload) <= maximum:
        raise CandidateChildError("candidate_runtime_unavailable")
    stream.write(_HEADER.pack(len(payload)))
    stream.write(payload)
    stream.flush()


if __name__ == "__main__":
    raise SystemExit(main())
