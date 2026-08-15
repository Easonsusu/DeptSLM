"""Private content-free Phase 12.2 result artifacts."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from app.adapter_evaluation_domain import (
    ADAPTER_EVALUATION_ARTIFACT_CONTRACT_VERSION,
    ADAPTER_EVALUATION_GATE_POLICY_VERSION,
    ADAPTER_EVALUATION_METRIC_CONTRACT_VERSION,
    ADAPTER_EVALUATION_RUNNER_CONTRACT_VERSION,
    ADAPTER_EVALUATION_SEED_POLICY_VERSION,
    safe_error_code,
)
from app.authorization import DepartmentScope
from app.evaluation_artifacts import (
    STAGE_OWNERSHIP_MARKER,
    ArtifactDigest,
    _ensure_private_directory,
    _real_directory,
    _safe_remove_tree,
    _verify_expected_files,
    _write_bytes,
    _write_lines,
    _write_stage_ownership_marker,
    canonical_json_bytes,
)
from app.evaluation_domain import EvaluationContractError

ADAPTER_EVALUATION_RESULT_FILES = frozenset({"manifest.json", "summary.json", "case_results.jsonl"})

_METRIC_NAMES = frozenset(
    {
        "retrieval_recall_at_5",
        "retrieval_recall_at_10",
        "retrieval_recall_at_20",
        "retrieval_mrr_at_20",
        "answer_status_accuracy",
        "citation_precision",
        "citation_recall",
        "normalized_exact_match",
        "character_f1",
        "invalid_contract_rate",
    }
)
_MANIFEST_KEYS = frozenset(
    {
        "artifact_contract_version",
        "department_id",
        "evaluation_id",
        "adapter_id",
        "adapter_version",
        "suite_id",
        "publication_attempt_id",
        "attempt_number",
        "base_seed",
        "baseline_lane_id",
        "candidate_lane_id",
        "base_model_id",
        "base_model_revision",
        "registry_publication_attempt_id",
        "registry_attempt_number",
        "registry_manifest_sha256",
        "adapter_config_sha256",
        "adapter_config_byte_size",
        "adapter_model_sha256",
        "adapter_model_byte_size",
        "runner_contract_version",
        "metric_contract_version",
        "gate_policy_version",
        "seed_policy_version",
        "code_revision",
    }
)
_SUMMARY_KEYS = frozenset(
    {
        "baseline_metrics",
        "candidate_metrics",
        "metric_deltas",
        "baseline_gate_status",
        "candidate_gate_status",
        "baseline_failed_gate_count",
        "candidate_failed_gate_count",
    }
)
_CASE_KEYS = frozenset(
    {
        "target",
        "case_id",
        "expected_status",
        "actual_status",
        "relevant_chunk_count",
        "retrieval_candidate_count",
        "retrieved_relevant_at_5",
        "retrieved_relevant_at_10",
        "retrieved_relevant_at_20",
        "reciprocal_rank_at_20",
        "status_correct",
        "cited_count",
        "cited_relevant_count",
        "citation_precision",
        "citation_recall",
        "normalized_exact_match",
        "character_f1",
        "answer_contract_valid",
        "case_gate_passed",
        "error_code",
    }
)


@dataclass(frozen=True, slots=True)
class AdapterEvaluationStagedArtifact:
    path: Path
    final_path: Path
    files: tuple[tuple[str, ArtifactDigest], ...]
    manifest: dict[str, object]


@dataclass(frozen=True, slots=True)
class AdapterEvaluationPublishedArtifact:
    path: Path
    files: tuple[tuple[str, ArtifactDigest], ...]


class AdapterEvaluationArtifactStore:
    """Keep adapter-evaluation output in its own external namespace."""

    def __init__(self, data_dir: Path) -> None:
        root = _real_directory(data_dir, writable=False)
        eval_root = _real_directory(root / "eval_results", writable=True)
        self.final_root = _ensure_private_directory(eval_root, "adapter_runs")
        staging_root = _ensure_private_directory(eval_root, "staging")
        self.staging_root = _ensure_private_directory(staging_root, "adapter_runs")

    def stage_run(
        self,
        scope: DepartmentScope,
        evaluation_id: UUID,
        publication_attempt_id: UUID,
        *,
        manifest: dict[str, object],
        summary: dict[str, object],
        case_rows: list[dict[str, object]],
    ) -> AdapterEvaluationStagedArtifact:
        _validate_ids(scope, evaluation_id, publication_attempt_id)
        manifest = _prepare_manifest(manifest, scope, evaluation_id, publication_attempt_id)
        _assert_content_free(manifest)
        _validate_summary(summary)
        _assert_content_free(summary)
        for row in case_rows:
            _validate_case_row(row)
            _assert_content_free(row)
        parent = _ensure_private_directory(self.staging_root, str(scope.value))
        parent = _ensure_private_directory(parent, str(evaluation_id))
        stage = parent / str(publication_attempt_id)
        try:
            os.mkdir(stage, 0o700)
        except FileExistsError as error:
            raise EvaluationContractError("result_publication_failed") from error
        try:
            os.chmod(stage, 0o700)
            _write_stage_ownership_marker(stage)
            summary_digest = _write_bytes(
                stage, "summary.json", canonical_json_bytes(summary) + b"\n"
            )
            cases_digest = _write_lines(
                stage,
                "case_results.jsonl",
                (canonical_json_bytes(row) + b"\n" for row in case_rows),
            )
            value = dict(manifest)
            value["files"] = {
                "summary.json": {
                    "sha256": summary_digest.sha256,
                    "byte_size": summary_digest.byte_size,
                },
                "case_results.jsonl": {
                    "sha256": cases_digest.sha256,
                    "byte_size": cases_digest.byte_size,
                },
            }
            manifest_digest = _write_bytes(
                stage, "manifest.json", canonical_json_bytes(value) + b"\n"
            )
            files = (
                ("manifest.json", manifest_digest),
                ("summary.json", summary_digest),
                ("case_results.jsonl", cases_digest),
            )
            _verify_expected_files(
                stage,
                ADAPTER_EVALUATION_RESULT_FILES,
                dict(files),
                internal_files=frozenset({STAGE_OWNERSHIP_MARKER}),
            )
            final_parent = _ensure_private_directory(self.final_root, str(scope.value))
            value["files"] = {
                "summary.json": {
                    "sha256": summary_digest.sha256,
                    "byte_size": summary_digest.byte_size,
                },
                "case_results.jsonl": {
                    "sha256": cases_digest.sha256,
                    "byte_size": cases_digest.byte_size,
                },
            }
            return AdapterEvaluationStagedArtifact(
                stage, final_parent / str(evaluation_id), files, value
            )
        except Exception:
            _safe_remove_tree(stage)
            raise

    def publish(
        self, staged: AdapterEvaluationStagedArtifact
    ) -> AdapterEvaluationPublishedArtifact:
        _verify_expected_files(
            staged.path,
            ADAPTER_EVALUATION_RESULT_FILES,
            dict(staged.files),
            internal_files=frozenset({STAGE_OWNERSHIP_MARKER}),
        )
        marker = staged.path / STAGE_OWNERSHIP_MARKER
        try:
            marker.unlink()
        except FileNotFoundError:
            pass
        _verify_expected_files(staged.path, ADAPTER_EVALUATION_RESULT_FILES, dict(staged.files))
        final = staged.final_path
        if final.exists():
            raise EvaluationContractError("result_publication_failed")
        try:
            os.rename(staged.path, final)
            os.chmod(final, 0o700)
            verified = _verify_expected_files(
                final, ADAPTER_EVALUATION_RESULT_FILES, dict(staged.files)
            )
            _validate_manifest_file(final / "manifest.json", staged.manifest, verified)
            return AdapterEvaluationPublishedArtifact(final, tuple(sorted(verified.items())))
        except EvaluationContractError:
            raise
        except OSError as error:
            raise EvaluationContractError("result_publication_failed") from error

    def cleanup_stage(
        self, scope: DepartmentScope, evaluation_id: UUID, publication_attempt_id: UUID
    ) -> None:
        _validate_ids(scope, evaluation_id, publication_attempt_id)
        path = (
            self.staging_root / str(scope.value) / str(evaluation_id) / str(publication_attempt_id)
        )
        _safe_remove_tree(path)

    def verify_published(
        self,
        scope: DepartmentScope,
        evaluation_id: UUID,
        publication_attempt_id: UUID,
        *,
        expected_manifest: dict[str, object],
        expected_files: dict[str, ArtifactDigest],
    ) -> AdapterEvaluationPublishedArtifact:
        """Reverify one exact final result before PostgreSQL success."""

        _validate_ids(scope, evaluation_id, publication_attempt_id)
        final = self.final_root / str(scope.value) / str(evaluation_id)
        try:
            verified = _verify_expected_files(
                final,
                ADAPTER_EVALUATION_RESULT_FILES,
                expected_files,
            )
            _validate_manifest_file(final / "manifest.json", expected_manifest, verified)
            return AdapterEvaluationPublishedArtifact(final, tuple(sorted(verified.items())))
        except EvaluationContractError:
            raise
        except OSError as error:
            raise EvaluationContractError("result_publication_failed") from error

    def cleanup_published(
        self, scope: DepartmentScope, evaluation_id: UUID, publication_attempt_id: UUID
    ) -> None:
        """Remove only a failed final whose manifest names this exact attempt."""

        _validate_ids(scope, evaluation_id, publication_attempt_id)
        path = self.final_root / str(scope.value) / str(evaluation_id)
        if not path.exists():
            return
        if path.is_symlink() or not path.is_dir():
            raise EvaluationContractError("result_publication_failed")
        try:
            raw = (path / "manifest.json").read_bytes()
            manifest = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise EvaluationContractError("result_publication_failed") from error
        if not isinstance(manifest, dict) or manifest.get("publication_attempt_id") != str(
            publication_attempt_id
        ):
            raise EvaluationContractError("result_publication_failed")
        _safe_remove_tree(path)


def _validate_ids(
    scope: DepartmentScope, evaluation_id: UUID, publication_attempt_id: UUID
) -> None:
    if not isinstance(scope, DepartmentScope) or not all(
        isinstance(value, UUID) and value.int != 0
        for value in (evaluation_id, publication_attempt_id)
    ):
        raise EvaluationContractError("result_publication_failed")


def _assert_content_free(value: object) -> None:
    forbidden = {
        "question",
        "accepted_answer",
        "generated_answer",
        "prompt",
        "evidence",
        "evidence_text",
        "chunk_text",
        "vector",
        "adapter_bytes",
        "adapter_config",
        "path",
        "filename",
    }
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = re.sub(r"[^a-z0-9]+", "", key.casefold()) if isinstance(key, str) else ""
            if not isinstance(key, str) or normalized in {
                re.sub(r"[^a-z0-9]+", "", item) for item in forbidden
            }:
                raise EvaluationContractError("result_publication_failed")
            _assert_content_free(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _assert_content_free(child)


def _prepare_manifest(
    manifest: dict[str, object],
    scope: DepartmentScope,
    evaluation_id: UUID,
    publication_attempt_id: UUID,
) -> dict[str, object]:
    if not isinstance(manifest, dict):
        raise EvaluationContractError("result_publication_failed")
    value = dict(manifest)
    value["artifact_contract_version"] = ADAPTER_EVALUATION_ARTIFACT_CONTRACT_VERSION
    if set(value) != _MANIFEST_KEYS:
        raise EvaluationContractError("result_publication_failed")
    if (
        value.get("department_id") != str(scope.value)
        or value.get("evaluation_id") != str(evaluation_id)
        or value.get("publication_attempt_id") != str(publication_attempt_id)
    ):
        raise EvaluationContractError("result_publication_failed")
    for name in (
        "adapter_id",
        "suite_id",
        "baseline_lane_id",
        "candidate_lane_id",
        "registry_publication_attempt_id",
    ):
        try:
            if UUID(value[name]).int == 0:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise EvaluationContractError("result_publication_failed") from None
    for name in (
        "adapter_version",
        "attempt_number",
        "registry_attempt_number",
        "adapter_config_byte_size",
        "adapter_model_byte_size",
    ):
        if type(value.get(name)) is not int or value[name] <= 0:
            raise EvaluationContractError("result_publication_failed")
    if type(value.get("base_seed")) is not int or not 0 <= value["base_seed"] < 1 << 63:
        raise EvaluationContractError("result_publication_failed")
    for name in (
        "registry_manifest_sha256",
        "adapter_config_sha256",
        "adapter_model_sha256",
    ):
        if (
            not isinstance(value.get(name), str)
            or re.fullmatch(r"[0-9a-f]{64}", value[name]) is None
        ):
            raise EvaluationContractError("result_publication_failed")
    if (
        value.get("base_model_id") != "Qwen/Qwen3-0.6B"
        or value.get("base_model_revision") != "c1899de289a04d12100db370d81485cdf75e47ca"
        or value.get("runner_contract_version") != ADAPTER_EVALUATION_RUNNER_CONTRACT_VERSION
        or value.get("metric_contract_version") != ADAPTER_EVALUATION_METRIC_CONTRACT_VERSION
        or value.get("gate_policy_version") != ADAPTER_EVALUATION_GATE_POLICY_VERSION
        or value.get("seed_policy_version") != ADAPTER_EVALUATION_SEED_POLICY_VERSION
    ):
        raise EvaluationContractError("result_publication_failed")
    return value


def _validate_summary(summary: dict[str, object]) -> None:
    if not isinstance(summary, dict) or set(summary) != _SUMMARY_KEYS:
        raise EvaluationContractError("result_publication_failed")
    for name in ("baseline_metrics", "candidate_metrics", "metric_deltas"):
        metrics = summary.get(name)
        if not isinstance(metrics, dict) or set(metrics) != _METRIC_NAMES:
            raise EvaluationContractError("result_publication_failed")
        for value in metrics.values():
            if not hasattr(value, "is_finite") or not value.is_finite():
                raise EvaluationContractError("result_publication_failed")
            if name == "metric_deltas":
                if value < -1 or value > 1:
                    raise EvaluationContractError("result_publication_failed")
            elif value < 0 or value > 1:
                raise EvaluationContractError("result_publication_failed")
    if summary.get("baseline_gate_status") not in {"passed", "failed"} or summary.get(
        "candidate_gate_status"
    ) not in {"passed", "failed"}:
        raise EvaluationContractError("result_publication_failed")
    for name in ("baseline_failed_gate_count", "candidate_failed_gate_count"):
        if type(summary.get(name)) is not int or not 0 <= summary[name] <= 8:
            raise EvaluationContractError("result_publication_failed")


def _validate_case_row(row: dict[str, object]) -> None:
    if not isinstance(row, dict) or set(row) != _CASE_KEYS:
        raise EvaluationContractError("result_publication_failed")
    if row.get("target") not in {"baseline", "candidate"}:
        raise EvaluationContractError("result_publication_failed")
    if not isinstance(row.get("case_id"), UUID) or row["case_id"].int == 0:
        raise EvaluationContractError("result_publication_failed")
    if row.get("expected_status") not in {"answered", "insufficient_information"} or row.get(
        "actual_status"
    ) not in {"answered", "insufficient_information", "failed"}:
        raise EvaluationContractError("result_publication_failed")
    if (
        row.get("error_code") is not None
        and safe_error_code(row["error_code"]) != row["error_code"]
    ):
        raise EvaluationContractError("result_publication_failed")
    for name in (
        "relevant_chunk_count",
        "retrieval_candidate_count",
        "retrieved_relevant_at_5",
        "retrieved_relevant_at_10",
        "retrieved_relevant_at_20",
        "cited_count",
        "cited_relevant_count",
    ):
        if type(row.get(name)) is not int or row[name] < 0:
            raise EvaluationContractError("result_publication_failed")
    if row["expected_status"] == "answered":
        if not 1 <= row["relevant_chunk_count"] <= 8:
            raise EvaluationContractError("result_publication_failed")
    elif row["relevant_chunk_count"] != 0:
        raise EvaluationContractError("result_publication_failed")
    if row["actual_status"] == "failed":
        if row["error_code"] is None:
            raise EvaluationContractError("result_publication_failed")
    elif row["error_code"] is not None:
        raise EvaluationContractError("result_publication_failed")
    if (
        row["retrieved_relevant_at_10"] < row["retrieved_relevant_at_5"]
        or row["retrieved_relevant_at_20"] < row["retrieved_relevant_at_10"]
        or row["retrieved_relevant_at_20"] > row["relevant_chunk_count"]
        or row["cited_relevant_count"] > row["cited_count"]
        or row["cited_relevant_count"] > row["relevant_chunk_count"]
    ):
        raise EvaluationContractError("result_publication_failed")
    for name in (
        "reciprocal_rank_at_20",
        "citation_precision",
        "citation_recall",
        "normalized_exact_match",
        "character_f1",
    ):
        if not hasattr(row.get(name), "is_finite") or not row[name].is_finite():
            raise EvaluationContractError("result_publication_failed")
        if not 0 <= row[name] <= 1:
            raise EvaluationContractError("result_publication_failed")
    for name in ("status_correct", "answer_contract_valid", "case_gate_passed"):
        if type(row.get(name)) is not bool:
            raise EvaluationContractError("result_publication_failed")
    if row["case_gate_passed"] != (row["status_correct"] and row["answer_contract_valid"]):
        raise EvaluationContractError("result_publication_failed")


def _validate_manifest_file(
    manifest_path: Path,
    expected: dict[str, object],
    verified: dict[str, ArtifactDigest],
) -> None:
    try:
        raw = manifest_path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise EvaluationContractError("result_publication_failed") from error
    if not isinstance(value, dict) or value.get("files") != {
        "summary.json": {
            "sha256": verified["summary.json"].sha256,
            "byte_size": verified["summary.json"].byte_size,
        },
        "case_results.jsonl": {
            "sha256": verified["case_results.jsonl"].sha256,
            "byte_size": verified["case_results.jsonl"].byte_size,
        },
    }:
        raise EvaluationContractError("result_publication_failed")
    actual = dict(value)
    actual.pop("files", None)
    expected_without_files = dict(expected)
    expected_without_files.pop("files", None)
    if actual != expected_without_files:
        raise EvaluationContractError("result_publication_failed")
