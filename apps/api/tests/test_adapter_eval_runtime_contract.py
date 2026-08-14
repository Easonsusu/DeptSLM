import json
import os
import struct
import subprocess
import sys
from pathlib import Path
from uuid import uuid4


def test_adapter_eval_runtime_is_private_and_exactly_pinned():
    root = Path(__file__).parents[3] / "services" / "adapter-eval-runtime"
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    assert '"peft==0.18.1"' in pyproject
    assert '"transformers==4.55.0"' in pyproject
    assert '"safetensors==0.7.0"' in pyproject
    assert "EXPOSE 8011" in dockerfile
    assert "8011:8011" not in dockerfile
    assert "DATABASE_URL" in (root / "deptslm_adapter_runtime" / "settings.py").read_text(
        encoding="utf-8"
    )
    assert "candidate_adapter_load_failed" in (
        root / "deptslm_adapter_runtime" / "loader.py"
    ).read_text(encoding="utf-8")


def test_fake_candidate_child_returns_contract_response_without_model_download(tmp_path):
    root = Path(__file__).parents[3]
    data_dir = tmp_path / "data"
    (data_dir / "model_cache").mkdir(parents=True)
    (data_dir / "adapters" / "registry").mkdir(parents=True)
    env = {
        "DEPTSLM_DATA_DIR": str(data_dir),
        "DEPTSLM_ADAPTER_EVAL_PROVIDER": "fake",
        "DEPTSLM_ADAPTER_EVAL_BASE_REVISION": "c1899de289a04d12100db370d81485cdf75e47ca",
        "ENVIRONMENT": "test",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "TOKENIZERS_PARALLELISM": "false",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONPATH": os.pathsep.join(
            (
                str(root / "apps" / "api"),
                str(root / "services" / "rag-worker"),
                str(root / "services" / "adapter-eval-runtime"),
            )
        ),
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "deptslm_adapter_runtime.candidate_child"],
        cwd=root,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None and process.stdin is not None
    try:
        assert _read_frame(process.stdout) == {"ready": True}
        value = {
            "operation": "generate",
            "payload": {
                "question": "What is approved?",
                "evidence": [{"source_id": "S1", "text": "Approved."}],
                "prompt_version": "phase7-grounded-answer-prompt-v1",
                "answer_contract_version": "phase7-grounded-answer-v1",
                "seed": 7,
                "target": "candidate",
                "base_model_id": "Qwen/Qwen3-0.6B",
                "base_model_revision": "c1899de289a04d12100db370d81485cdf75e47ca",
                "department_id": str(uuid4()),
                "adapter_id": str(uuid4()),
                "adapter_version": 1,
                "registry_publication_attempt_id": str(uuid4()),
                "registry_attempt_number": 1,
                "registry_manifest_sha256": "a" * 64,
                "adapter_config_sha256": "b" * 64,
                "adapter_config_byte_size": 1,
                "adapter_model_sha256": "c" * 64,
                "adapter_model_byte_size": 1,
            },
        }
        _write_frame(process.stdin, value)
        response = _read_frame(process.stdout)
        assert response["ok"] is True
        assert response["result"]["status"] == "answered"
        assert response["result"]["citations"] == ["S1"]
        _write_frame(
            process.stdin,
            {
                "operation": "verify",
                "payload": {
                    key: value
                    for key, value in value["payload"].items()
                    if key
                    not in {
                        "question",
                        "evidence",
                        "prompt_version",
                        "answer_contract_version",
                        "seed",
                    }
                },
            },
        )
        verification = _read_frame(process.stdout)
        assert verification == {"ok": True, "result": {"verified": True}}
        invalid = dict(value["payload"])
        invalid["model_path"] = "/outside/runtime"
        _write_frame(
            process.stdin,
            {"operation": "generate", "payload": invalid},
        )
        assert _read_frame(process.stdout) == {"ok": False, "code": "invalid_request"}
    finally:
        process.terminate()
        process.wait(timeout=5)


def _write_frame(stream, value):
    payload = json.dumps(value, separators=(",", ":")).encode()
    stream.write(struct.pack(">I", len(payload)) + payload)
    stream.flush()


def _read_frame(stream):
    header = stream.read(4)
    assert len(header) == 4
    size = struct.unpack(">I", header)[0]
    return json.loads(stream.read(size))
