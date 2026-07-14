"""Shared isolated API test environment."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def isolated_database_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide a lazy, unreachable PostgreSQL URL for non-database tests."""

    test_url = os.getenv("DATABASE_TEST_URL")
    monkeypatch.setenv(
        "DATABASE_URL",
        test_url or "postgresql+psycopg://deptslm:deptslm@127.0.0.1:1/deptslm_test",
    )
