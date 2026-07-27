"""Private descriptor-checked Phase 10 SFT source and dataset artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from app.authorization import DepartmentScope

SOURCE_FILES = frozenset({"manifest.json", "examples.jsonl"})
DATASET_FILES = frozenset({"manifest.json", "train.jsonl", "validation.jsonl", "provenance.jsonl"})
STAGE_MARKER = ".deptslm-stage-owner"
_MARKER_BYTES = b"deptslm-sft-stage-v1\n"


class SftArtifactError(RuntimeError):
    def __init__(self, code: str = "dataset_publication_failed") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ArtifactDigest:
    sha256: str
    byte_size: int


@dataclass(frozen=True, slots=True)
class SftStagedArtifact:
    path: Path
    final_path: Path
    files: tuple[tuple[str, ArtifactDigest], ...]

    @property
    def manifest(self) -> ArtifactDigest:
        return dict(self.files)["manifest.json"]


class SftArtifactStore:
    """Use UUID-only paths below the pre-existing external ``training_datasets`` root."""

    def __init__(self, data_dir: Path) -> None:
        self.root = _open_root(data_dir / "training_datasets", writable=True)
        self.sources = _ensure_private_directory(self.root, "sources")
        self.datasets = _ensure_private_directory(self.root, "datasets")
        staging = _ensure_private_directory(self.root, ".staging")
        self.staging_sources = _ensure_private_directory(staging, "sources")
        self.staging_datasets = _ensure_private_directory(staging, "datasets")

    def stage_source(
        self,
        scope: DepartmentScope,
        source_bundle_id: UUID,
        import_attempt_id: UUID,
        *,
        manifest: bytes,
        examples: bytes,
    ) -> SftStagedArtifact:
        return self._stage(
            self.staging_sources,
            self.sources,
            scope,
            source_bundle_id,
            import_attempt_id,
            (("examples.jsonl", examples), ("manifest.json", manifest)),
            SOURCE_FILES,
        )

    def stage_dataset(
        self,
        scope: DepartmentScope,
        build_id: UUID,
        publication_attempt_id: UUID,
        *,
        manifest: bytes,
        train: bytes,
        validation: bytes,
        provenance: bytes,
    ) -> SftStagedArtifact:
        return self._stage(
            self.staging_datasets,
            self.datasets,
            scope,
            build_id,
            publication_attempt_id,
            (
                ("manifest.json", manifest),
                ("provenance.jsonl", provenance),
                ("train.jsonl", train),
                ("validation.jsonl", validation),
            ),
            DATASET_FILES,
        )

    def publish(self, staged: SftStagedArtifact, *, allowlist: frozenset[str]) -> SftStagedArtifact:
        try:
            _verify_files(staged.path, allowlist, dict(staged.files), marker_allowed=True)
            _remove_marker(staged.path)
            _verify_files(staged.path, allowlist, dict(staged.files), marker_allowed=False)
            if staged.final_path.exists():
                raise SftArtifactError()
            parent = _ensure_private_directory(
                staged.final_path.parent.parent, staged.final_path.parent.name
            )
            if parent != staged.final_path.parent:
                raise SftArtifactError()
            os.rename(staged.path, staged.final_path)
            os.chmod(staged.final_path, 0o700)
            verified = _verify_files(
                staged.final_path, allowlist, dict(staged.files), marker_allowed=False
            )
            return SftStagedArtifact(
                staged.final_path, staged.final_path, tuple(sorted(verified.items()))
            )
        except OSError as error:
            raise SftArtifactError() from error

    def remove_owned_source_stage(
        self, scope: DepartmentScope, source_bundle_id: UUID, import_attempt_id: UUID
    ) -> bool:
        return _remove_stage(self.staging_sources, scope, source_bundle_id, import_attempt_id)

    def remove_owned_dataset_stage(
        self, scope: DepartmentScope, build_id: UUID, publication_attempt_id: UUID
    ) -> bool:
        return _remove_stage(self.staging_datasets, scope, build_id, publication_attempt_id)

    def remove_owned_source_final(
        self,
        scope: DepartmentScope,
        source_bundle_id: UUID,
        import_attempt_id: UUID,
        *,
        manifest_sha256: str,
        examples_sha256: str,
    ) -> bool:
        return _remove_final(
            self.sources,
            scope,
            source_bundle_id,
            manifest_sha256=manifest_sha256,
            expected={
                "department_id": str(scope.value),
                "source_bundle_id": str(source_bundle_id),
                "import_attempt_id": str(import_attempt_id),
                "files": {"examples.jsonl": examples_sha256},
            },
        )

    def remove_owned_dataset_final(
        self,
        scope: DepartmentScope,
        build_id: UUID,
        publication_attempt_id: UUID,
        *,
        attempt_number: int,
        code_revision: str,
        manifest_sha256: str,
        train_sha256: str,
        validation_sha256: str,
        provenance_sha256: str,
    ) -> bool:
        return _remove_final(
            self.datasets,
            scope,
            build_id,
            manifest_sha256=manifest_sha256,
            expected={
                "department_id": str(scope.value),
                "build_id": str(build_id),
                "publication_attempt_id": str(publication_attempt_id),
                "attempt_number": attempt_number,
                "code_revision": code_revision,
                "files": {
                    "train.jsonl": train_sha256,
                    "validation.jsonl": validation_sha256,
                    "provenance.jsonl": provenance_sha256,
                },
            },
        )

    def read_source(self, scope: DepartmentScope, source_bundle_id: UUID) -> tuple[bytes, bytes]:
        path = self.sources / str(scope.value) / str(source_bundle_id)
        directory = _open_directory(path)
        try:
            verified = _verify_files(directory, SOURCE_FILES, {}, marker_allowed=False)
            manifest = _read_file_at(directory, "manifest.json", maximum=128 * 1024)
            examples = _read_file_at(directory, "examples.jsonl", maximum=512 * 1024 * 1024)
            _verify_files(directory, SOURCE_FILES, verified, marker_allowed=False)
            return manifest, examples
        finally:
            os.close(directory)

    def _stage(
        self,
        staging_root: Path,
        final_root: Path,
        scope: DepartmentScope,
        resource_id: UUID,
        attempt_id: UUID,
        values: tuple[tuple[str, bytes], ...],
        allowlist: frozenset[str],
    ) -> SftStagedArtifact:
        if not isinstance(scope, DepartmentScope) or resource_id.int == 0 or attempt_id.int == 0:
            raise SftArtifactError()
        try:
            department = _ensure_private_directory(staging_root, str(scope.value))
            resource = _ensure_private_directory(department, str(resource_id))
            stage = resource / str(attempt_id)
            os.mkdir(stage, 0o700)
            os.chmod(stage, 0o700)
            _write_file(stage, STAGE_MARKER, _MARKER_BYTES)
            files = tuple((name, _write_file(stage, name, value)) for name, value in values)
            _verify_files(stage, allowlist, dict(files), marker_allowed=True)
            return SftStagedArtifact(stage, final_root / str(scope.value) / str(resource_id), files)
        except OSError as error:
            raise SftArtifactError() from error


def _open_root(path: Path, *, writable: bool) -> Path:
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise SftArtifactError()
        if not path.is_absolute() or (writable and not os.access(path, os.W_OK | os.X_OK)):
            raise SftArtifactError()
        return path
    except OSError as error:
        raise SftArtifactError() from error


def _ensure_private_directory(parent: Path, name: str) -> Path:
    if not name or "/" in name or name in {".", ".."}:
        raise SftArtifactError()
    path = parent / name
    try:
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
        metadata = path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_mode & 0o077
        ):
            raise SftArtifactError()
        os.chmod(path, 0o700)
        return path
    except OSError as error:
        raise SftArtifactError() from error


def _open_directory(path: Path) -> int:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_mode & 0o077:
            raise SftArtifactError()
        return descriptor
    except OSError as error:
        raise SftArtifactError("source_artifact_missing") from error


def _read_file_at(directory: int, name: str, *, maximum: int) -> bytes:
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or not 1 <= before.st_size <= maximum
        ):
            raise SftArtifactError("source_artifact_mismatch")
        output = bytearray()
        while True:
            chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - len(output)))
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > maximum:
                raise SftArtifactError("source_artifact_mismatch")
        after = os.fstat(descriptor)
        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if not _same_file(before, after) or not _same_file(before, current):
            raise SftArtifactError("source_artifact_mismatch")
        return bytes(output)
    except OSError as error:
        raise SftArtifactError("source_artifact_mismatch") from error
    finally:
        try:
            os.close(descriptor)
        except (OSError, UnboundLocalError):
            pass


def _write_file(directory: Path, name: str, value: bytes) -> ArtifactDigest:
    if not isinstance(value, bytes) or not value:
        raise SftArtifactError()
    try:
        descriptor = os.open(
            directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        return ArtifactDigest(hashlib.sha256(value).hexdigest(), len(value))
    except OSError as error:
        raise SftArtifactError() from error


def _verify_files(
    path_or_descriptor: Path | int,
    allowlist: frozenset[str],
    expected: dict[str, ArtifactDigest],
    *,
    marker_allowed: bool,
) -> dict[str, ArtifactDigest]:
    close = isinstance(path_or_descriptor, Path)
    descriptor = _open_directory(path_or_descriptor) if close else path_or_descriptor
    try:
        names = set(os.listdir(descriptor))
        permitted = set(allowlist) | ({STAGE_MARKER} if marker_allowed else set())
        if names != permitted:
            raise SftArtifactError()
        verified = {name: _digest_file(descriptor, name) for name in sorted(allowlist)}
        if expected and verified != expected:
            raise SftArtifactError()
        _verify_manifest(
            _read_file_at(descriptor, "manifest.json", maximum=256 * 1024), allowlist, verified
        )
        return verified
    finally:
        if close:
            os.close(descriptor)


def _digest_file(directory: int, name: str) -> ArtifactDigest:
    raw = _read_file_at(directory, name, maximum=512 * 1024 * 1024)
    return ArtifactDigest(hashlib.sha256(raw).hexdigest(), len(raw))


def _verify_manifest(
    raw: bytes, allowlist: frozenset[str], verified: dict[str, ArtifactDigest]
) -> None:
    def reject_duplicates(pairs):
        value = dict(pairs)
        if len(value) != len(pairs):
            raise ValueError()
        return value

    try:
        manifest = json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicates)
        declared = manifest["files"]
        payloads = allowlist - {"manifest.json"}
        if not isinstance(manifest, dict) or set(declared) != payloads:
            raise ValueError()
        for name in payloads:
            value = declared[name]
            digest = verified[name]
            if value != {"sha256": digest.sha256, "byte_size": digest.byte_size}:
                raise ValueError()
    except (KeyError, TypeError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        raise SftArtifactError() from error


def _remove_marker(path: Path) -> None:
    descriptor = _open_directory(path)
    try:
        os.unlink(STAGE_MARKER, dir_fd=descriptor)
    except OSError as error:
        raise SftArtifactError() from error
    finally:
        os.close(descriptor)


def _remove_stage(root: Path, scope: DepartmentScope, resource_id: UUID, attempt_id: UUID) -> bool:
    if resource_id.int == 0 or attempt_id.int == 0:
        raise SftArtifactError()
    descriptors: list[int] = []
    try:
        descriptor = _open_directory(root)
        descriptors.append(descriptor)
        for name in (str(scope.value), str(resource_id), str(attempt_id)):
            descriptor = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            metadata = os.fstat(descriptor)
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_mode & 0o077:
                raise SftArtifactError()
            descriptors.append(descriptor)
        _remove_contents(descriptors[-1])
        parent = descriptors[-2]
        before = os.fstat(descriptors[-1])
        current = os.stat(str(attempt_id), dir_fd=parent, follow_symlinks=False)
        if current.st_dev != before.st_dev or current.st_ino != before.st_ino:
            raise SftArtifactError()
        os.rmdir(str(attempt_id), dir_fd=parent)
        return True
    except FileNotFoundError:
        return False
    except OSError as error:
        raise SftArtifactError() from error
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _remove_final(
    root: Path,
    scope: DepartmentScope,
    resource_id: UUID,
    *,
    manifest_sha256: str,
    expected: dict[str, object],
) -> bool:
    descriptors: list[int] = []
    try:
        descriptor = _open_directory(root)
        descriptors.append(descriptor)
        for name in (str(scope.value), str(resource_id)):
            descriptor = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            metadata = os.fstat(descriptor)
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_mode & 0o077:
                raise SftArtifactError()
            descriptors.append(descriptor)
        files = expected["files"]
        if not isinstance(files, dict):
            raise SftArtifactError()
        verified = _verify_files(
            descriptors[-1], frozenset({"manifest.json", *files}), {}, marker_allowed=False
        )
        if verified["manifest.json"].sha256 != manifest_sha256:
            raise SftArtifactError()
        manifest = _parse_manifest(
            _read_file_at(descriptors[-1], "manifest.json", maximum=256 * 1024)
        )
        if any(manifest.get(key) != value for key, value in expected.items() if key != "files"):
            raise SftArtifactError()
        declared_files = manifest.get("files")
        if not isinstance(declared_files, dict) or set(declared_files) != set(files):
            raise SftArtifactError()
        for name, digest in files.items():
            value = declared_files[name]
            if (
                not isinstance(value, dict)
                or value.get("sha256") != digest
                or value.get("sha256") != verified[name].sha256
                or value.get("byte_size") != verified[name].byte_size
            ):
                raise SftArtifactError()
        parent = descriptors[-2]
        target = descriptors[-1]
        before = os.fstat(target)
        current = os.stat(str(resource_id), dir_fd=parent, follow_symlinks=False)
        if current.st_dev != before.st_dev or current.st_ino != before.st_ino:
            raise SftArtifactError()
        _remove_contents(target)
        os.rmdir(str(resource_id), dir_fd=parent)
        return True
    except FileNotFoundError:
        return False
    except OSError as error:
        raise SftArtifactError() from error
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _parse_manifest(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise SftArtifactError() from error
    if not isinstance(value, dict):
        raise SftArtifactError()
    return value


def _reject_duplicate_pairs(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError()
    return value


def _remove_contents(directory: int) -> None:
    for name in os.listdir(directory):
        metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            try:
                _remove_contents(child)
                os.rmdir(name, dir_fd=directory)
            finally:
                os.close(child)
        elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
            os.unlink(name, dir_fd=directory)
        else:
            raise SftArtifactError()


def _same_file(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        stat.S_ISREG(second.st_mode)
        and first.st_dev == second.st_dev
        and first.st_ino == second.st_ino
        and first.st_size == second.st_size
        and first.st_nlink == second.st_nlink == 1
        and first.st_mtime_ns == second.st_mtime_ns
        and first.st_ctime_ns == second.st_ctime_ns
    )
