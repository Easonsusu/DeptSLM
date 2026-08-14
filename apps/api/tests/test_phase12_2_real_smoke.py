"""Explicitly opt-in, offline Phase 12.2 candidate-adapter smoke coverage."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import sys
from pathlib import Path
from uuid import UUID

import pytest

SERVICE_ROOT = Path(__file__).parents[2] / "services" / "adapter-eval-runtime"
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))


def test_opt_in_real_candidate_adapter_smoke(monkeypatch: pytest.MonkeyPatch) -> None:
    if os.getenv("DEPTSLM_PHASE12_2_REAL_ADAPTER_SMOKE") != "1":
        pytest.skip("Phase 12.2 real adapter smoke test is explicitly opt-in")
    data_value = os.getenv("DEPTSLM_DATA_DIR", "")
    registry_value = os.getenv("DEPTSLM_PHASE12_2_ADAPTER_REGISTRY_ROOT", "")
    adapter_version = os.getenv("DEPTSLM_PHASE12_2_ADAPTER_VERSION", "")
    if not data_value or not registry_value or not adapter_version:
        pytest.skip("external model store and adapter registry fixture are required")
    data_dir = Path(data_value)
    registry_root = Path(registry_value)
    if not data_dir.is_dir() or not registry_root.is_dir():
        pytest.skip("external model store and adapter registry fixture are unavailable")
    for package, expected in (
        ("peft", "0.18.1"),
        ("transformers", "4.55.0"),
        ("safetensors", "0.7.0"),
    ):
        actual = importlib.metadata.version(package)
        assert actual == expected, f"{package} must be exactly {expected}"
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.setenv("HF_DATASETS_OFFLINE", "1")

    from deptslm_adapter_runtime.candidate_child import CandidateSession
    from deptslm_adapter_runtime.loader import verify_and_copy_adapter

    from app.adapter_contract import BASE_MODEL_ID, BASE_MODEL_REVISION
    from app.adapter_registry_domain import canonical_json_bytes
    from app.model_store import validate_generation_model_store
    from app.rag_domain import ANSWER_CONTRACT_VERSION, PROMPT_VERSION

    validate_generation_model_store(data_dir)
    manifest_path = registry_root / "manifest.json"
    if not manifest_path.is_file():
        pytest.skip("registry fixture must point at one exact adapter final directory")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = manifest["files"]
    department_id = UUID(manifest["department_id"])
    adapter_id = UUID(manifest["adapter_id"])
    publication_id = UUID(manifest["publication_attempt_id"])
    copied = verify_and_copy_adapter(
        registry_root.parent.parent,
        department_id=department_id,
        adapter_id=adapter_id,
        adapter_version=int(adapter_version),
        registry_publication_attempt_id=publication_id,
        registry_attempt_number=int(manifest["attempt_number"]),
        expected_manifest_sha256=hashlib.sha256(canonical_json_bytes(manifest)).hexdigest(),
        expected_config_sha256=files["adapter_config.json"]["sha256"],
        expected_config_byte_size=files["adapter_config.json"]["byte_size"],
        expected_model_sha256=files["adapter_model.safetensors"]["sha256"],
        expected_model_byte_size=files["adapter_model.safetensors"]["byte_size"],
    )
    try:
        assert copied.config_path.stat().st_size == files["adapter_config.json"]["byte_size"]
        assert copied.model_path.stat().st_size == files["adapter_model.safetensors"]["byte_size"]
        session = CandidateSession(data_dir, "real")
        try:
            result = session.generate(
                {
                    "target": "candidate",
                    "base_model_id": BASE_MODEL_ID,
                    "base_model_revision": BASE_MODEL_REVISION,
                    "department_id": str(department_id),
                    "adapter_id": str(adapter_id),
                    "adapter_version": int(adapter_version),
                    "registry_publication_attempt_id": str(publication_id),
                    "registry_attempt_number": int(manifest["attempt_number"]),
                    "registry_manifest_sha256": hashlib.sha256(
                        canonical_json_bytes(manifest)
                    ).hexdigest(),
                    "adapter_config_sha256": files["adapter_config.json"]["sha256"],
                    "adapter_config_byte_size": files["adapter_config.json"]["byte_size"],
                    "adapter_model_sha256": files["adapter_model.safetensors"]["sha256"],
                    "adapter_model_byte_size": files["adapter_model.safetensors"]["byte_size"],
                    "question": "What is approved?",
                    "evidence": [{"source_id": "S1", "text": "Approved."}],
                    "prompt_version": PROMPT_VERSION,
                    "answer_contract_version": ANSWER_CONTRACT_VERSION,
                    "seed": 7,
                }
            )
            assert result["status"] in {"answered", "insufficient_information"}
            assert session.model is not None
            assert session.model.__class__.__name__ == "PeftModel"
        finally:
            session.close()
    finally:
        copied.close()
