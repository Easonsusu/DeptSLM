"""Safe process decision sink; mutation-success audit rows are database-backed."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Protocol


class AuditResult(StrEnum):
    ALLOWED = "allowed"
    DENIED = "denied"


@dataclass(frozen=True, slots=True)
class AuditEvent:
    actor_subject: str | None
    action: str
    result: AuditResult
    reason_code: str
    department_id: str | None = None
    correlation_id: str | None = None
    resource_id: str | None = None


class AuditSink(Protocol):
    def emit(self, event: AuditEvent) -> None: ...


@dataclass(frozen=True, slots=True)
class LoggingAuditSink:
    """Write only the typed safe fields defined by AuditEvent."""

    logger: logging.Logger = logging.getLogger("deptslm.audit")

    def emit(self, event: AuditEvent) -> None:
        self.logger.info("auth_audit %s", json.dumps(asdict(event), sort_keys=True))
