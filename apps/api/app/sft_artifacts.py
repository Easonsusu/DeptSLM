"""Descriptor-bound, private Phase 10 SFT source and dataset artifacts.

No public API or persistent metadata contains a filesystem path.  Every
operation below the external ``training_datasets`` root is descriptor-relative
and rejects substituted, linked, non-private, or foreign-UID entries.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from app.authorization import DepartmentScope

SOURCE_FILES = frozenset({"manifest.json", "examples.jsonl"})
DATASET_FILES = frozenset({"manifest.json", "train.jsonl", "validation.jsonl", "provenance.jsonl"})
STAGE_MARKER = ".deptslm-stage-owner"
_MARKER_BYTES = b"deptslm-sft-stage-v1\n"
_Checkpoint = Callable[[], None]


class SftArtifactError(RuntimeError):
    def __init__(self, code: str = "dataset_publication_failed") -> None:
        self.code = (
            code
            if code
            in {
                "source_artifact_missing",
                "source_artifact_mismatch",
                "dataset_publication_failed",
                "artifact_ownership_mismatch",
                "artifact_permissions_invalid",
                "staging_path_unsafe",
            }
            else "dataset_publication_failed"
        )
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class ArtifactDigest:
    sha256: str
    byte_size: int


@dataclass(slots=True)
class SftStagedArtifact:
    """An exact descriptor chain retained from staging through publication."""

    category: str
    scope: DepartmentScope
    resource_id: UUID
    attempt_id: UUID
    files: tuple[tuple[str, ArtifactDigest], ...]
    stage_parent_fd: int | None
    stage_fd: int | None
    final_parent_fd: int | None

    @property
    def manifest(self) -> ArtifactDigest:
        return dict(self.files)["manifest.json"]

    def close(self) -> None:
        for attribute in ("stage_fd", "stage_parent_fd", "final_parent_fd"):
            descriptor = getattr(self, attribute)
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                setattr(self, attribute, None)


@dataclass(slots=True)
class SftFinalArtifactVerification:
    """A verified final artifact whose directory and files remain open.

    The caller must keep this object alive through its short PostgreSQL commit.
    It deliberately retains only descriptors and content-free digests; file
    contents are never copied into process metadata.
    """

    artifact: SftStagedArtifact
    allowlist: frozenset[str]
    files: tuple[tuple[str, ArtifactDigest], ...]
    file_descriptors: tuple[tuple[str, int, os.stat_result], ...]

    def close(self) -> None:
        for _name, descriptor, _metadata in self.file_descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass
        self.file_descriptors = ()
        self.artifact.close()

    def recheck_identity(self) -> None:
        """Bind an earlier complete hash to the still-open final artifact.

        This intentionally does not rehash large artifacts while database locks
        are held.  Stable descriptor, entry, and directory identities prove that
        the earlier digest applies to the object being committed.
        """

        artifact = self.artifact
        if artifact.stage_fd is None or artifact.final_parent_fd is None:
            raise SftArtifactError("artifact_ownership_mismatch")
        _require_private_directory(artifact.stage_fd, writable=True)
        _entry_matches(artifact.final_parent_fd, str(artifact.resource_id), artifact.stage_fd)
        if set(os.listdir(artifact.stage_fd)) != set(self.allowlist):
            raise SftArtifactError("artifact_ownership_mismatch")
        expected = dict(self.files)
        for name, descriptor, before in self.file_descriptors:
            after = os.fstat(descriptor)
            current = os.stat(name, dir_fd=artifact.stage_fd, follow_symlinks=False)
            if not _same_file(before, after) or not _same_file(before, current):
                raise SftArtifactError("artifact_ownership_mismatch")
        # The retained manifest descriptor is part of the same identity set;
        # checking its digest record prevents a caller from substituting an
        # incomplete or unrelated result between preverification and commit.
        if set(expected) != set(self.allowlist):
            raise SftArtifactError("artifact_ownership_mismatch")


class SftArtifactStore:
    """External storage rooted once at a private, service-owned descriptor."""

    def __init__(self, data_dir: Path) -> None:
        if not isinstance(data_dir, Path) or not data_dir.is_absolute():
            raise SftArtifactError("artifact_permissions_invalid")
        self._root_fd = _open_directory_path(data_dir / "training_datasets", writable=True)
        self._sources_fd = _ensure_private_child(self._root_fd, "sources")
        self._datasets_fd = _ensure_private_child(self._root_fd, "datasets")
        staging = _ensure_private_child(self._root_fd, ".staging")
        self._staging_sources_fd = _ensure_private_child(staging, "sources")
        self._staging_datasets_fd = _ensure_private_child(staging, "datasets")
        os.close(staging)

    def close(self) -> None:
        for descriptor in (
            getattr(self, "_staging_datasets_fd", None),
            getattr(self, "_staging_sources_fd", None),
            getattr(self, "_datasets_fd", None),
            getattr(self, "_sources_fd", None),
            getattr(self, "_root_fd", None),
        ):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def __enter__(self) -> SftArtifactStore:
        return self

    def __exit__(self, *_ignored: object) -> None:
        self.close()

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
            self._staging_sources_fd,
            self._sources_fd,
            "source",
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
            self._staging_datasets_fd,
            self._datasets_fd,
            "dataset",
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

    def prepare_dataset_stage(
        self,
        scope: DepartmentScope,
        build_id: UUID,
        publication_attempt_id: UUID,
    ) -> SftStagedArtifact:
        """Create only the exact private stage directory for a child writer."""

        return self._prepare_stage(
            self._staging_datasets_fd,
            self._datasets_fd,
            "dataset",
            scope,
            build_id,
            publication_attempt_id,
        )

    def publish(
        self,
        staged: SftStagedArtifact,
        *,
        allowlist: frozenset[str],
        expected: dict[str, object] | None = None,
        retain: bool = False,
        checkpoint: _Checkpoint | None = None,
    ) -> SftStagedArtifact:
        if (
            staged.stage_parent_fd is None
            or staged.stage_fd is None
            or staged.final_parent_fd is None
        ):
            raise SftArtifactError("artifact_ownership_mismatch")
        if allowlist not in {SOURCE_FILES, DATASET_FILES}:
            raise SftArtifactError()
        try:
            check = checkpoint or _noop
            check()
            _entry_matches(staged.stage_parent_fd, str(staged.attempt_id), staged.stage_fd)
            verified = _verify_files(
                staged.stage_fd,
                allowlist,
                dict(staged.files),
                marker_allowed=True,
                checkpoint=check,
            )
            if expected is not None:
                _require_exact_manifest(
                    _parse_manifest(
                        _read_file_at(
                            staged.stage_fd,
                            "manifest.json",
                            maximum=256 * 1024,
                            checkpoint=check,
                        )
                    ),
                    allowlist,
                    verified,
                    expected,
                )
            check()
            _unlink_exact(staged.stage_fd, STAGE_MARKER)
            _verify_files(
                staged.stage_fd,
                allowlist,
                dict(staged.files),
                marker_allowed=False,
                checkpoint=check,
            )
            try:
                os.stat(
                    str(staged.resource_id), dir_fd=staged.final_parent_fd, follow_symlinks=False
                )
            except FileNotFoundError:
                pass
            else:
                raise SftArtifactError("artifact_ownership_mismatch")
            check()
            os.rename(
                str(staged.attempt_id),
                str(staged.resource_id),
                src_dir_fd=staged.stage_parent_fd,
                dst_dir_fd=staged.final_parent_fd,
            )
            _fsync(staged.stage_parent_fd)
            check()
            _fsync(staged.final_parent_fd)
            _entry_matches(staged.final_parent_fd, str(staged.resource_id), staged.stage_fd)
            verified = _verify_files(
                staged.stage_fd,
                allowlist,
                dict(staged.files),
                marker_allowed=False,
                checkpoint=check,
            )
            # Keep the exact final directory and its parent descriptor open so
            # final verification can be bound to the later metadata commit.
            if staged.stage_parent_fd is not None:
                os.close(staged.stage_parent_fd)
                staged.stage_parent_fd = None
            staged.files = tuple(sorted(verified.items()))
            if retain:
                return staged
            result = SftStagedArtifact(
                staged.category,
                staged.scope,
                staged.resource_id,
                staged.attempt_id,
                staged.files,
                None,
                None,
                None,
            )
            staged.close()
            return result
        except OSError as error:
            staged.close()
            raise SftArtifactError("dataset_publication_failed") from error

    def read_source(self, scope: DepartmentScope, source_bundle_id: UUID) -> tuple[bytes, bytes]:
        directory = self._open_final(self._sources_fd, scope, source_bundle_id)
        try:
            verified = _verify_files(directory, SOURCE_FILES, {}, marker_allowed=False)
            manifest = _read_file_at(directory, "manifest.json", maximum=128 * 1024)
            examples = _read_file_at(directory, "examples.jsonl", maximum=512 * 1024 * 1024)
            _verify_files(directory, SOURCE_FILES, verified, marker_allowed=False)
            return manifest, examples
        finally:
            os.close(directory)

    def open_source_directory(self, scope: DepartmentScope, source_bundle_id: UUID) -> int:
        """Return one verified, descriptor-relative source directory handle.

        The caller owns the returned descriptor and may pass only that handle to
        the isolated child.  No path is passed across the execution boundary.
        """

        return self._open_final(self._sources_fd, scope, source_bundle_id)

    def verify_source_final(
        self,
        scope: DepartmentScope,
        source_bundle_id: UUID,
        *,
        expected: dict[str, object],
    ) -> dict[str, ArtifactDigest]:
        return self._verify_final(self._sources_fd, scope, source_bundle_id, SOURCE_FILES, expected)

    def verify_dataset_final(
        self,
        scope: DepartmentScope,
        build_id: UUID,
        *,
        expected: dict[str, object],
    ) -> dict[str, ArtifactDigest]:
        return self._verify_final(self._datasets_fd, scope, build_id, DATASET_FILES, expected)

    def verify_retained_final(
        self,
        artifact: SftStagedArtifact,
        *,
        allowlist: frozenset[str],
        expected: dict[str, object],
        checkpoint: _Checkpoint | None = None,
    ) -> SftFinalArtifactVerification:
        """Hash once outside locks and retain descriptors for commit-time proof."""

        if artifact.stage_fd is None or artifact.final_parent_fd is None:
            raise SftArtifactError("artifact_ownership_mismatch")
        if allowlist not in {SOURCE_FILES, DATASET_FILES}:
            raise SftArtifactError()
        check = checkpoint or _noop
        check()
        _require_private_directory(artifact.stage_fd, writable=True)
        _entry_matches(artifact.final_parent_fd, str(artifact.resource_id), artifact.stage_fd)
        names = set(os.listdir(artifact.stage_fd))
        if names != set(allowlist):
            raise SftArtifactError("artifact_ownership_mismatch")
        descriptors: list[tuple[str, int, os.stat_result]] = []
        try:
            files: dict[str, ArtifactDigest] = {}
            manifest_raw: bytes | None = None
            for name in sorted(allowlist):
                check()
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=artifact.stage_fd)
                try:
                    metadata = _require_private_file(descriptor, maximum=512 * 1024 * 1024)
                    current = os.stat(name, dir_fd=artifact.stage_fd, follow_symlinks=False)
                    if not _same_file(metadata, current):
                        raise SftArtifactError("artifact_ownership_mismatch")
                    digest, raw = _digest_open_file(
                        descriptor, maximum=512 * 1024 * 1024, checkpoint=check
                    )
                    if not _same_file(metadata, os.fstat(descriptor)):
                        raise SftArtifactError("artifact_ownership_mismatch")
                    descriptors.append((name, descriptor, metadata))
                    descriptor = -1
                    files[name] = digest
                    if name == "manifest.json":
                        if raw is None or len(raw) > 256 * 1024:
                            raise SftArtifactError("artifact_ownership_mismatch")
                        manifest_raw = raw
                finally:
                    if descriptor >= 0:
                        os.close(descriptor)
            if manifest_raw is None:
                raise SftArtifactError("artifact_ownership_mismatch")
            manifest = _parse_manifest(manifest_raw)
            _require_exact_manifest(manifest, allowlist, files, expected)
            artifact.files = tuple(sorted(files.items()))
            return SftFinalArtifactVerification(
                artifact, allowlist, artifact.files, tuple(descriptors)
            )
        except Exception:
            for _name, descriptor, _metadata in descriptors:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            raise

    def verify_staged(
        self,
        artifact: SftStagedArtifact,
        *,
        allowlist: frozenset[str],
        expected: dict[str, object],
        checkpoint: _Checkpoint | None = None,
    ) -> dict[str, ArtifactDigest]:
        """Verify a child-written stage before it can be renamed into final storage."""

        if artifact.stage_fd is None or artifact.stage_parent_fd is None:
            raise SftArtifactError("artifact_ownership_mismatch")
        check = checkpoint or _noop
        check()
        _entry_matches(artifact.stage_parent_fd, str(artifact.attempt_id), artifact.stage_fd)
        verified = _verify_files(
            artifact.stage_fd, allowlist, {}, marker_allowed=True, checkpoint=check
        )
        manifest = _parse_manifest(
            _read_file_at(artifact.stage_fd, "manifest.json", maximum=256 * 1024, checkpoint=check)
        )
        _require_exact_manifest(manifest, allowlist, verified, expected)
        artifact.files = tuple(sorted(verified.items()))
        return verified

    def open_retained_final(
        self,
        scope: DepartmentScope,
        resource_id: UUID,
        *,
        category: str,
        attempt_id: UUID,
        allowlist: frozenset[str],
        expected: dict[str, object],
    ) -> SftFinalArtifactVerification:
        """Open an already-published final artifact without reopening at commit."""

        if category not in {"source", "dataset"}:
            raise SftArtifactError("artifact_ownership_mismatch")
        root = self._sources_fd if category == "source" else self._datasets_fd
        parent = _open_private_child(root, str(scope.value))
        directory: int | None = None
        try:
            directory = _open_private_child(parent, str(resource_id))
            artifact = SftStagedArtifact(
                category,
                scope,
                resource_id,
                attempt_id,
                (),
                None,
                directory,
                parent,
            )
            directory = parent = None
            return self.verify_retained_final(artifact, allowlist=allowlist, expected=expected)
        finally:
            if directory is not None:
                os.close(directory)
            if parent is not None:
                os.close(parent)

    def open_stage_scratch(
        self,
        artifact: SftStagedArtifact,
        name: str,
        *,
        expected: ArtifactDigest,
        checkpoint: _Checkpoint | None = None,
    ) -> int:
        """Retain an exact scratch descriptor while its parent is later renamed.

        The descriptor survives unlinking the scratch entry before final
        allowlist verification, enabling exact final authority revalidation
        without reopening a path or retaining source content in memory.
        """

        if artifact.stage_fd is None or artifact.stage_parent_fd is None:
            raise SftArtifactError("artifact_ownership_mismatch")
        check = checkpoint or _noop
        check()
        _entry_matches(artifact.stage_parent_fd, str(artifact.attempt_id), artifact.stage_fd)
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=artifact.stage_fd)
            metadata = _require_private_file(descriptor, maximum=512 * 1024 * 1024)
            digest, _ = _digest_open_file(descriptor, maximum=512 * 1024 * 1024, checkpoint=check)
            current = os.stat(name, dir_fd=artifact.stage_fd, follow_symlinks=False)
            if (
                digest != expected
                or not _same_file(metadata, os.fstat(descriptor))
                or not _same_file(metadata, current)
            ):
                raise SftArtifactError("artifact_ownership_mismatch")
            os.lseek(descriptor, 0, os.SEEK_SET)
            return descriptor
        except Exception:
            try:
                os.close(descriptor)
            except (OSError, UnboundLocalError):
                pass
            raise

    def remove_owned_source_stage(
        self,
        scope: DepartmentScope,
        source_bundle_id: UUID,
        import_attempt_id: UUID,
        *,
        checkpoint: _Checkpoint | None = None,
    ) -> bool:
        return self._remove_stage(
            self._staging_sources_fd,
            scope,
            source_bundle_id,
            import_attempt_id,
            checkpoint=checkpoint,
        )

    def open_source_stage(
        self, scope: DepartmentScope, source_bundle_id: UUID, import_attempt_id: UUID
    ) -> SftStagedArtifact:
        return self._open_stage(
            self._staging_sources_fd,
            self._sources_fd,
            "source",
            scope,
            source_bundle_id,
            import_attempt_id,
            SOURCE_FILES,
        )

    def remove_owned_dataset_stage(
        self,
        scope: DepartmentScope,
        build_id: UUID,
        publication_attempt_id: UUID,
        *,
        checkpoint: _Checkpoint | None = None,
    ) -> bool:
        return self._remove_stage(
            self._staging_datasets_fd,
            scope,
            build_id,
            publication_attempt_id,
            checkpoint=checkpoint,
        )

    def open_dataset_stage(
        self, scope: DepartmentScope, build_id: UUID, publication_attempt_id: UUID
    ) -> SftStagedArtifact:
        return self._open_stage(
            self._staging_datasets_fd,
            self._datasets_fd,
            "dataset",
            scope,
            build_id,
            publication_attempt_id,
            DATASET_FILES,
        )

    def remove_owned_source_final(
        self,
        scope: DepartmentScope,
        source_bundle_id: UUID,
        import_attempt_id: UUID,
        *,
        expected: dict[str, object],
        checkpoint: _Checkpoint | None = None,
    ) -> bool:
        return self._remove_final(
            self._sources_fd,
            scope,
            source_bundle_id,
            SOURCE_FILES,
            expected,
            checkpoint=checkpoint,
        )

    def remove_owned_dataset_final(
        self,
        scope: DepartmentScope,
        build_id: UUID,
        publication_attempt_id: UUID,
        *,
        expected: dict[str, object],
        checkpoint: _Checkpoint | None = None,
    ) -> bool:
        return self._remove_final(
            self._datasets_fd,
            scope,
            build_id,
            DATASET_FILES,
            expected,
            checkpoint=checkpoint,
        )

    def _stage(
        self,
        staging_root_fd: int,
        final_root_fd: int,
        category: str,
        scope: DepartmentScope,
        resource_id: UUID,
        attempt_id: UUID,
        values: tuple[tuple[str, bytes], ...],
        allowlist: frozenset[str],
    ) -> SftStagedArtifact:
        result = self._prepare_stage(
            staging_root_fd,
            final_root_fd,
            category,
            scope,
            resource_id,
            attempt_id,
        )
        try:
            if result.stage_fd is None:
                raise SftArtifactError("artifact_ownership_mismatch")
            files = tuple(
                (name, _write_file(result.stage_fd, name, value)) for name, value in values
            )
            _verify_files(result.stage_fd, allowlist, dict(files), marker_allowed=True)
            result.files = files
            return result
        except Exception:
            result.close()
            raise

    def _prepare_stage(
        self,
        staging_root_fd: int,
        final_root_fd: int,
        category: str,
        scope: DepartmentScope,
        resource_id: UUID,
        attempt_id: UUID,
    ) -> SftStagedArtifact:
        if not isinstance(scope, DepartmentScope) or resource_id.int == 0 or attempt_id.int == 0:
            raise SftArtifactError()
        descriptors: list[int] = []
        try:
            department = _ensure_private_child(staging_root_fd, str(scope.value))
            descriptors.append(department)
            resource = _ensure_private_child(department, str(resource_id))
            descriptors.append(resource)
            try:
                stage = _create_private_child(resource, str(attempt_id))
                _write_file(stage, STAGE_MARKER, _MARKER_BYTES)
            except FileExistsError:
                # A crash after mkdir or marker construction leaves an owned
                # directory.  Its UUID chain, not marker bytes, is the recovery
                # authority; a child will only receive a fresh empty stage.
                stage = _open_private_child(resource, str(attempt_id))
                if set(os.listdir(stage)) != {STAGE_MARKER}:
                    os.close(stage)
                    raise SftArtifactError("artifact_ownership_mismatch")
            descriptors.append(stage)
            final_department = _ensure_private_child(final_root_fd, str(scope.value))
            descriptors.append(final_department)
            result = SftStagedArtifact(
                category,
                scope,
                resource_id,
                attempt_id,
                (),
                resource,
                stage,
                final_department,
            )
            descriptors = [department]
            return result
        except OSError as error:
            raise SftArtifactError("dataset_publication_failed") from error
        finally:
            for descriptor in reversed(descriptors):
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def _open_final(self, root_fd: int, scope: DepartmentScope, resource_id: UUID) -> int:
        department = _open_private_child(root_fd, str(scope.value))
        try:
            return _open_private_child(department, str(resource_id))
        finally:
            os.close(department)

    def _open_stage(
        self,
        staging_root_fd: int,
        final_root_fd: int,
        category: str,
        scope: DepartmentScope,
        resource_id: UUID,
        attempt_id: UUID,
        allowlist: frozenset[str],
    ) -> SftStagedArtifact:
        department = _open_private_child(staging_root_fd, str(scope.value))
        try:
            resource = _open_private_child(department, str(resource_id))
            try:
                stage = _open_private_child(resource, str(attempt_id))
                try:
                    files = _verify_files(stage, allowlist, {}, marker_allowed=True)
                    final_department = _ensure_private_child(final_root_fd, str(scope.value))
                    result = SftStagedArtifact(
                        category,
                        scope,
                        resource_id,
                        attempt_id,
                        tuple(sorted(files.items())),
                        resource,
                        stage,
                        final_department,
                    )
                    resource = stage = final_department = None  # retained by result
                    return result
                finally:
                    if stage is not None:
                        os.close(stage)
            finally:
                if resource is not None:
                    os.close(resource)
        finally:
            os.close(department)

    def _verify_final(
        self,
        root_fd: int,
        scope: DepartmentScope,
        resource_id: UUID,
        allowlist: frozenset[str],
        expected: dict[str, object],
    ) -> dict[str, ArtifactDigest]:
        directory = self._open_final(root_fd, scope, resource_id)
        try:
            verified = _verify_files(directory, allowlist, {}, marker_allowed=False)
            manifest = _parse_manifest(
                _read_file_at(directory, "manifest.json", maximum=256 * 1024)
            )
            _require_exact_manifest(manifest, allowlist, verified, expected)
            return verified
        finally:
            os.close(directory)

    def _remove_stage(
        self,
        root_fd: int,
        scope: DepartmentScope,
        resource_id: UUID,
        attempt_id: UUID,
        *,
        checkpoint: _Checkpoint | None = None,
    ) -> bool:
        if resource_id.int == 0 or attempt_id.int == 0:
            raise SftArtifactError()
        try:
            department = _open_private_child(root_fd, str(scope.value))
            try:
                resource = _open_private_child(department, str(resource_id))
                try:
                    stage = _open_private_child(resource, str(attempt_id))
                    try:
                        _remove_contents(stage, checkpoint=checkpoint or _noop)
                        _rmdir_exact(resource, str(attempt_id), stage)
                        _fsync(resource)
                        return True
                    finally:
                        os.close(stage)
                finally:
                    os.close(resource)
            finally:
                os.close(department)
        except FileNotFoundError:
            return False
        except OSError as error:
            raise SftArtifactError("staging_path_unsafe") from error

    def _remove_final(
        self,
        root_fd: int,
        scope: DepartmentScope,
        resource_id: UUID,
        allowlist: frozenset[str],
        expected: dict[str, object],
        *,
        checkpoint: _Checkpoint | None = None,
    ) -> bool:
        try:
            department = _open_private_child(root_fd, str(scope.value))
            try:
                target = _open_private_child(department, str(resource_id))
                try:
                    check = checkpoint or _noop
                    check()
                    verified = _verify_files(
                        target, allowlist, {}, marker_allowed=False, checkpoint=check
                    )
                    manifest = _parse_manifest(
                        _read_file_at(target, "manifest.json", maximum=256 * 1024, checkpoint=check)
                    )
                    _require_exact_manifest(manifest, allowlist, verified, expected)
                    _remove_contents(target, checkpoint=check)
                    _rmdir_exact(department, str(resource_id), target)
                    _fsync(department)
                    return True
                finally:
                    os.close(target)
            finally:
                os.close(department)
        except FileNotFoundError:
            return False
        except OSError as error:
            raise SftArtifactError("artifact_ownership_mismatch") from error


def _open_directory_path(path: Path, *, writable: bool) -> int:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        _require_private_directory(descriptor, writable=writable)
        return descriptor
    except OSError as error:
        raise SftArtifactError("artifact_permissions_invalid") from error


def _require_private_directory(descriptor: int, *, writable: bool) -> os.stat_result:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
        or (writable and not metadata.st_mode & stat.S_IWUSR)
    ):
        raise SftArtifactError("artifact_permissions_invalid")
    return metadata


def _require_private_file(descriptor: int, *, maximum: int) -> os.stat_result:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
        or metadata.st_nlink != 1
        or not 1 <= metadata.st_size <= maximum
    ):
        raise SftArtifactError("artifact_ownership_mismatch")
    return metadata


def _ensure_private_child(parent_fd: int, name: str) -> int:
    _safe_name(name)
    try:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            _fsync(parent_fd)
        except FileExistsError:
            pass
        return _open_private_child(parent_fd, name)
    except OSError:
        raise


def _create_private_child(parent_fd: int, name: str) -> int:
    _safe_name(name)
    os.mkdir(name, 0o700, dir_fd=parent_fd)
    _fsync(parent_fd)
    return _open_private_child(parent_fd, name)


def _open_private_child(parent_fd: int, name: str) -> int:
    _safe_name(name)
    descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        _require_private_directory(descriptor, writable=True)
        _entry_matches(parent_fd, name, descriptor)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _safe_name(name: str) -> None:
    if not name or "/" in name or name in {".", ".."}:
        raise SftArtifactError("artifact_ownership_mismatch")


def _entry_matches(parent_fd: int, name: str, child_fd: int) -> None:
    current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    opened = os.fstat(child_fd)
    if (
        not stat.S_ISDIR(current.st_mode)
        or current.st_dev != opened.st_dev
        or current.st_ino != opened.st_ino
    ):
        raise SftArtifactError("artifact_ownership_mismatch")


def _rmdir_exact(parent_fd: int, name: str, child_fd: int) -> None:
    _entry_matches(parent_fd, name, child_fd)
    os.rmdir(name, dir_fd=parent_fd)


def _unlink_exact(directory_fd: int, name: str) -> None:
    metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise SftArtifactError("artifact_ownership_mismatch")
    os.unlink(name, dir_fd=directory_fd)


def _write_file(directory_fd: int, name: str, value: bytes) -> ArtifactDigest:
    if not isinstance(value, bytes) or not value:
        raise SftArtifactError()
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory_fd,
    )
    try:
        total = 0
        view = memoryview(value)
        while total < len(value):
            total += os.write(descriptor, view[total:])
        os.fsync(descriptor)
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        opened = os.fstat(descriptor)
        if not _same_file(opened, current):
            raise SftArtifactError("artifact_ownership_mismatch")
        _fsync(directory_fd)
        return ArtifactDigest(hashlib.sha256(value).hexdigest(), len(value))
    except OSError as error:
        raise SftArtifactError() from error
    finally:
        os.close(descriptor)


def _read_file_at(
    directory_fd: int,
    name: str,
    *,
    maximum: int,
    checkpoint: _Checkpoint | None = None,
) -> bytes:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or not 1 <= before.st_size <= maximum
        ):
            raise SftArtifactError("source_artifact_mismatch")
        output = bytearray()
        check = checkpoint or _noop
        while True:
            check()
            part = os.read(descriptor, min(64 * 1024, maximum + 1 - len(output)))
            if not part:
                break
            output.extend(part)
            if len(output) > maximum:
                raise SftArtifactError("source_artifact_mismatch")
        after = os.fstat(descriptor)
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if not _same_file(before, after) or not _same_file(before, current):
            raise SftArtifactError("source_artifact_mismatch")
        return bytes(output)
    except OSError as error:
        raise SftArtifactError("source_artifact_mismatch") from error
    finally:
        os.close(descriptor)


def _verify_files(
    directory_fd: int,
    allowlist: frozenset[str],
    expected: dict[str, ArtifactDigest],
    *,
    marker_allowed: bool,
    checkpoint: _Checkpoint | None = None,
) -> dict[str, ArtifactDigest]:
    check = checkpoint or _noop
    check()
    names = set(os.listdir(directory_fd))
    permitted = set(allowlist) | ({STAGE_MARKER} if marker_allowed else set())
    if names != permitted:
        raise SftArtifactError("artifact_ownership_mismatch")
    verified = {
        name: _digest_file(directory_fd, name, checkpoint=check) for name in sorted(allowlist)
    }
    if expected and verified != expected:
        raise SftArtifactError("artifact_ownership_mismatch")
    manifest = _parse_manifest(
        _read_file_at(directory_fd, "manifest.json", maximum=256 * 1024, checkpoint=check)
    )
    _require_manifest_files(manifest, allowlist, verified)
    return verified


def _digest_file(
    directory_fd: int, name: str, *, checkpoint: _Checkpoint | None = None
) -> ArtifactDigest:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    try:
        digest, _ = _digest_open_file(
            descriptor, maximum=512 * 1024 * 1024, checkpoint=checkpoint or _noop
        )
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if not _same_file(os.fstat(descriptor), current):
            raise SftArtifactError("artifact_ownership_mismatch")
        return digest
    finally:
        os.close(descriptor)


def _digest_open_file(
    descriptor: int, *, maximum: int, checkpoint: _Checkpoint | None = None
) -> tuple[ArtifactDigest, bytes | None]:
    """Hash one retained descriptor without holding a second pathname handle."""

    before = _require_private_file(descriptor, maximum=maximum)
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    total = 0
    retained = bytearray() if before.st_size <= 256 * 1024 else None
    check = checkpoint or _noop
    while True:
        check()
        part = os.read(descriptor, min(64 * 1024, maximum + 1 - total))
        if not part:
            break
        total += len(part)
        if total > maximum:
            raise SftArtifactError("artifact_ownership_mismatch")
        digest.update(part)
        if retained is not None:
            retained.extend(part)
    after = os.fstat(descriptor)
    if total != before.st_size or not _same_file(before, after):
        raise SftArtifactError("artifact_ownership_mismatch")
    return ArtifactDigest(digest.hexdigest(), total), (
        bytes(retained) if retained is not None else None
    )


def _require_manifest_files(
    manifest: dict[str, object], allowlist: frozenset[str], verified: dict[str, ArtifactDigest]
) -> None:
    declared = manifest.get("files")
    payloads = allowlist - {"manifest.json"}
    if not isinstance(declared, dict) or set(declared) != payloads:
        raise SftArtifactError("artifact_ownership_mismatch")
    for name in payloads:
        if declared[name] != {
            "sha256": verified[name].sha256,
            "byte_size": verified[name].byte_size,
        }:
            raise SftArtifactError("artifact_ownership_mismatch")


def _require_exact_manifest(
    manifest: dict[str, object],
    allowlist: frozenset[str],
    verified: dict[str, ArtifactDigest],
    expected: dict[str, object],
) -> None:
    required_source = {
        "artifact_contract_version",
        "department_id",
        "source_bundle_id",
        "import_attempt_id",
        "stage_id",
        "normalization_version",
        "example_contract_version",
        "example_count",
        "group_count",
        "source_reference_count",
        "files",
    }
    required_dataset = {
        "artifact_contract_version",
        "department_id",
        "source_bundle_id",
        "build_id",
        "publication_attempt_id",
        "attempt_number",
        "code_revision",
        "normalization_version",
        "example_contract_version",
        "split_version",
        "validation_ratio",
        "source_example_count",
        "source_group_count",
        "source_reference_count",
        "train_example_count",
        "validation_example_count",
        "files",
    }
    required = required_source if allowlist == SOURCE_FILES else required_dataset
    if set(manifest) != required:
        raise SftArtifactError("artifact_ownership_mismatch")
    _require_manifest_files(manifest, allowlist, verified)
    if set(expected) != required:
        raise SftArtifactError("artifact_ownership_mismatch")
    if any(manifest.get(key) != value for key, value in expected.items()):
        raise SftArtifactError("artifact_ownership_mismatch")


def _parse_manifest(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise SftArtifactError("artifact_ownership_mismatch") from error
    if not isinstance(value, dict):
        raise SftArtifactError("artifact_ownership_mismatch")
    return value


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError()
    return result


def _remove_contents(directory_fd: int, *, checkpoint: _Checkpoint) -> None:
    for name in os.listdir(directory_fd):
        checkpoint()
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
            try:
                _require_private_directory(child, writable=True)
                _entry_matches(directory_fd, name, child)
                _remove_contents(child, checkpoint=checkpoint)
                _rmdir_exact(directory_fd, name, child)
            finally:
                os.close(child)
        elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
            _unlink_exact(directory_fd, name)
        else:
            raise SftArtifactError("staging_path_unsafe")
    _fsync(directory_fd)


def _same_file(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        stat.S_ISREG(second.st_mode)
        and first.st_dev == second.st_dev
        and first.st_ino == second.st_ino
        and first.st_uid == second.st_uid
        and stat.S_IMODE(first.st_mode) == stat.S_IMODE(second.st_mode)
        and first.st_size == second.st_size
        and first.st_nlink == second.st_nlink == 1
        and first.st_mtime_ns == second.st_mtime_ns
        and first.st_ctime_ns == second.st_ctime_ns
    )


def _fsync(descriptor: int) -> None:
    try:
        os.fsync(descriptor)
    except OSError as error:
        raise SftArtifactError("dataset_publication_failed") from error


def _noop() -> None:
    return None
