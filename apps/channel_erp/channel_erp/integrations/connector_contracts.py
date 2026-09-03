"""Platform-neutral contracts for connector outbound operations.

The contracts contain no transport implementation.  Adapters opt in to
capabilities explicitly; unsupported writes fail closed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional


class ConnectorOperation(str, Enum):
    CREATE = "Create"
    UPDATE = "Update"
    CANCEL = "Cancel"
    AUDIT = "Audit"
    QUERY = "Query"


class OutboundStatus(str, Enum):
    PREFLIGHT = "Preflight"
    PENDING = "Pending"
    PROCESSING = "Processing"
    SUCCEEDED = "Succeeded"
    RETRYING = "Retrying"
    FAILED = "Failed"
    BLOCKED = "Blocked"
    CONFLICT = "Conflict"
    UNCERTAIN = "Uncertain"


ALLOWED_TRANSITIONS = {
    OutboundStatus.PREFLIGHT: {
        OutboundStatus.PENDING,
        OutboundStatus.BLOCKED,
        OutboundStatus.CONFLICT,
        OutboundStatus.FAILED,
    },
    OutboundStatus.PENDING: {
        OutboundStatus.PREFLIGHT,
        OutboundStatus.PROCESSING,
        OutboundStatus.BLOCKED,
        OutboundStatus.CONFLICT,
        OutboundStatus.FAILED,
    },
    OutboundStatus.RETRYING: {
        OutboundStatus.PREFLIGHT,
        OutboundStatus.PENDING,
        OutboundStatus.PROCESSING,
        OutboundStatus.BLOCKED,
        OutboundStatus.CONFLICT,
        OutboundStatus.FAILED,
    },
    OutboundStatus.PROCESSING: {
        OutboundStatus.SUCCEEDED,
        OutboundStatus.RETRYING,
        OutboundStatus.FAILED,
        OutboundStatus.BLOCKED,
        OutboundStatus.CONFLICT,
        OutboundStatus.UNCERTAIN,
    },
    OutboundStatus.UNCERTAIN: {
        OutboundStatus.SUCCEEDED,
        OutboundStatus.PENDING,
        OutboundStatus.BLOCKED,
        OutboundStatus.FAILED,
        OutboundStatus.UNCERTAIN,
    },
}


class ConnectorOperationError(Exception):
    """Base exception carrying safe transport evidence."""

    def __init__(self, message, *, context_id=None, response=None):
        super().__init__(message)
        self.context_id = context_id
        self.response = response


class OperationBlockedError(ConnectorOperationError):
    pass


class UncertainOperationError(ConnectorOperationError):
    """The request may have reached the remote system.

    Create operations raising this exception must never be retried until a
    probe proves that the remote entity does not exist.
    """


@dataclass(frozen=True)
class PreflightResult:
    allowed: bool
    reason: str = ""
    normalized_payload: Optional[Mapping[str, Any]] = None
    context: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OperationResult:
    outcome: str
    external_id: Optional[str] = None
    context_id: Optional[str] = None
    response: Optional[Mapping[str, Any]] = None

    @classmethod
    def succeeded(cls, **kwargs):
        return cls("Succeeded", **kwargs)

    @classmethod
    def uncertain(cls, **kwargs):
        return cls("Uncertain", **kwargs)


@dataclass(frozen=True)
class ProbeResult:
    outcome: str
    external_id: Optional[str] = None
    context_id: Optional[str] = None
    response: Optional[Mapping[str, Any]] = None

    @classmethod
    def found(cls, **kwargs):
        return cls("Found", **kwargs)

    @classmethod
    def not_found(cls, **kwargs):
        return cls("Not Found", **kwargs)

    @classmethod
    def unknown(cls, **kwargs):
        return cls("Unknown", **kwargs)


def normalize_operation(value) -> ConnectorOperation:
    try:
        return ConnectorOperation(str(value))
    except ValueError as exc:
        raise OperationBlockedError(f"不支持的连接器操作：{value}") from exc


def ensure_transition(current, target):
    current = OutboundStatus(str(current))
    target = OutboundStatus(str(target))
    if current == target:
        return target.value
    if target not in ALLOWED_TRANSITIONS.get(current, set()):
        raise OperationBlockedError(f"非法出站状态转换：{current.value} -> {target.value}")
    return target.value


def normalize_capabilities(values):
    return {normalize_operation(value).value for value in (values or ())}


def normalize_operation_result(value):
    if isinstance(value, OperationResult):
        return value
    if not isinstance(value, Mapping):
        return OperationResult.succeeded(response={"value": value})
    outcome = str(value.get("outcome") or value.get("status") or "Succeeded")
    if outcome.lower() in {"uncertain", "unknown", "timeout_unknown"}:
        outcome = "Uncertain"
    elif outcome.lower() in {"success", "succeeded", "ok", "found"}:
        outcome = "Succeeded"
    return OperationResult(
        outcome=outcome,
        external_id=value.get("external_id") or value.get("externalId"),
        context_id=value.get("context_id") or value.get("contextId"),
        response=dict(value),
    )


def normalize_probe_result(value):
    if isinstance(value, ProbeResult):
        return value
    if not isinstance(value, Mapping):
        return ProbeResult.unknown(response={"value": value})
    outcome = str(value.get("outcome") or value.get("status") or "Unknown")
    normalized = outcome.strip().lower().replace("_", " ")
    if normalized in {"found", "exists", "succeeded", "success"}:
        outcome = "Found"
    elif normalized in {"not found", "absent", "missing"}:
        outcome = "Not Found"
    else:
        outcome = "Unknown"
    return ProbeResult(
        outcome=outcome,
        external_id=value.get("external_id") or value.get("externalId"),
        context_id=value.get("context_id") or value.get("contextId"),
        response=dict(value),
    )
