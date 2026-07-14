"""Phase 3 tests that do not require a running database."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.admin import main
from app.settings import ConfigurationError, Settings


def test_database_url_is_required(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DEPTSLM_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL")
    with pytest.raises(ConfigurationError, match="DATABASE_URL"):
        Settings.from_environment()


def test_database_url_requires_psycopg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DEPTSLM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "sqlite:///unsafe.db")
    with pytest.raises(ConfigurationError, match="postgresql\\+psycopg"):
        Settings.from_environment()


@pytest.mark.parametrize(
    "environment", [None, "", "production", "staging", "preview", "qa", "unknown", "prodution"]
)
def test_bootstrap_refuses_unsafe_environment(
    monkeypatch: pytest.MonkeyPatch, environment: str | None, capsys: pytest.CaptureFixture[str]
) -> None:
    if environment is None:
        monkeypatch.delenv("ENVIRONMENT", raising=False)
    else:
        monkeypatch.setenv("ENVIRONMENT", environment)
    result = main(
        [
            "bootstrap-department",
            "--slug",
            "science",
            "--display-name",
            "Science",
            "--admin-issuer",
            "https://issuer.invalid",
            "--admin-subject",
            "opaque-admin",
        ]
    )
    captured = capsys.readouterr()
    assert result == 2
    assert "DATABASE_URL" not in captured.out + captured.err
