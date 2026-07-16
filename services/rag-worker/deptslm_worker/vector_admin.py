"""Explicit, idempotent Qdrant schema bootstrap command."""

from __future__ import annotations

import argparse
import logging

from deptslm_worker.index_settings import IndexConfigurationError, QdrantSettings
from deptslm_worker.qdrant_adapter import DepartmentQdrant, QdrantBoundaryError


def main() -> int:
    parser = argparse.ArgumentParser(description="DeptSLM Qdrant administration")
    parser.add_argument("command", choices=("bootstrap",))
    parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        settings = QdrantSettings.from_environment()
        qdrant = DepartmentQdrant(
            settings.qdrant_url,
            settings.qdrant_api_key,
            settings.qdrant_timeout_seconds,
        )
        try:
            qdrant.bootstrap_collection()
        finally:
            qdrant.close()
    except (IndexConfigurationError, QdrantBoundaryError) as error:
        logging.error("vector bootstrap failed: %s", error)
        return 2
    logging.info("verified Qdrant collection schema")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
