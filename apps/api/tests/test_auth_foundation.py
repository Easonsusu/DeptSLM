"""Security tests for Phase 2 authentication and authorization foundations."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.audit import AuditEvent, AuditSink, LoggingAuditSink
from app.auth import (
    AuthenticatedPrincipal,
    DepartmentRole,
    HS256TokenVerifier,
    MembershipStatus,
)
from app.authorization import (
    DepartmentAuthorizationContext,
    DepartmentScope,
    MembershipResolver,
    MembershipResult,
    require_department_roles,
)
from app.main import app
from app.settings import ConfigurationError, Settings
from app.storage_paths import department_storage_path
from app.vector_scope import DepartmentVectorScope

SECRET = "unit-test-secret-not-for-runtime-0123456789-abcdefghijklmnopqrstuvwxyz"
ISSUER = "https://issuer.test.invalid"
AUDIENCE = "deptslm-tests"
SUBJECT = "test-subject"


@dataclass
class CollectingAuditSink(AuditSink):
    events: list[AuditEvent] = field(default_factory=list)

    def emit(self, event: AuditEvent) -> None:
        self.events.append(event)


@dataclass(frozen=True)
class FakeMembershipResolver(MembershipResolver):
    memberships: tuple[MembershipResult, ...] = ()

    def resolve(
        self, principal: AuthenticatedPrincipal, department: DepartmentScope
    ) -> MembershipResult | None:
        return next(
            (
                membership
                for membership in self.memberships
                if membership.subject == principal.subject and membership.department == department
            ),
            None,
        )


def make_token(**overrides: object) -> str:
    now = datetime.now(UTC)
    payload: dict[str, object] = {
        "sub": SUBJECT,
        "iss": ISSUER,
        "aud": AUDIENCE,
        "exp": now + timedelta(minutes=5),
        "nbf": now - timedelta(seconds=1),
    }
    payload.update(overrides)
    return jwt.encode(payload, SECRET, algorithm="HS256")


def auth_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DEPTSLM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("DEPTSLM_AUTH_MODE", "hs256")
    monkeypatch.setenv("DEPTSLM_AUTH_ISSUER", ISSUER)
    monkeypatch.setenv("DEPTSLM_AUTH_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("DEPTSLM_AUTH_SECRET", SECRET)


def test_valid_token_returns_safe_identity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    auth_environment(monkeypatch, tmp_path)
    with TestClient(app) as client:
        response = client.get("/auth/me", headers={"Authorization": f"Bearer {make_token()}"})

    assert response.status_code == 200
    assert response.json() == {"subject": SUBJECT, "issuer": ISSUER}
    assert SECRET not in response.text


@pytest.mark.parametrize(
    ("authorization", "expected_status"),
    ((None, 401), ("Bearer", 401), ("Basic abc", 401), ("Bearer a b", 401)),
)
def test_missing_or_malformed_bearer_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    authorization: str | None,
    expected_status: int,
) -> None:
    auth_environment(monkeypatch, tmp_path)
    headers = {} if authorization is None else {"Authorization": authorization}
    with TestClient(app) as client:
        response = client.get("/auth/me", headers=headers)
    assert response.status_code == expected_status
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "token",
    (
        jwt.encode(
            {
                "sub": SUBJECT,
                "iss": ISSUER,
                "aud": AUDIENCE,
                "exp": datetime.now(UTC) + timedelta(minutes=5),
            },
            "wrong-unit-test-secret-0123456789-abcdefghijklmnopqrstuvwxyz-000000",
            algorithm="HS256",
        ),
        make_token(exp=datetime.now(UTC) - timedelta(minutes=1)),
        make_token(iss="https://wrong.invalid"),
        make_token(aud="wrong-audience"),
        make_token(sub=None),
        jwt.encode(
            {
                "sub": SUBJECT,
                "iss": ISSUER,
                "aud": AUDIENCE,
                "exp": datetime.now(UTC) + timedelta(minutes=5),
            },
            SECRET,
            algorithm="HS384",
        ),
        jwt.encode(
            {
                "sub": SUBJECT,
                "iss": ISSUER,
                "aud": AUDIENCE,
                "exp": datetime.now(UTC) + timedelta(minutes=5),
            },
            key="",
            algorithm="none",
        ),
    ),
)
def test_invalid_tokens_are_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, token: str
) -> None:
    auth_environment(monkeypatch, tmp_path)
    with TestClient(app) as client:
        response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_unconfigured_authentication_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DEPTSLM_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.setenv("DEPTSLM_AUTH_MODE", "disabled")
    monkeypatch.delenv("DEPTSLM_AUTH_ISSUER", raising=False)
    monkeypatch.delenv("DEPTSLM_AUTH_AUDIENCE", raising=False)
    monkeypatch.delenv("DEPTSLM_AUTH_SECRET", raising=False)
    with TestClient(app) as client:
        response = client.get("/auth/me", headers={"Authorization": "Bearer token"})
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"
        assert client.get("/health").status_code == 200
        assert client.get("/version").status_code == 200


@pytest.mark.parametrize("environment", ("local", "development", "dev", "test"))
def test_hs256_mode_accepts_only_reviewed_local_environments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, environment: str
) -> None:
    auth_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("ENVIRONMENT", environment)
    settings = Settings.from_environment()
    assert settings.environment == environment


@pytest.mark.parametrize(
    "environment",
    (
        None,
        "",
        "production",
        "prod",
        "staging",
        "preview",
        "qa",
        "unknown",
        "prodution",
        "TEST",
    ),
)
def test_hs256_mode_rejects_unreviewed_or_missing_environments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, environment: str | None
) -> None:
    auth_environment(monkeypatch, tmp_path)
    if environment is None:
        monkeypatch.delenv("ENVIRONMENT")
    else:
        monkeypatch.setenv("ENVIRONMENT", environment)
    with pytest.raises(ConfigurationError, match="ENVIRONMENT"):
        Settings.from_environment()


@pytest.mark.parametrize(
    "missing_variable",
    ("DEPTSLM_AUTH_ISSUER", "DEPTSLM_AUTH_AUDIENCE", "DEPTSLM_AUTH_SECRET"),
)
def test_hs256_mode_rejects_missing_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, missing_variable: str
) -> None:
    auth_environment(monkeypatch, tmp_path)
    monkeypatch.delenv(missing_variable)
    with pytest.raises(ConfigurationError, match=missing_variable):
        Settings.from_environment()


@pytest.mark.parametrize(
    "secret",
    ("", "short-secret", "replace-with-a-local-development-secret"),
)
def test_hs256_mode_rejects_empty_short_or_placeholder_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, secret: str
) -> None:
    auth_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("DEPTSLM_AUTH_SECRET", secret)
    with pytest.raises(ConfigurationError, match="DEPTSLM_AUTH_SECRET"):
        Settings.from_environment()


def test_hs256_secret_length_is_measured_in_utf8_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    auth_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("DEPTSLM_AUTH_SECRET", "密" * 11)
    assert Settings.from_environment().auth_secret == "密" * 11


def test_future_not_before_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    auth_environment(monkeypatch, tmp_path)
    token = make_token(nbf=datetime.now(UTC) + timedelta(minutes=1))
    with TestClient(app) as client:
        response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def make_authorization_client(
    resolver: MembershipResolver,
    *,
    roles: tuple[DepartmentRole, ...] = tuple(DepartmentRole),
    audit_sink: AuditSink | None = None,
) -> tuple[TestClient, AuditSink]:
    test_app = FastAPI()
    sink = audit_sink or CollectingAuditSink()
    test_app.state.token_verifier = HS256TokenVerifier(SECRET, ISSUER, AUDIENCE)
    test_app.state.membership_resolver = resolver
    test_app.state.audit_sink = sink

    @test_app.get("/test-scope")
    def scoped_route(
        context: DepartmentAuthorizationContext = Depends(require_department_roles(*roles)),
    ) -> dict[str, str]:
        return {
            "subject": context.subject,
            "department_id": str(context.department),
            "role": context.role,
        }

    return TestClient(test_app), sink


def membership(
    department: DepartmentScope,
    *,
    role: DepartmentRole = DepartmentRole.STUDENT,
    membership_status: MembershipStatus = MembershipStatus.ACTIVE,
) -> MembershipResult:
    return MembershipResult("membership-test", SUBJECT, department, role, membership_status)


def request_headers(
    department: DepartmentScope | str | None, *, token: str | None = None
) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {token or make_token()}",
        "X-Request-ID": "74d24ea0-0fd4-4802-9fc7-c4d1b2909015",
    }
    if department is not None:
        headers["X-Department-ID"] = str(department)
    return headers


@pytest.mark.parametrize("role", tuple(DepartmentRole))
def test_active_membership_allows_every_explicit_role(role: DepartmentRole) -> None:
    department = DepartmentScope(uuid4())
    client, _ = make_authorization_client(
        FakeMembershipResolver((membership(department, role=role),)), roles=(role,)
    )
    response = client.get("/test-scope", headers=request_headers(department))
    assert response.status_code == 200
    assert response.json()["role"] == role


@pytest.mark.parametrize(
    "resolver",
    (
        FakeMembershipResolver(),
        FakeMembershipResolver(
            (membership(DepartmentScope(uuid4()), membership_status=MembershipStatus.SUSPENDED),)
        ),
        FakeMembershipResolver(
            (membership(DepartmentScope(uuid4()), membership_status=MembershipStatus.REVOKED),)
        ),
    ),
)
def test_missing_or_inactive_membership_is_denied(resolver: MembershipResolver) -> None:
    client, _ = make_authorization_client(resolver)
    assert (
        client.get("/test-scope", headers=request_headers(DepartmentScope(uuid4()))).status_code
        == 403
    )


def test_suspended_and_revoked_matching_memberships_are_denied() -> None:
    for membership_status in (MembershipStatus.SUSPENDED, MembershipStatus.REVOKED):
        department = DepartmentScope(uuid4())
        resolver = FakeMembershipResolver(
            (membership(department, membership_status=membership_status),)
        )
        client, _ = make_authorization_client(resolver)
        assert client.get("/test-scope", headers=request_headers(department)).status_code == 403


def test_wrong_role_is_denied() -> None:
    department = DepartmentScope(uuid4())
    client, _ = make_authorization_client(
        FakeMembershipResolver((membership(department, role=DepartmentRole.VIEWER),)),
        roles=(DepartmentRole.DEPARTMENT_ADMIN,),
    )
    response = client.get("/test-scope", headers=request_headers(department))
    assert response.status_code == 403
    assert "WWW-Authenticate" not in response.headers


def test_cross_department_direct_object_reference_is_denied() -> None:
    allowed_department = DepartmentScope(uuid4())
    requested_department = DepartmentScope(uuid4())
    resolver = FakeMembershipResolver((membership(allowed_department),))
    client, _ = make_authorization_client(resolver)
    assert (
        client.get("/test-scope", headers=request_headers(requested_department)).status_code == 403
    )


@pytest.mark.parametrize("department", (None, "not-a-uuid"))
def test_missing_or_malformed_department_scope_is_denied(department: str | None) -> None:
    client, _ = make_authorization_client(FakeMembershipResolver())
    assert client.get("/test-scope", headers=request_headers(department)).status_code == 403


def test_system_admin_has_no_global_bypass() -> None:
    other_department = DepartmentScope(uuid4())
    resolver = FakeMembershipResolver(
        (membership(other_department, role=DepartmentRole.SYSTEM_ADMIN),)
    )
    client, _ = make_authorization_client(resolver, roles=(DepartmentRole.SYSTEM_ADMIN,))
    assert (
        client.get("/test-scope", headers=request_headers(DepartmentScope(uuid4()))).status_code
        == 403
    )


def test_context_and_scope_are_immutable() -> None:
    department = DepartmentScope(uuid4())
    context = DepartmentAuthorizationContext(
        SUBJECT, department, DepartmentRole.STUDENT, "membership-test"
    )
    with pytest.raises(FrozenInstanceError):
        context.subject = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        department.value = uuid4()  # type: ignore[misc]


def test_audit_events_cover_allow_and_deny_without_sensitive_data(
    caplog: pytest.LogCaptureFixture,
) -> None:
    department = DepartmentScope(uuid4())
    client, sink = make_authorization_client(FakeMembershipResolver((membership(department),)))
    exact_token = make_token()
    client.get("/test-scope", headers=request_headers(department, token=exact_token))
    client.get("/test-scope", headers=request_headers(DepartmentScope(uuid4())))

    serialized = repr(sink.events)
    assert any(event.result == "allowed" for event in sink.events)
    assert any(event.result == "denied" for event in sink.events)
    assert SECRET not in serialized
    assert exact_token not in serialized
    assert "document content" not in serialized
    assert "source content" not in serialized
    assert "raw request body" not in serialized
    assert "training content" not in serialized

    caplog.set_level("INFO", logger="deptslm.audit")
    logging_client, _ = make_authorization_client(
        FakeMembershipResolver((membership(department),)),
        audit_sink=LoggingAuditSink(),
    )
    logging_client.get("/test-scope", headers=request_headers(department, token=exact_token))
    log_output = caplog.text
    assert exact_token not in log_output
    assert SECRET not in log_output
    assert "source content" not in log_output
    assert "document content" not in log_output
    assert "raw request body" not in log_output
    assert "training content" not in log_output


def test_department_storage_path_stays_under_external_root(tmp_path: Path) -> None:
    department = DepartmentScope(uuid4())
    result = department_storage_path(tmp_path, department, "uploads", "file.txt")
    assert result.is_relative_to(tmp_path.resolve())
    assert str(department) in result.parts


@pytest.mark.parametrize("child", ("../escape", "/absolute", "uploads/../../escape"))
def test_department_storage_path_rejects_unsafe_children(tmp_path: Path, child: str) -> None:
    with pytest.raises(ValueError):
        department_storage_path(tmp_path, DepartmentScope(uuid4()), child)


def test_department_storage_path_rejects_symlink_escape(tmp_path: Path) -> None:
    department = DepartmentScope(uuid4())
    department_root = tmp_path / "departments" / str(department)
    department_root.mkdir(parents=True)
    outside = tmp_path.parent / f"outside-{uuid4()}"
    outside.mkdir()
    (department_root / "link").symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(ValueError):
            department_storage_path(tmp_path, department, "link", "file.txt")
    finally:
        outside.rmdir()


def test_malformed_department_identifier_is_rejected() -> None:
    with pytest.raises(ValueError):
        DepartmentScope.parse("not-a-uuid")
    with pytest.raises(ValueError):
        DepartmentScope(UUID(int=0))


def test_vector_scope_always_contains_department_filter() -> None:
    department = DepartmentScope(uuid4())
    assert DepartmentVectorScope(department).payload_filter() == {
        "must": [{"key": "department_id", "match": {"value": str(department)}}]
    }
