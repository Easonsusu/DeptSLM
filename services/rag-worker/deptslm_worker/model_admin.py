"""Explicit administrative preparation for the pinned embedding model."""

from __future__ import annotations

import argparse
import json
import os
import shutil
from uuid import uuid4

from app.vector_index_domain import EMBEDDING_MODEL_ID, EMBEDDING_MODEL_REVISION
from app.rag_domain import GENERATION_MODEL_ID, GENERATION_MODEL_REVISION
from deptslm_worker.index_settings import _data_root
from deptslm_worker.model_store import (
    MANIFEST_NAME,
    ModelStoreError,
    build_manifest,
    build_generation_manifest,
    generation_model_directory,
    model_directory,
    validate_model_store,
    validate_generation_model_store,
)


def prepare_embedding() -> None:
    data_dir = _data_root(
        os.getenv("DEPTSLM_DATA_DIR", ""), required_directories=("model_cache",)
    )
    _prepare_model(
        data_dir,
        model_id=EMBEDDING_MODEL_ID,
        revision=EMBEDDING_MODEL_REVISION,
        destination=model_directory(data_dir),
        manifest_builder=build_manifest,
        validator=validate_model_store,
    )


def prepare_generation() -> None:
    data_dir = _data_root(
        os.getenv("DEPTSLM_DATA_DIR", ""), required_directories=("model_cache",)
    )
    _prepare_model(
        data_dir,
        model_id=GENERATION_MODEL_ID,
        revision=GENERATION_MODEL_REVISION,
        destination=generation_model_directory(data_dir),
        manifest_builder=build_generation_manifest,
        validator=validate_generation_model_store,
    )


def prepare_rag_models() -> None:
    prepare_embedding()
    prepare_generation()


def _prepare_model(
    data_dir,
    *,
    model_id,
    revision,
    destination,
    manifest_builder,
    validator,
) -> None:
    if destination.exists():
        validator(data_dir)
        return
    model_cache = data_dir / "model_cache"
    staging = model_cache / f".prepare-{uuid4()}"
    cache = staging / ".cache"
    staging.mkdir(mode=0o700)
    token = os.getenv("HF_TOKEN") or None
    try:
        try:
            from huggingface_hub import HfApi, snapshot_download
        except ImportError as error:
            raise ModelStoreError("embedding_model_unavailable") from error
        info = HfApi().model_info(
            model_id,
            revision=revision,
            token=token,
        )
        if info.sha != revision:
            raise ModelStoreError("embedding_model_unavailable")
        snapshot_download(
            repo_id=model_id,
            revision=revision,
            local_dir=staging,
            cache_dir=cache,
            token=token,
            local_dir_use_symlinks=False,
        )
        shutil.rmtree(cache, ignore_errors=True)
        metadata_dir = staging / ".cache"
        shutil.rmtree(metadata_dir, ignore_errors=True)
        manifest = manifest_builder(staging)
        manifest_path = staging / MANIFEST_NAME
        descriptor = os.open(
            manifest_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            payload = (
                json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode()
            written = 0
            while written < len(payload):
                count = os.write(descriptor, payload[written:])
                if count <= 0:
                    raise ModelStoreError("embedding_model_unavailable")
                written += count
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.rename(staging, destination)
        validator(data_dir)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="DeptSLM model administration")
    parser.add_argument(
        "command",
        choices=("prepare-embedding", "prepare-generation", "prepare-rag-models"),
    )
    args = parser.parse_args()
    try:
        if args.command == "prepare-embedding":
            prepare_embedding()
        elif args.command == "prepare-generation":
            prepare_generation()
        else:
            prepare_rag_models()
        print("Pinned model assets are prepared and verified.")
        return 0
    except Exception:
        print("Model preparation failed.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
