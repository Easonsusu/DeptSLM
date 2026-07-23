"""Private descriptor-checked external artifacts for Phase 9 evaluation."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from app.authorization import DepartmentScope
from app.evaluation_domain import (
    ARTIFACT_CONTRACT_VERSION,
    MAX_CASE_JSONL_LINE_BYTES,
    MAX_SUITE_INPUT_BYTES,
    EvaluationCaseScore,
    EvaluationContractError,
)

SUITE_FILES = frozenset({"manifest.json", "cases.jsonl"})
RUN_FILES = frozenset({"manifest.json", "summary.json", "case_results.jsonl"})


@dataclass(frozen=True, slots=True)
class ArtifactDigest:
    sha256: str
    byte_size: int


@dataclass(frozen=True, slots=True)
class StagedArtifact:
    path: Path
    final_path: Path
    manifest: ArtifactDigest
    payload: ArtifactDigest


class EvaluationArtifactStore:
    """Use only UUID-derived private paths below the external eval_results root."""

    def __init__(self, data_dir: Path) -> None:
        root = _real_directory(data_dir, writable=False)
        self.root = _real_directory(root / "eval_results", writable=True)
        self.suites = _ensure_private_directory(self.root, "suites")
        self.runs = _ensure_private_directory(self.root, "runs")
        staging = _ensure_private_directory(self.root, "staging")
        self.staging_suites = _ensure_private_directory(staging, "suites")
        self.staging_runs = _ensure_private_directory(staging, "runs")

    def stage_suite(
        self,
        scope: DepartmentScope,
        suite_id: UUID,
        stage_id: UUID,
        manifest_value: dict[str, object],
        canonical_case_lines: Iterable[bytes],
    ) -> StagedArtifact:
        stage = self._stage_directory(self.staging_suites, scope, suite_id, stage_id)
        final = self._final_directory(self.suites, scope, suite_id)
        try:
            payload = _write_lines(stage, "cases.jsonl", canonical_case_lines)
            if not 1 <= payload.byte_size <= MAX_SUITE_INPUT_BYTES:
                raise EvaluationContractError("suite_contract_invalid")
            manifest = dict(manifest_value)
            manifest["artifact_contract_version"] = ARTIFACT_CONTRACT_VERSION
            manifest["files"] = {
                "cases.jsonl": {
                    "sha256": payload.sha256,
                    "byte_size": payload.byte_size,
                }
            }
            manifest_digest = _write_bytes(
                stage, "manifest.json", canonical_json_bytes(manifest) + b"\n"
            )
            _verify_directory(stage, SUITE_FILES)
            return StagedArtifact(stage, final, manifest_digest, payload)
        except Exception:
            _safe_remove_tree(stage)
            raise

    def stage_run(
        self,
        scope: DepartmentScope,
        suite_id: UUID,
        run_id: UUID,
        claim_token: UUID,
        *,
        manifest_value: dict[str, object],
        summary_value: dict[str, object],
        scores: Iterable[EvaluationCaseScore],
    ) -> tuple[StagedArtifact, ArtifactDigest]:
        stage = self._stage_directory(self.staging_runs, scope, run_id, claim_token)
        final = self._final_directory(self.runs, scope, run_id)
        try:
            case_digest = _write_lines(
                stage,
                "case_results.jsonl",
                (canonical_json_bytes(_score_value(score)) + b"\n" for score in scores),
            )
            summary_digest = _write_bytes(
                stage, "summary.json", canonical_json_bytes(summary_value) + b"\n"
            )
            manifest = dict(manifest_value)
            manifest["artifact_contract_version"] = ARTIFACT_CONTRACT_VERSION
            manifest["files"] = {
                "summary.json": {
                    "sha256": summary_digest.sha256,
                    "byte_size": summary_digest.byte_size,
                },
                "case_results.jsonl": {
                    "sha256": case_digest.sha256,
                    "byte_size": case_digest.byte_size,
                },
            }
            manifest_digest = _write_bytes(
                stage, "manifest.json", canonical_json_bytes(manifest) + b"\n"
            )
            _verify_directory(stage, RUN_FILES)
            return (
                StagedArtifact(stage, final, manifest_digest, case_digest),
                summary_digest,
            )
        except Exception:
            _safe_remove_tree(stage)
            raise

    def publish(self, staged: StagedArtifact, allowlist: frozenset[str]) -> None:
        _verify_directory(staged.path, allowlist)
        _ensure_parent(staged.final_path)
        if staged.final_path.exists():
            raise EvaluationContractError("result_publication_failed")
        try:
            os.rename(staged.path, staged.final_path)
            os.chmod(staged.final_path, 0o700)
            _verify_directory(staged.final_path, allowlist)
        except EvaluationContractError:
            raise
        except OSError as error:
            raise EvaluationContractError("result_publication_failed") from error

    def cleanup_stage(
        self, scope: DepartmentScope, resource_id: UUID, stage_id: UUID, *, suite: bool
    ) -> None:
        root = self.staging_suites if suite else self.staging_runs
        path = root / str(scope.value) / str(resource_id) / str(stage_id)
        _require_beneath(root, path)
        _safe_remove_tree(path)

    def remove_final(self, scope: DepartmentScope, resource_id: UUID, *, suite: bool) -> None:
        root = self.suites if suite else self.runs
        path = root / str(scope.value) / str(resource_id)
        _require_beneath(root, path)
        _safe_remove_tree(path)

    def iter_suite_cases(
        self,
        scope: DepartmentScope,
        suite_id: UUID,
        *,
        manifest_sha256: str,
        cases_sha256: str,
        cases_byte_size: int,
    ) -> Iterator[dict[str, object]]:
        path = self._final_directory(self.suites, scope, suite_id)
        _verify_directory(path, SUITE_FILES)
        manifest = _digest_file(path / "manifest.json")
        cases = _digest_file(path / "cases.jsonl")
        if (
            manifest.sha256 != manifest_sha256
            or cases.sha256 != cases_sha256
            or cases.byte_size != cases_byte_size
        ):
            raise EvaluationContractError("suite_artifact_mismatch")
        yield from _iter_json_lines(path / "cases.jsonl")

    def _stage_directory(
        self,
        root: Path,
        scope: DepartmentScope,
        resource_id: UUID,
        stage_id: UUID,
    ) -> Path:
        _require_identifiers(scope, resource_id, stage_id)
        parent = _ensure_private_directory(root, str(scope.value))
        parent = _ensure_private_directory(parent, str(resource_id))
        stage = parent / str(stage_id)
        try:
            os.mkdir(stage, 0o700)
        except FileExistsError as error:
            raise EvaluationContractError("result_publication_failed") from error
        return _real_directory(stage, writable=True)

    @staticmethod
    def _final_directory(root: Path, scope: DepartmentScope, resource_id: UUID) -> Path:
        _require_identifiers(scope, resource_id)
        path = root / str(scope.value) / str(resource_id)
        _require_beneath(root, path)
        return path


def validate_suite_source_directory(source: Path, repository_root: Path) -> Path:
    if not source.is_absolute():
        raise EvaluationContractError()
    source = Path(os.path.abspath(source))
    root = repository_root.resolve()
    if source == root or source.is_relative_to(root) or root.is_relative_to(source):
        raise EvaluationContractError()
    directory = _real_directory(source, writable=False)
    entries = {entry.name for entry in os.scandir(directory)}
    if entries != {"suite.json", "cases.jsonl"}:
        raise EvaluationContractError()
    total = 0
    for name in entries:
        metadata = (directory / name).lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise EvaluationContractError()
        total += metadata.st_size
    if not 1 <= total <= MAX_SUITE_INPUT_BYTES:
        raise EvaluationContractError()
    return directory


def read_suite_definition(source: Path) -> dict[str, object]:
    file_path = source / "suite.json"
    raw = _read_bounded_file(file_path, maximum=64 * 1024)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EvaluationContractError() from error
    if not isinstance(value, dict):
        raise EvaluationContractError()
    return value


def iter_source_cases(source: Path) -> Iterator[dict[str, object]]:
    yield from _iter_json_lines(source / "cases.jsonl")


def canonical_json_bytes(value: object) -> bytes:
    return _json_value(value).encode("utf-8")


def _json_value(value: object) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise EvaluationContractError()
        return format(value, "f")
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, (str, UUID)):
        return json.dumps(str(value), ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_json_value(item) for item in value) + "]"
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise EvaluationContractError()
        return (
            "{"
            + ",".join(
                f"{json.dumps(key, ensure_ascii=False)}:{_json_value(value[key])}"
                for key in sorted(value)
            )
            + "}"
        )
    raise EvaluationContractError()


def _score_value(score: EvaluationCaseScore) -> dict[str, object]:
    return {
        "case_id": score.case_id,
        "expected_status": score.expected_status,
        "actual_status": score.actual_status,
        "relevant_chunk_count": score.relevant_chunk_count,
        "retrieved_relevant_at_5": score.retrieved_relevant_at_5,
        "retrieved_relevant_at_10": score.retrieved_relevant_at_10,
        "retrieved_relevant_at_20": score.retrieved_relevant_at_20,
        "reciprocal_rank_at_20": score.reciprocal_rank_at_20,
        "status_correct": score.status_correct,
        "cited_count": score.cited_count,
        "cited_relevant_count": score.cited_relevant_count,
        "citation_precision": score.citation_precision,
        "citation_recall": score.citation_recall,
        "normalized_exact_match": score.normalized_exact_match,
        "character_f1": score.character_f1,
        "answer_contract_valid": score.answer_contract_valid,
        "case_gate_passed": score.case_gate_passed,
        "error_code": score.error_code,
    }


def _iter_json_lines(path: Path) -> Iterator[dict[str, object]]:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            for raw in handle:
                if not raw.endswith(b"\n") or len(raw) > MAX_CASE_JSONL_LINE_BYTES:
                    raise EvaluationContractError("suite_artifact_mismatch")
                try:
                    value = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise EvaluationContractError("suite_artifact_mismatch") from error
                if not isinstance(value, dict):
                    raise EvaluationContractError("suite_artifact_mismatch")
                yield value
    except EvaluationContractError:
        raise
    except FileNotFoundError as error:
        raise EvaluationContractError("suite_artifact_missing") from error
    except OSError as error:
        raise EvaluationContractError("suite_artifact_mismatch") from error


def _write_lines(directory: Path, name: str, lines: Iterable[bytes]) -> ArtifactDigest:
    digest = hashlib.sha256()
    size = 0
    descriptor = _exclusive_file(directory / name)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            for line in lines:
                if not isinstance(line, bytes) or not line.endswith(b"\n"):
                    raise EvaluationContractError()
                if len(line) > MAX_CASE_JSONL_LINE_BYTES:
                    raise EvaluationContractError()
                size += len(line)
                if size > MAX_SUITE_INPUT_BYTES:
                    raise EvaluationContractError()
                digest.update(line)
                handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            os.unlink(directory / name)
        except OSError:
            pass
        raise
    if size == 0:
        raise EvaluationContractError()
    return ArtifactDigest(digest.hexdigest(), size)


def _write_bytes(directory: Path, name: str, value: bytes) -> ArtifactDigest:
    descriptor = _exclusive_file(directory / name)
    with os.fdopen(descriptor, "wb", closefd=True) as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    return ArtifactDigest(hashlib.sha256(value).hexdigest(), len(value))


def _exclusive_file(path: Path) -> int:
    try:
        return os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as error:
        raise EvaluationContractError("result_publication_failed") from error


def _digest_file(path: Path) -> ArtifactDigest:
    digest = hashlib.sha256()
    size = 0
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise EvaluationContractError("suite_artifact_mismatch")
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            while chunk := handle.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
    except EvaluationContractError:
        raise
    except FileNotFoundError as error:
        raise EvaluationContractError("suite_artifact_missing") from error
    except OSError as error:
        raise EvaluationContractError("suite_artifact_mismatch") from error
    return ArtifactDigest(digest.hexdigest(), size)


def _read_bounded_file(path: Path, *, maximum: int) -> bytes:
    digest = _digest_file(path)
    if not 1 <= digest.byte_size <= maximum:
        raise EvaluationContractError()
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb", closefd=True) as handle:
        value = handle.read(maximum + 1)
    if len(value) > maximum:
        raise EvaluationContractError()
    return value


def _verify_directory(path: Path, allowlist: frozenset[str]) -> None:
    directory = _real_directory(path, writable=False)
    entries = {entry.name for entry in os.scandir(directory)}
    if entries != allowlist:
        raise EvaluationContractError("suite_artifact_mismatch")
    for name in entries:
        metadata = (directory / name).lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_mode & 0o077
        ):
            raise EvaluationContractError("suite_artifact_mismatch")


def _real_directory(path: Path, *, writable: bool) -> Path:
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise EvaluationContractError("suite_artifact_missing") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise EvaluationContractError("suite_artifact_mismatch")
    mode = os.R_OK | os.X_OK | (os.W_OK if writable else 0)
    if not os.access(path, mode):
        raise EvaluationContractError("suite_artifact_mismatch")
    return path.resolve()


def _ensure_private_directory(parent: Path, name: str) -> Path:
    if not name or "/" in name or "\\" in name or name in {".", ".."}:
        raise EvaluationContractError()
    path = parent / name
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    directory = _real_directory(path, writable=True)
    os.chmod(directory, 0o700)
    return directory


def _ensure_parent(path: Path) -> None:
    parent = _ensure_private_directory(path.parent.parent, path.parent.name)
    if parent != path.parent.resolve():
        raise EvaluationContractError("result_publication_failed")


def _require_identifiers(
    scope: DepartmentScope, resource_id: UUID, stage_id: UUID | None = None
) -> None:
    if (
        not isinstance(scope, DepartmentScope)
        or not isinstance(resource_id, UUID)
        or resource_id.int == 0
        or (stage_id is not None and (not isinstance(stage_id, UUID) or stage_id.int == 0))
    ):
        raise EvaluationContractError()


def _require_beneath(root: Path, path: Path) -> None:
    normalized = Path(os.path.abspath(path))
    if normalized == root or not normalized.is_relative_to(root):
        raise EvaluationContractError()


def _safe_remove_tree(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise EvaluationContractError("result_publication_failed")
    shutil.rmtree(path)
