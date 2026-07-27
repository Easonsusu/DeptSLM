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
    GATE_NAMES,
    MAX_CASE_JSONL_LINE_BYTES,
    MAX_SUITE_INPUT_BYTES,
    EvaluationCaseScore,
    EvaluationContractError,
)

SUITE_FILES = frozenset({"manifest.json", "cases.jsonl"})
RUN_FILES = frozenset({"manifest.json", "summary.json", "case_results.jsonl"})
SOURCE_SUITE_FILES = frozenset({"suite.json", "cases.jsonl"})


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
    files: tuple[tuple[str, ArtifactDigest], ...]


@dataclass(frozen=True, slots=True)
class PublishedArtifact:
    path: Path
    manifest: ArtifactDigest
    payload: ArtifactDigest
    summary: ArtifactDigest | None


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
            staged = StagedArtifact(
                stage,
                final,
                manifest_digest,
                payload,
                (
                    ("cases.jsonl", payload),
                    ("manifest.json", manifest_digest),
                ),
            )
            _verify_expected_files(stage, SUITE_FILES, dict(staged.files))
            return staged
        except Exception:
            _safe_remove_tree(stage)
            raise

    def stage_run(
        self,
        scope: DepartmentScope,
        suite_id: UUID,
        run_id: UUID,
        publication_attempt_id: UUID,
        *,
        manifest_value: dict[str, object],
        summary_value: dict[str, object],
        scores: Iterable[EvaluationCaseScore],
    ) -> tuple[StagedArtifact, ArtifactDigest]:
        stage = self._stage_directory(self.staging_runs, scope, run_id, publication_attempt_id)
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
            staged = StagedArtifact(
                stage,
                final,
                manifest_digest,
                case_digest,
                (
                    ("case_results.jsonl", case_digest),
                    ("manifest.json", manifest_digest),
                    ("summary.json", summary_digest),
                ),
            )
            _verify_expected_files(stage, RUN_FILES, dict(staged.files))
            return (
                staged,
                summary_digest,
            )
        except Exception:
            _safe_remove_tree(stage)
            raise

    def publish(self, staged: StagedArtifact, allowlist: frozenset[str]) -> PublishedArtifact:
        expected = dict(staged.files)
        if set(expected) != allowlist:
            raise EvaluationContractError("result_publication_failed")
        _verify_expected_files(staged.path, allowlist, expected)
        _ensure_parent(staged.final_path)
        if staged.final_path.exists():
            raise EvaluationContractError("result_publication_failed")
        try:
            os.rename(staged.path, staged.final_path)
            os.chmod(staged.final_path, 0o700)
            verified = _verify_expected_files(staged.final_path, allowlist, expected)
            return PublishedArtifact(
                staged.final_path,
                verified["manifest.json"],
                verified["cases.jsonl" if "cases.jsonl" in verified else "case_results.jsonl"],
                verified.get("summary.json"),
            )
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

    def verify_published_run(
        self,
        scope: DepartmentScope,
        run_id: UUID,
        *,
        expected_manifest: dict[str, object],
        expected: StagedArtifact | None = None,
    ) -> PublishedArtifact:
        """Re-open a final result through no-follow descriptors and bind it to one attempt."""

        path = self._final_directory(self.runs, scope, run_id)
        verified = (
            _verify_expected_files(
                path,
                RUN_FILES,
                dict(expected.files) if expected is not None else {},
            )
            if expected is not None
            else _verify_run_files(path)
        )
        manifest = _verified_run_manifest(path)
        required_manifest = dict(expected_manifest)
        required_manifest["artifact_contract_version"] = ARTIFACT_CONTRACT_VERSION
        required_manifest["files"] = {
            "summary.json": {
                "sha256": verified["summary.json"].sha256,
                "byte_size": verified["summary.json"].byte_size,
            },
            "case_results.jsonl": {
                "sha256": verified["case_results.jsonl"].sha256,
                "byte_size": verified["case_results.jsonl"].byte_size,
            },
        }
        if manifest != required_manifest:
            raise EvaluationContractError("result_publication_failed")
        return PublishedArtifact(
            path,
            verified["manifest.json"],
            verified["case_results.jsonl"],
            verified["summary.json"],
        )

    def remove_owned_run_final(
        self,
        scope: DepartmentScope,
        run_id: UUID,
        suite_id: UUID,
        publication_attempt_id: UUID,
        attempt_number: int,
        code_revision: str,
    ) -> bool:
        """Remove only a final directory whose closed manifest proves exact ownership."""

        path = self._final_directory(self.runs, scope, run_id)
        if not path.exists():
            return False
        manifest = _verified_run_manifest(path)
        if not _run_manifest_owned_by(
            manifest,
            scope,
            run_id,
            suite_id,
            publication_attempt_id,
            attempt_number,
            code_revision,
        ):
            raise EvaluationContractError("result_publication_failed")
        _safe_remove_tree(path)
        return True

    def verify_published_suite(
        self,
        scope: DepartmentScope,
        suite_id: UUID,
        *,
        expected_manifest: dict[str, object],
        expected: StagedArtifact,
    ) -> PublishedArtifact:
        path = self._final_directory(self.suites, scope, suite_id)
        verified = _verify_expected_files(path, SUITE_FILES, dict(expected.files))
        manifest = _read_suite_manifest(path)
        _validate_suite_manifest_shape(manifest)
        required = dict(expected_manifest)
        required["artifact_contract_version"] = ARTIFACT_CONTRACT_VERSION
        required["files"] = {
            "cases.jsonl": {
                "sha256": verified["cases.jsonl"].sha256,
                "byte_size": verified["cases.jsonl"].byte_size,
            }
        }
        if manifest != required:
            raise EvaluationContractError("result_publication_failed")
        return PublishedArtifact(
            path,
            verified["manifest.json"],
            verified["cases.jsonl"],
            None,
        )

    def run_final_owned_by(
        self,
        scope: DepartmentScope,
        run_id: UUID,
        suite_id: UUID,
        publication_attempt_id: UUID,
        attempt_number: int,
        code_revision: str,
    ) -> bool:
        path = self._final_directory(self.runs, scope, run_id)
        if not path.exists():
            return False
        manifest = _verified_run_manifest(path)
        return _run_manifest_owned_by(
            manifest,
            scope,
            run_id,
            suite_id,
            publication_attempt_id,
            attempt_number,
            code_revision,
        )

    def run_stage_owned_by(
        self,
        scope: DepartmentScope,
        run_id: UUID,
        suite_id: UUID,
        publication_attempt_id: UUID,
        attempt_number: int,
        code_revision: str,
    ) -> bool:
        path = self._existing_stage_path(self.staging_runs, scope, run_id, publication_attempt_id)
        if not _path_exists(path):
            return False
        manifest = _verified_run_manifest(path)
        return _run_manifest_owned_by(
            manifest,
            scope,
            run_id,
            suite_id,
            publication_attempt_id,
            attempt_number,
            code_revision,
        )

    def remove_owned_run_stage(
        self,
        scope: DepartmentScope,
        run_id: UUID,
        suite_id: UUID,
        publication_attempt_id: UUID,
        attempt_number: int,
        code_revision: str,
    ) -> bool:
        path = self._existing_stage_path(self.staging_runs, scope, run_id, publication_attempt_id)
        if not _path_exists(path):
            return False
        if not self.run_stage_owned_by(
            scope,
            run_id,
            suite_id,
            publication_attempt_id,
            attempt_number,
            code_revision,
        ):
            raise EvaluationContractError("result_publication_failed")
        _safe_remove_tree(path)
        return True

    def remove_owned_suite_final(
        self,
        scope: DepartmentScope,
        suite_id: UUID,
        import_attempt_id: UUID,
    ) -> bool:
        path = self._final_directory(self.suites, scope, suite_id)
        if not path.exists():
            return False
        manifest = _verified_suite_manifest(path)
        if (
            manifest.get("department_id") != str(scope.value)
            or manifest.get("suite_id") != str(suite_id)
            or manifest.get("import_attempt_id") != str(import_attempt_id)
        ):
            raise EvaluationContractError("result_publication_failed")
        _safe_remove_tree(path)
        return True

    def suite_final_owned_by(
        self, scope: DepartmentScope, suite_id: UUID, import_attempt_id: UUID
    ) -> bool:
        path = self._final_directory(self.suites, scope, suite_id)
        if not path.exists():
            return False
        manifest = _verified_suite_manifest(path)
        return (
            manifest.get("department_id") == str(scope.value)
            and manifest.get("suite_id") == str(suite_id)
            and manifest.get("import_attempt_id") == str(import_attempt_id)
        )

    def suite_stage_owned_by(
        self,
        scope: DepartmentScope,
        suite_id: UUID,
        stage_id: UUID,
        import_attempt_id: UUID,
    ) -> bool:
        path = self._existing_stage_path(self.staging_suites, scope, suite_id, stage_id)
        if not _path_exists(path):
            return False
        manifest = _verified_suite_manifest(path)
        return (
            manifest.get("department_id") == str(scope.value)
            and manifest.get("suite_id") == str(suite_id)
            and manifest.get("stage_id") == str(stage_id)
            and manifest.get("import_attempt_id") == str(import_attempt_id)
        )

    def remove_owned_suite_stage(
        self,
        scope: DepartmentScope,
        suite_id: UUID,
        stage_id: UUID,
        import_attempt_id: UUID,
    ) -> bool:
        path = self._existing_stage_path(self.staging_suites, scope, suite_id, stage_id)
        if not _path_exists(path):
            return False
        if not self.suite_stage_owned_by(scope, suite_id, stage_id, import_attempt_id):
            raise EvaluationContractError("result_publication_failed")
        _safe_remove_tree(path)
        return True

    def run_stage_present(
        self, scope: DepartmentScope, run_id: UUID, publication_attempt_id: UUID
    ) -> bool:
        return _path_exists(
            self._existing_stage_path(self.staging_runs, scope, run_id, publication_attempt_id)
        )

    def run_final_present(self, scope: DepartmentScope, run_id: UUID) -> bool:
        return _path_exists(self._final_directory(self.runs, scope, run_id))

    def suite_stage_present(self, scope: DepartmentScope, suite_id: UUID, stage_id: UUID) -> bool:
        return _path_exists(
            self._existing_stage_path(self.staging_suites, scope, suite_id, stage_id)
        )

    def suite_final_present(self, scope: DepartmentScope, suite_id: UUID) -> bool:
        return _path_exists(self._final_directory(self.suites, scope, suite_id))

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
        directory = _open_directory(path)
        try:
            _verify_directory_descriptor(directory, SUITE_FILES)
            manifest_descriptor, manifest_metadata = _open_regular_at(directory, "manifest.json")
            try:
                manifest_raw, manifest = _read_bounded_descriptor(
                    manifest_descriptor,
                    manifest_metadata,
                    maximum=64 * 1024,
                )
                _verify_path_identity(directory, "manifest.json", manifest_metadata)
            finally:
                os.close(manifest_descriptor)
            if manifest.sha256 != manifest_sha256:
                raise EvaluationContractError("suite_artifact_mismatch")
            manifest_value = _parse_json_object(manifest_raw)
            _validate_canonical_suite_manifest(
                manifest_value,
                cases_sha256=cases_sha256,
                cases_byte_size=cases_byte_size,
            )
            if manifest_value.get("suite_id") != str(suite_id) or manifest_value.get(
                "department_id"
            ) != str(scope.value):
                raise EvaluationContractError("suite_artifact_mismatch")
            cases_descriptor, cases_metadata = _open_regular_at(directory, "cases.jsonl")
            try:
                yield from _iter_json_lines_descriptor(
                    cases_descriptor,
                    cases_metadata,
                    expected=ArtifactDigest(cases_sha256, cases_byte_size),
                    maximum=MAX_SUITE_INPUT_BYTES,
                )
                _verify_path_identity(directory, "cases.jsonl", cases_metadata)
            finally:
                os.close(cases_descriptor)
            _verify_path_identity(directory, "manifest.json", manifest_metadata)
            _verify_directory_descriptor(directory, SUITE_FILES)
        finally:
            os.close(directory)

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
    def _existing_stage_path(
        root: Path,
        scope: DepartmentScope,
        resource_id: UUID,
        stage_id: UUID,
    ) -> Path:
        _require_identifiers(scope, resource_id, stage_id)
        path = root / str(scope.value) / str(resource_id) / str(stage_id)
        _require_beneath(root, path)
        return path

    @staticmethod
    def _final_directory(root: Path, scope: DepartmentScope, resource_id: UUID) -> Path:
        _require_identifiers(scope, resource_id)
        path = root / str(scope.value) / str(resource_id)
        _require_beneath(root, path)
        return path


class SuiteSourceReader:
    """Read an administrative suite through one no-follow descriptor lifetime."""

    def __init__(self, source: Path, repository_root: Path) -> None:
        self.path = _validate_external_source_path(source, repository_root)
        self.directory = _open_directory(self.path)
        self.files: dict[str, tuple[int, os.stat_result]] = {}
        try:
            _verify_directory_descriptor(
                self.directory,
                SOURCE_SUITE_FILES,
                require_private=False,
            )
            for name in sorted(SOURCE_SUITE_FILES):
                self.files[name] = _open_regular_at(self.directory, name)
            total = sum(metadata.st_size for _descriptor, metadata in self.files.values())
            if not 1 <= total <= MAX_SUITE_INPUT_BYTES:
                raise EvaluationContractError()
        except Exception:
            self.close()
            raise

    def read_definition(self) -> dict[str, object]:
        descriptor, metadata = self.files["suite.json"]
        raw, _digest = _read_bounded_descriptor(
            descriptor,
            metadata,
            maximum=64 * 1024,
        )
        _verify_path_identity(self.directory, "suite.json", metadata)
        return _parse_json_object(raw)

    def iter_cases(self) -> Iterator[dict[str, object]]:
        descriptor, metadata = self.files["cases.jsonl"]
        yield from _iter_json_lines_descriptor(
            descriptor,
            metadata,
            expected=None,
            maximum=MAX_SUITE_INPUT_BYTES,
        )
        _verify_path_identity(self.directory, "cases.jsonl", metadata)
        _verify_path_identity(self.directory, "suite.json", self.files["suite.json"][1])

    def close(self) -> None:
        for descriptor, _metadata in self.files.values():
            try:
                os.close(descriptor)
            except OSError:
                pass
        self.files.clear()
        if getattr(self, "directory", -1) >= 0:
            try:
                os.close(self.directory)
            except OSError:
                pass
            self.directory = -1

    def __enter__(self) -> SuiteSourceReader:
        return self

    def __exit__(self, *_args) -> None:
        self.close()


def validate_suite_source_directory(source: Path, repository_root: Path) -> Path:
    with SuiteSourceReader(source, repository_root) as reader:
        return reader.path


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
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        metadata = os.fstat(descriptor)
        try:
            yield from _iter_json_lines_descriptor(
                descriptor,
                metadata,
                expected=None,
                maximum=MAX_SUITE_INPUT_BYTES,
            )
        finally:
            os.close(descriptor)
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


def _validate_external_source_path(source: Path, repository_root: Path) -> Path:
    if not source.is_absolute():
        raise EvaluationContractError()
    source = Path(os.path.abspath(source))
    root = repository_root.resolve()
    if source == root or source.is_relative_to(root) or root.is_relative_to(source):
        raise EvaluationContractError()
    return _real_directory(source, writable=False)


def _open_directory(path: Path) -> int:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise EvaluationContractError("suite_artifact_mismatch")
        return descriptor
    except EvaluationContractError:
        try:
            os.close(descriptor)
        except (OSError, UnboundLocalError):
            pass
        raise
    except FileNotFoundError as error:
        raise EvaluationContractError("suite_artifact_missing") from error
    except OSError as error:
        raise EvaluationContractError("suite_artifact_mismatch") from error


def _open_regular_at(directory: int, name: str) -> tuple[int, os.stat_result]:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory,
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise EvaluationContractError("suite_artifact_mismatch")
        return descriptor, metadata
    except EvaluationContractError:
        try:
            os.close(descriptor)
        except (OSError, UnboundLocalError):
            pass
        raise
    except FileNotFoundError as error:
        raise EvaluationContractError("suite_artifact_missing") from error
    except OSError as error:
        raise EvaluationContractError("suite_artifact_mismatch") from error


def _same_file_identity(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        stat.S_ISREG(after.st_mode)
        and after.st_dev == before.st_dev
        and after.st_ino == before.st_ino
        and after.st_size == before.st_size
        and after.st_nlink == before.st_nlink == 1
        and after.st_mode == before.st_mode
        and after.st_mtime_ns == before.st_mtime_ns
        and after.st_ctime_ns == before.st_ctime_ns
    )


def _verify_path_identity(directory: int, name: str, expected: os.stat_result) -> None:
    try:
        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError as error:
        raise EvaluationContractError("suite_artifact_missing") from error
    except OSError as error:
        raise EvaluationContractError("suite_artifact_mismatch") from error
    if not _same_file_identity(expected, current):
        raise EvaluationContractError("suite_artifact_mismatch")


def _read_bounded_descriptor(
    descriptor: int,
    metadata: os.stat_result,
    *,
    maximum: int,
) -> tuple[bytes, ArtifactDigest]:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or not 1 <= metadata.st_size <= maximum
    ):
        raise EvaluationContractError("suite_artifact_mismatch")
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    result = bytearray()
    while True:
        chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - len(result)))
        if not chunk:
            break
        result.extend(chunk)
        digest.update(chunk)
        if len(result) > maximum:
            raise EvaluationContractError("suite_artifact_mismatch")
    after = os.fstat(descriptor)
    if len(result) != metadata.st_size or not _same_file_identity(metadata, after):
        raise EvaluationContractError("suite_artifact_mismatch")
    return bytes(result), ArtifactDigest(digest.hexdigest(), len(result))


def _iter_json_lines_descriptor(
    descriptor: int,
    metadata: os.stat_result,
    *,
    expected: ArtifactDigest | None,
    maximum: int,
) -> Iterator[dict[str, object]]:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or not 1 <= metadata.st_size <= maximum
    ):
        raise EvaluationContractError("suite_artifact_mismatch")
    os.lseek(descriptor, 0, os.SEEK_SET)
    duplicate = os.dup(descriptor)
    digest = hashlib.sha256()
    total = 0
    try:
        with os.fdopen(duplicate, "rb", closefd=True) as handle:
            duplicate = -1
            while True:
                raw = handle.readline(MAX_CASE_JSONL_LINE_BYTES + 1)
                if not raw:
                    break
                if (
                    len(raw) > MAX_CASE_JSONL_LINE_BYTES
                    or not raw.endswith(b"\n")
                    or total + len(raw) > maximum
                ):
                    raise EvaluationContractError("suite_artifact_mismatch")
                total += len(raw)
                digest.update(raw)
                try:
                    value = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise EvaluationContractError("suite_artifact_mismatch") from error
                if not isinstance(value, dict):
                    raise EvaluationContractError("suite_artifact_mismatch")
                yield value
        actual = ArtifactDigest(digest.hexdigest(), total)
        after = os.fstat(descriptor)
        if (
            total != metadata.st_size
            or not _same_file_identity(metadata, after)
            or (expected is not None and actual != expected)
        ):
            raise EvaluationContractError("suite_artifact_mismatch")
    finally:
        if duplicate >= 0:
            os.close(duplicate)


def _digest_descriptor(
    descriptor: int,
    metadata: os.stat_result,
    *,
    maximum: int = MAX_SUITE_INPUT_BYTES,
) -> ArtifactDigest:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or not 1 <= metadata.st_size <= maximum
    ):
        raise EvaluationContractError("suite_artifact_mismatch")
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - size))
        if not chunk:
            break
        size += len(chunk)
        if size > maximum:
            raise EvaluationContractError("suite_artifact_mismatch")
        digest.update(chunk)
    after = os.fstat(descriptor)
    if size != metadata.st_size or not _same_file_identity(metadata, after):
        raise EvaluationContractError("suite_artifact_mismatch")
    return ArtifactDigest(digest.hexdigest(), size)


def _verify_directory_descriptor(
    directory: int,
    allowlist: frozenset[str],
    *,
    require_private: bool = True,
) -> None:
    try:
        entries = set(os.listdir(directory))
        if entries != allowlist:
            raise EvaluationContractError("suite_artifact_mismatch")
        for name in sorted(entries):
            descriptor, metadata = _open_regular_at(directory, name)
            try:
                if require_private and metadata.st_mode & 0o077:
                    raise EvaluationContractError("suite_artifact_mismatch")
                _verify_path_identity(directory, name, metadata)
            finally:
                os.close(descriptor)
    except EvaluationContractError:
        raise
    except OSError as error:
        raise EvaluationContractError("suite_artifact_mismatch") from error


def _verify_expected_files(
    path: Path,
    allowlist: frozenset[str],
    expected: dict[str, ArtifactDigest],
) -> dict[str, ArtifactDigest]:
    directory = _open_directory(path)
    try:
        _verify_directory_descriptor(directory, allowlist)
        verified: dict[str, ArtifactDigest] = {}
        manifest_raw = b""
        opened = {name: _open_regular_at(directory, name) for name in sorted(allowlist)}
        try:
            for name in sorted(allowlist):
                descriptor, metadata = opened[name]
                if name == "manifest.json":
                    manifest_raw, digest = _read_bounded_descriptor(
                        descriptor,
                        metadata,
                        maximum=64 * 1024,
                    )
                else:
                    digest = _digest_descriptor(descriptor, metadata)
                if digest != expected.get(name):
                    raise EvaluationContractError("result_publication_failed")
                verified[name] = digest
            for name, (_descriptor, metadata) in opened.items():
                _verify_path_identity(directory, name, metadata)
        finally:
            for descriptor, _metadata in opened.values():
                os.close(descriptor)
        _verify_directory_descriptor(directory, allowlist)
        _validate_published_manifest(manifest_raw, allowlist, verified)
        return verified
    finally:
        os.close(directory)


def _validate_published_manifest(
    raw: bytes,
    allowlist: frozenset[str],
    verified: dict[str, ArtifactDigest],
) -> None:
    manifest = _parse_json_object(raw)
    payload_names = allowlist - {"manifest.json"}
    declared = manifest.get("files")
    if not isinstance(declared, dict) or set(declared) != payload_names:
        raise EvaluationContractError("result_publication_failed")
    for name in payload_names:
        value = declared.get(name)
        expected = verified[name]
        if value != {"sha256": expected.sha256, "byte_size": expected.byte_size}:
            raise EvaluationContractError("result_publication_failed")


def _read_manifest(path: Path) -> dict[str, object]:
    directory = _open_directory(path)
    try:
        _verify_directory_descriptor(directory, RUN_FILES)
        descriptor, metadata = _open_regular_at(directory, "manifest.json")
        try:
            raw, _digest = _read_bounded_descriptor(descriptor, metadata, maximum=64 * 1024)
            _verify_path_identity(directory, "manifest.json", metadata)
            return _parse_json_object(raw)
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


def _read_suite_manifest(path: Path) -> dict[str, object]:
    directory = _open_directory(path)
    try:
        _verify_directory_descriptor(directory, SUITE_FILES)
        descriptor, metadata = _open_regular_at(directory, "manifest.json")
        try:
            raw, _digest = _read_bounded_descriptor(descriptor, metadata, maximum=64 * 1024)
            _verify_path_identity(directory, "manifest.json", metadata)
            return _parse_json_object(raw)
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


def _verified_run_manifest(path: Path) -> dict[str, object]:
    manifest = _read_manifest(path)
    _validate_run_manifest_shape(manifest)
    _verify_run_files(path)
    return manifest


def _verified_suite_manifest(path: Path) -> dict[str, object]:
    manifest = _read_suite_manifest(path)
    _validate_suite_manifest_shape(manifest)
    files = manifest["files"]
    assert isinstance(files, dict)
    payload = files["cases.jsonl"]
    assert isinstance(payload, dict)
    expected = {
        "cases.jsonl": ArtifactDigest(payload["sha256"], payload["byte_size"]),
    }
    directory = _open_directory(path)
    try:
        descriptor, metadata = _open_regular_at(directory, "manifest.json")
        try:
            _raw, manifest_digest = _read_bounded_descriptor(
                descriptor, metadata, maximum=64 * 1024
            )
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)
    expected["manifest.json"] = manifest_digest
    _verify_expected_files(path, SUITE_FILES, expected)
    return manifest


def _verify_run_files(path: Path) -> dict[str, ArtifactDigest]:
    manifest = _read_manifest(path)
    _validate_run_manifest_shape(manifest)
    files = manifest["files"]
    assert isinstance(files, dict)
    expected = {
        name: ArtifactDigest(value["sha256"], value["byte_size"])
        for name, value in files.items()
        if isinstance(value, dict)
        and isinstance(value.get("sha256"), str)
        and isinstance(value.get("byte_size"), int)
    }
    if set(expected) != {"summary.json", "case_results.jsonl"}:
        raise EvaluationContractError("result_publication_failed")
    # The manifest digest is not self-declared. Re-open it in the verifier and
    # let descriptor hashing establish it together with both payload digests.
    directory = _open_directory(path)
    try:
        descriptor, metadata = _open_regular_at(directory, "manifest.json")
        try:
            _raw, manifest_digest = _read_bounded_descriptor(
                descriptor, metadata, maximum=64 * 1024
            )
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)
    expected["manifest.json"] = manifest_digest
    return _verify_expected_files(path, RUN_FILES, expected)


def _validate_digest_value(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"sha256", "byte_size"}
        and isinstance(value["sha256"], str)
        and len(value["sha256"]) == 64
        and all(character in "0123456789abcdef" for character in value["sha256"])
        and isinstance(value["byte_size"], int)
        and not isinstance(value["byte_size"], bool)
        and value["byte_size"] > 0
    )


def _valid_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return UUID(value).int != 0
    except ValueError:
        return False


def _valid_positive_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _run_manifest_owned_by(
    manifest: dict[str, object],
    scope: DepartmentScope,
    run_id: UUID,
    suite_id: UUID,
    publication_attempt_id: UUID,
    attempt_number: int,
    code_revision: str,
) -> bool:
    return (
        _valid_positive_integer(attempt_number)
        and manifest.get("department_id") == str(scope.value)
        and manifest.get("run_id") == str(run_id)
        and manifest.get("suite_id") == str(suite_id)
        and manifest.get("publication_attempt_id") == str(publication_attempt_id)
        and manifest.get("attempt_number") == attempt_number
        and manifest.get("code_revision") == code_revision
    )


def _validate_suite_manifest_shape(value: dict[str, object]) -> None:
    expected_keys = {
        "suite_id",
        "department_id",
        "import_attempt_id",
        "stage_id",
        "suite_contract_version",
        "metric_contract_version",
        "answer_normalization_version",
        "gate_policy_version",
        "case_count",
        "answered_case_count",
        "insufficient_case_count",
        "gates",
        "artifact_contract_version",
        "files",
    }
    if (
        set(value) != expected_keys
        or value.get("artifact_contract_version") != ARTIFACT_CONTRACT_VERSION
    ):
        raise EvaluationContractError("suite_artifact_mismatch")
    if not all(
        _valid_uuid(value.get(name))
        for name in ("suite_id", "department_id", "import_attempt_id", "stage_id")
    ):
        raise EvaluationContractError("suite_artifact_mismatch")
    if not all(
        isinstance(value.get(name), int) and not isinstance(value.get(name), bool)
        for name in ("case_count", "answered_case_count", "insufficient_case_count")
    ):
        raise EvaluationContractError("suite_artifact_mismatch")
    gates = value.get("gates")
    files = value.get("files")
    if (
        not isinstance(gates, dict)
        or set(gates) != set(GATE_NAMES)
        or not isinstance(files, dict)
        or set(files) != {"cases.jsonl"}
        or not _validate_digest_value(files.get("cases.jsonl"))
    ):
        raise EvaluationContractError("suite_artifact_mismatch")


def _validate_run_manifest_shape(value: dict[str, object]) -> None:
    expected_keys = {
        "run_id",
        "suite_id",
        "department_id",
        "publication_attempt_id",
        "attempt_number",
        "metric_contract_version",
        "runner_contract_version",
        "answer_normalization_version",
        "gate_policy_version",
        "seed_derivation_version",
        "base_seed",
        "code_revision",
        "case_count",
        "query_embedding_pipeline_version",
        "query_embedding_model_id",
        "query_embedding_model_revision",
        "query_embedding_dimension",
        "query_embedding_distance",
        "generation_model_id",
        "generation_model_revision",
        "prompt_version",
        "answer_contract_version",
        "qdrant_collection",
        "vector_schema_version",
        "artifact_contract_version",
        "files",
    }
    if (
        set(value) != expected_keys
        or value.get("artifact_contract_version") != ARTIFACT_CONTRACT_VERSION
    ):
        raise EvaluationContractError("result_publication_failed")
    if not all(
        _valid_uuid(value.get(name))
        for name in ("run_id", "suite_id", "department_id", "publication_attempt_id")
    ):
        raise EvaluationContractError("result_publication_failed")
    if (
        not isinstance(value.get("base_seed"), int)
        or isinstance(value.get("base_seed"), bool)
        or not _valid_positive_integer(value.get("attempt_number"))
        or not isinstance(value.get("case_count"), int)
        or isinstance(value.get("case_count"), bool)
        or not isinstance(value.get("query_embedding_dimension"), int)
        or isinstance(value.get("query_embedding_dimension"), bool)
        or not isinstance(value.get("code_revision"), str)
        or len(value["code_revision"]) != 40
        or any(character not in "0123456789abcdef" for character in value["code_revision"])
    ):
        raise EvaluationContractError("result_publication_failed")
    files = value.get("files")
    if (
        not isinstance(files, dict)
        or set(files) != {"summary.json", "case_results.jsonl"}
        or not all(_validate_digest_value(files.get(name)) for name in files)
    ):
        raise EvaluationContractError("result_publication_failed")


def _validate_run_summary_shape(value: dict[str, object]) -> None:
    metric_keys = {
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
    expected = {
        "case_count",
        "metrics",
        "gates",
        "gate_results",
        "gate_status",
        "failed_gate_count",
    }
    if set(value) != expected:
        raise EvaluationContractError("result_publication_failed")
    if not isinstance(value.get("case_count"), int) or isinstance(value.get("case_count"), bool):
        raise EvaluationContractError("result_publication_failed")
    if (
        not isinstance(value.get("metrics"), dict)
        or set(value["metrics"]) != metric_keys
        or not isinstance(value.get("gates"), dict)
        or set(value["gates"]) != set(GATE_NAMES)
        or not isinstance(value.get("gate_results"), dict)
        or set(value["gate_results"]) != set(GATE_NAMES)
        or value.get("gate_status") not in {"passed", "failed"}
        or not isinstance(value.get("failed_gate_count"), int)
        or isinstance(value.get("failed_gate_count"), bool)
    ):
        raise EvaluationContractError("result_publication_failed")


def _parse_json_object(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EvaluationContractError("suite_artifact_mismatch") from error
    if not isinstance(value, dict):
        raise EvaluationContractError("suite_artifact_mismatch")
    return value


def _validate_canonical_suite_manifest(
    value: dict[str, object],
    *,
    cases_sha256: str,
    cases_byte_size: int,
) -> None:
    _validate_suite_manifest_shape(value)
    if value.get("files") != {
        "cases.jsonl": {
            "sha256": cases_sha256,
            "byte_size": cases_byte_size,
        }
    }:
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


def _path_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True
