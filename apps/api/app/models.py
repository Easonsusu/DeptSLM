"""Phase 3 PostgreSQL persistence models."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.auth import DepartmentRole, MembershipStatus

USER_STATUSES = ("active", "suspended", "revoked")
DEPARTMENT_STATUSES = ("active", "archived")
MEMBERSHIP_STATUSES = tuple(item.value for item in MembershipStatus)
DEPARTMENT_ROLES = tuple(item.value for item in DepartmentRole)
AUDIT_RESULTS = ("allowed", "denied")


class Base(DeclarativeBase):
    pass


def utc_timestamp() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class UserIdentity(Base):
    __tablename__ = "user_identities"
    __table_args__ = (
        UniqueConstraint("issuer", "subject", name="uq_user_identity_issuer_subject"),
        CheckConstraint("issuer ~ '[^[:space:]]'", name="ck_user_identity_issuer_nonempty"),
        CheckConstraint("subject ~ '[^[:space:]]'", name="ck_user_identity_subject_nonempty"),
        CheckConstraint(
            "status IN ('active','suspended','revoked')",
            name="ck_user_identity_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    issuer: Mapped[str] = mapped_column(String(512), nullable=False)
    subject: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = utc_timestamp()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Department(Base):
    __tablename__ = "departments"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_department_slug"),
        CheckConstraint(
            "slug ~ '^[a-z0-9]+(?:-[a-z0-9]+)*$'",
            name="ck_department_slug_format",
        ),
        CheckConstraint("length(slug) BETWEEN 2 AND 63", name="ck_department_slug_length"),
        CheckConstraint(
            "length(btrim(display_name)) BETWEEN 1 AND 200",
            name="ck_department_display_name_length",
        ),
        CheckConstraint("status IN ('active','archived')", name="ck_department_status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    slug: Mapped[str] = mapped_column(String(63), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = utc_timestamp()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("user_id", "department_id", name="uq_membership_user_department"),
        CheckConstraint(
            "role IN ('system_admin','department_admin','instructor','student','viewer')",
            name="ck_membership_role",
        ),
        CheckConstraint(
            "status IN ('active','suspended','revoked')",
            name="ck_membership_status",
        ),
        Index("ix_membership_department_status", "department_id", "status"),
        Index("ix_membership_user_status", "user_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_identities.id", ondelete="RESTRICT"), nullable=False
    )
    department_id: Mapped[UUID] = mapped_column(
        ForeignKey("departments.id", ondelete="RESTRICT"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("user_identities.id", ondelete="RESTRICT")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = utc_timestamp()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class PersistentAuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint("length(action) > 0", name="ck_audit_action_nonempty"),
        CheckConstraint("length(resource_type) > 0", name="ck_audit_resource_type_nonempty"),
        CheckConstraint("result IN ('allowed','denied')", name="ck_audit_result"),
        CheckConstraint("length(reason_code) > 0", name="ck_audit_reason_nonempty"),
        Index("ix_audit_department_created", "department_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    actor_subject: Mapped[str | None] = mapped_column(String(512))
    actor_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("user_identities.id", ondelete="RESTRICT")
    )
    department_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("departments.id", ondelete="RESTRICT")
    )
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(100))
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(100), nullable=False)
    correlation_id: Mapped[UUID | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = utc_timestamp()
