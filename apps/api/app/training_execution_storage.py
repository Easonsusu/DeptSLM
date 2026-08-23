"""Descriptor-bound Phase 14.1 execution attempt storage.

Only the five retained Phase 11 job files are copied into an exact private
attempt input directory.  The control plane never parses or stores their
contents in PostgreSQL, logs, or API responses.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from app.authorization import DepartmentScope
from app.models import TrainingJob
from app.sft_artifacts import ArtifactDigest, SftArtifactError, SftArtifactStore
from app.training_execution_domain import authority_fingerprint
from app.training_job_domain import TRAINING_JOB_FILES


class TrainingExecutionStorageError(RuntimeError):
    def __init__(self, code: str = "input_snapshot_failed") -> None:
        self.code = code
        super().__init__(code)


@dataclass(slots=True)
class TrainingExecutionAttemptStage:
    department_fd: int
    execution_fd: int
    attempts_fd: int
    attempt_fd: int
    input_fd: int
    scratch_fd: int
    logs_fd: int
    output_stage_fd: int

    def close(self) -> None:
        for name in (
            "output_stage_fd",
            "logs_fd",
            "scratch_fd",
            "input_fd",
            "attempt_fd",
            "attempts_fd",
            "execution_fd",
            "department_fd",
        ):
            descriptor = getattr(self, name, None)
            if descriptor is not None and descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                setattr(self, name, -1)


@dataclass(frozen=True, slots=True)
class InputSnapshot:
    fingerprint: str
    files: tuple[tuple[str, ArtifactDigest], ...]


def _private_directory(descriptor: int) -> None:
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise TrainingExecutionStorageError("input_snapshot_failed")
    if metadata.st_uid != os.getuid():
        raise TrainingExecutionStorageError("input_snapshot_failed")


def _ensure_child(parent: int, name: str) -> int:
    if not name or "/" in name or name in {".", ".."}:
        raise TrainingExecutionStorageError()
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    except FileNotFoundError:
        os.mkdir(name, 0o700, dir_fd=parent)
        descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        _private_directory(descriptor)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _fresh_child(parent: int, name: str) -> int:
    if not name or "/" in name or name in {".", ".."}:
        raise TrainingExecutionStorageError()
    try:
        os.mkdir(name, 0o700, dir_fd=parent)
        descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            _private_directory(descriptor)
            return descriptor
        except Exception:
            os.close(descriptor)
            raise
    except FileExistsError as error:
        raise TrainingExecutionStorageError("input_snapshot_failed") from error
    except (OSError, TrainingExecutionStorageError) as error:
        raise TrainingExecutionStorageError("input_snapshot_failed") from error


def _open_root(data_dir: Path) -> int:
    if not isinstance(data_dir, Path) or not data_dir.is_absolute():
        raise TrainingExecutionStorageError("input_snapshot_failed")
    try:
        if not data_dir.is_dir():
            raise TrainingExecutionStorageError("input_snapshot_failed")
        descriptor = os.open(data_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        _private_directory(descriptor)
        return descriptor
    except (OSError, TrainingExecutionStorageError) as error:
        raise TrainingExecutionStorageError("input_snapshot_failed") from error


class TrainingExecutionArtifactStore:
    """Owns one descriptor-rooted ``training_runs`` tree."""

    def __init__(self, data_dir: Path) -> None:
        if not isinstance(data_dir, Path) or not data_dir.is_absolute():
            raise TrainingExecutionStorageError("input_snapshot_failed")
        self._data_dir = data_dir
        root = _open_root(data_dir)
        try:
            self._root_fd = _ensure_child(root, "training_runs")
        finally:
            os.close(root)

    def close(self) -> None:
        descriptor = getattr(self, "_root_fd", None)
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
            self._root_fd = None

    def __enter__(self) -> TrainingExecutionArtifactStore:
        return self

    def __exit__(self, *_ignored: object) -> None:
        self.close()

    def create_attempt(
        self, department_id: UUID, execution_id: UUID, attempt_id: UUID
    ) -> TrainingExecutionAttemptStage:
        descriptors: list[int] = []
        try:
            department = _ensure_child(self._root_fd, str(department_id))
            descriptors.append(department)
            execution = _ensure_child(department, str(execution_id))
            descriptors.append(execution)
            attempts = _ensure_child(execution, "attempts")
            descriptors.append(attempts)
            attempt = _fresh_child(attempts, str(attempt_id))
            descriptors.append(attempt)
            input_fd = _ensure_child(attempt, "input")
            descriptors.append(input_fd)
            scratch_fd = _ensure_child(attempt, "scratch")
            descriptors.append(scratch_fd)
            logs_fd = _ensure_child(attempt, "logs")
            descriptors.append(logs_fd)
            output_fd = _ensure_child(attempt, "output_stage")
            descriptors.append(output_fd)
            return TrainingExecutionAttemptStage(
                department, execution, attempts, attempt, input_fd, scratch_fd, logs_fd, output_fd
            )
        except (OSError, TrainingExecutionStorageError) as error:
            for descriptor in reversed(descriptors):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if isinstance(error, TrainingExecutionStorageError):
                raise
            raise TrainingExecutionStorageError("input_snapshot_failed") from error

    @staticmethod
    def _expected_manifest(job: TrainingJob) -> dict[str, object]:
        return {
            "artifact_contract_version": job.artifact_contract_version,
            "manifest_contract_version": job.manifest_contract_version,
            "configuration_contract_version": job.configuration_contract_version,
            "dataset_info_contract_version": job.dataset_info_contract_version,
            "execution_profile_contract_version": job.execution_profile_contract_version,
            "department_id": str(job.department_id),
            "training_job_id": str(job.id),
            "publication_attempt_id": str(job.publication_attempt_id),
            "execution_scope_id": str(job.execution_scope_id),
            "attempt_number": job.attempt_number,
            "code_revision": job.code_revision,
            "dataset_build_id": str(job.dataset_build_id),
            "dataset_build_version": job.dataset_build_version,
            "dataset_artifact_contract_version": job.dataset_artifact_contract_version,
            "dataset_example_contract_version": job.dataset_example_contract_version,
            "dataset_normalization_version": job.dataset_normalization_version,
            "dataset_split_version": job.dataset_split_version,
            "dataset_manifest_sha256": job.dataset_manifest_sha256,
            "train_example_count": job.train_example_count,
            "validation_example_count": job.validation_example_count,
            "base_model_id": job.base_model_id,
            "base_model_revision": job.base_model_revision,
            "base_model_license": job.base_model_license,
            "llamafactory_version": job.llamafactory_version,
            "profile_id": job.profile_id,
            "maximum_record_content_bytes": job.maximum_record_content_bytes,
            "tokenizer_preflight_required": True,
            "dataset_rights_attested": job.dataset_rights_attested,
            "evaluation_contamination_reviewed": job.evaluation_contamination_reviewed,
            "files": {
                "training.yaml": {
                    "sha256": job.training_config_sha256,
                    "byte_size": job.training_config_byte_size,
                },
                "dataset_info.json": {
                    "sha256": job.dataset_info_sha256,
                    "byte_size": job.dataset_info_byte_size,
                },
                "train.jsonl": {"sha256": job.train_sha256, "byte_size": job.train_byte_size},
                "validation.jsonl": {
                    "sha256": job.validation_sha256,
                    "byte_size": job.validation_byte_size,
                },
            },
        }

    def snapshot_phase11_final(
        self,
        stage: TrainingExecutionAttemptStage,
        *,
        scope: DepartmentScope,
        job: TrainingJob,
    ) -> InputSnapshot:
        expected = self._expected_manifest(job)
        source: SftArtifactStore | None = None
        verified = None
        try:
            source = SftArtifactStore(self._data_dir)
            verified = source.open_retained_final(
                scope,
                job.id,
                category="training_job",
                attempt_id=job.publication_attempt_id,
                allowlist=TRAINING_JOB_FILES,
                expected=expected,
            )
            copied: dict[str, ArtifactDigest] = {}
            for name in sorted(TRAINING_JOB_FILES):
                source_fd, _metadata = verified.descriptor(name)
                os.lseek(source_fd, 0, os.SEEK_SET)
                destination = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=stage.input_fd,
                )
                digest = hashlib.sha256()
                size = 0
                try:
                    while True:
                        block = os.read(source_fd, 64 * 1024)
                        if not block:
                            break
                        view = memoryview(block)
                        while view:
                            written = os.write(destination, view)
                            if written <= 0:
                                raise TrainingExecutionStorageError("input_snapshot_failed")
                            view = view[written:]
                        digest.update(block)
                        size += len(block)
                    os.fsync(destination)
                finally:
                    os.close(destination)
                expected_digest = dict(verified.files)[name]
                actual = ArtifactDigest(digest.hexdigest(), size)
                if actual != expected_digest:
                    raise TrainingExecutionStorageError("input_snapshot_failed")
                copied[name] = actual
            os.fsync(stage.input_fd)
            fingerprint = authority_fingerprint(
                {
                    "training_job_id": str(job.id),
                    "department_id": str(scope.value),
                    "files": {
                        name: {"sha256": value.sha256, "byte_size": value.byte_size}
                        for name, value in sorted(copied.items())
                    },
                }
            )
            return InputSnapshot(fingerprint, tuple(sorted(copied.items())))
        except (SftArtifactError, OSError, TrainingExecutionStorageError) as error:
            if isinstance(error, TrainingExecutionStorageError):
                raise
            raise TrainingExecutionStorageError("input_snapshot_failed") from error
        finally:
            if verified is not None:
                verified.close()
            if source is not None:
                source.close()


__all__ = [
    "InputSnapshot",
    "TrainingExecutionArtifactStore",
    "TrainingExecutionAttemptStage",
    "TrainingExecutionStorageError",
]
