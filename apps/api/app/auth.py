"""Authentication models and development JWT verification."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import jwt

from app.settings import ConfigurationError, Settings


class AuthenticationError(Exception):
    """Raised when a request cannot produce a validated principal."""


class DepartmentRole(StrEnum):
    SYSTEM_ADMIN = "system_admin"
    DEPARTMENT_ADMIN = "department_admin"
    INSTRUCTOR = "instructor"
    STUDENT = "student"
    VIEWER = "viewer"


class MembershipStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    REVOKED = "revoked"


@dataclass(frozen=True, slots=True)
class AuthenticatedPrincipal:
    """Minimal identity established from a server-validated token."""

    subject: str
    issuer: str


class TokenVerifier(Protocol):
    """Boundary for converting a bearer token into a validated identity."""

    def verify(self, token: str) -> AuthenticatedPrincipal: ...


@dataclass(frozen=True, slots=True)
class DenyAllTokenVerifier:
    """Fail-closed verifier used when authentication is not configured."""

    def verify(self, token: str) -> AuthenticatedPrincipal:
        del token
        raise AuthenticationError("authentication_unconfigured")


@dataclass(frozen=True, slots=True)
class HS256TokenVerifier:
    """Development-only HS256 verifier with fixed algorithm policy."""

    secret: str
    issuer: str
    audience: str

    def verify(self, token: str) -> AuthenticatedPrincipal:
        try:
            payload = jwt.decode(
                token,
                self.secret,
                algorithms=["HS256"],
                issuer=self.issuer,
                audience=self.audience,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as error:
            raise AuthenticationError("invalid_token") from error

        subject = payload.get("sub")
        if not isinstance(subject, str) or not subject.strip():
            raise AuthenticationError("invalid_subject")
        return AuthenticatedPrincipal(subject=subject.strip(), issuer=self.issuer)


def build_token_verifier(settings: Settings) -> TokenVerifier:
    """Build a verifier, failing closed for incomplete configuration."""

    if settings.auth_mode != "hs256":
        return DenyAllTokenVerifier()
    if not settings.auth_secret or not settings.auth_issuer or not settings.auth_audience:
        raise ConfigurationError("HS256 authentication configuration is incomplete.")
    return HS256TokenVerifier(
        secret=settings.auth_secret,
        issuer=settings.auth_issuer,
        audience=settings.auth_audience,
    )
