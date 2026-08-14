"""Structured error model for the Unified Agent (tool) Registry.

Every tool invocation that cannot complete successfully surfaces a
:class:`ToolError` rather than raising, so the WebSocket Gateway can translate
failures into a uniform client-facing payload (design "Error Handling" table).

The registry distinguishes four error types (Requirement 7.4):

    * ``timeout``       — a tool did not return within its time budget (60s).
    * ``validation``    — inputs to the tool were structurally invalid.
    * ``execution``     — the tool ran but failed (raised / returned an error).
    * ``authorization`` — the caller may not operate on the referenced resource
      (drawing) and the tool was **not** executed (Requirement 7.3).

Authorization errors additionally carry the denied ``resource_id`` and a
``denial_reason`` (Requirement 7.3). All errors carry a ``recovery_suggestion``
so the user is always told at least one way forward (Requirement 7.4).

This module is pure and SDK-free: it has no boto3 / Strands / AgentCore
dependency and can be imported and unit-tested in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class ToolErrorType(str, Enum):
    """The categories of tool-invocation failure (Requirement 7.4).

    Values are stable lowercase strings so they can be embedded directly in
    structured error payloads and asserted against in tests.
    """

    TIMEOUT = "timeout"
    VALIDATION = "validation"
    EXECUTION = "execution"
    AUTHORIZATION = "authorization"


# The exact set of permitted ``error_type`` values (Requirement 7.4). Exposed
# as a frozenset for cheap membership checks and test assertions.
ERROR_TYPES: frozenset[str] = frozenset(t.value for t in ToolErrorType)

# Default recovery suggestions per error type. Callers may override with a more
# specific message; these guarantee a non-empty suggestion is always present.
_DEFAULT_RECOVERY: dict[ToolErrorType, str] = {
    ToolErrorType.TIMEOUT: (
        "The tool took too long to respond. Retry the request, and if it "
        "persists narrow the scope of the request or try again later."
    ),
    ToolErrorType.VALIDATION: (
        "The request inputs were invalid. Correct the highlighted fields and "
        "resubmit the request."
    ),
    ToolErrorType.EXECUTION: (
        "The tool failed while running. Retry the request; if it persists, "
        "report the issue with the tool name and inputs."
    ),
    ToolErrorType.AUTHORIZATION: (
        "You do not have access to the referenced drawing. Verify the "
        "resource id or request access from the drawing owner."
    ),
}


@dataclass(frozen=True)
class ToolError:
    """A structured, serializable tool-invocation error.

    Attributes:
        tool_name: The tool that failed (always present, Req 7.3/7.4).
        error_type: One of :class:`ToolErrorType` (Req 7.4).
        recovery_suggestion: A non-empty, human-readable next step (Req 7.4).
        resource_id: The denied drawing/resource id — set for authorization
            errors (Req 7.3); ``None`` otherwise.
        denial_reason: Why authorization was denied — set for authorization
            errors (Req 7.3); ``None`` otherwise.
        detail: Optional additional context (e.g. the underlying exception
            message or the timeout budget).
    """

    tool_name: str
    error_type: ToolErrorType
    recovery_suggestion: str
    resource_id: str | None = None
    denial_reason: str | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        # Guarantee the invariants relied on by the WebSocket Gateway/tests:
        # a known error type and a non-empty recovery suggestion.
        if self.error_type not in ToolErrorType:
            raise ValueError(f"Unknown error_type: {self.error_type!r}")
        if not self.tool_name or not self.tool_name.strip():
            raise ValueError("tool_name must be a non-empty string")
        if not self.recovery_suggestion or not self.recovery_suggestion.strip():
            raise ValueError("recovery_suggestion must be a non-empty string")

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for JSON transport.

        ``resource_id``, ``denial_reason``, and ``detail`` are only included
        when set so non-authorization errors stay compact.
        """
        payload: dict[str, Any] = {
            "tool_name": self.tool_name,
            "error_type": self.error_type.value,
            "recovery_suggestion": self.recovery_suggestion,
        }
        if self.resource_id is not None:
            payload["resource_id"] = self.resource_id
        if self.denial_reason is not None:
            payload["denial_reason"] = self.denial_reason
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload


def _recovery(error_type: ToolErrorType, override: str | None) -> str:
    """Return ``override`` if provided and non-blank, else the default."""
    if override and override.strip():
        return override.strip()
    return _DEFAULT_RECOVERY[error_type]


def authorization_error(
    tool_name: str,
    resource_id: str | None,
    denial_reason: str,
    recovery_suggestion: str | None = None,
) -> ToolError:
    """Build an ``authorization`` error (Requirement 7.3).

    Carries the denied ``resource_id`` and ``denial_reason`` in addition to the
    tool name and recovery suggestion.
    """
    return ToolError(
        tool_name=tool_name,
        error_type=ToolErrorType.AUTHORIZATION,
        recovery_suggestion=_recovery(ToolErrorType.AUTHORIZATION, recovery_suggestion),
        resource_id=resource_id,
        denial_reason=denial_reason,
    )


def timeout_error(
    tool_name: str,
    timeout_s: float | None = None,
    recovery_suggestion: str | None = None,
) -> ToolError:
    """Build a ``timeout`` error for a tool that exceeded its budget (Req 7.4)."""
    detail = f"Exceeded {timeout_s:g}s time budget." if timeout_s is not None else None
    return ToolError(
        tool_name=tool_name,
        error_type=ToolErrorType.TIMEOUT,
        recovery_suggestion=_recovery(ToolErrorType.TIMEOUT, recovery_suggestion),
        detail=detail,
    )


def validation_error(
    tool_name: str,
    detail: str | None = None,
    recovery_suggestion: str | None = None,
) -> ToolError:
    """Build a ``validation`` error for structurally invalid inputs (Req 7.4)."""
    return ToolError(
        tool_name=tool_name,
        error_type=ToolErrorType.VALIDATION,
        recovery_suggestion=_recovery(ToolErrorType.VALIDATION, recovery_suggestion),
        detail=detail,
    )


def execution_error(
    tool_name: str,
    detail: str | None = None,
    recovery_suggestion: str | None = None,
) -> ToolError:
    """Build an ``execution`` error for a tool that ran but failed (Req 7.4)."""
    return ToolError(
        tool_name=tool_name,
        error_type=ToolErrorType.EXECUTION,
        recovery_suggestion=_recovery(ToolErrorType.EXECUTION, recovery_suggestion),
        detail=detail,
    )


__all__ = [
    "ERROR_TYPES",
    "ToolError",
    "ToolErrorType",
    "authorization_error",
    "execution_error",
    "timeout_error",
    "validation_error",
]
