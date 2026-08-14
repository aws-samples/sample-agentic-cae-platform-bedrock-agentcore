"""Unified Agent (tool) Registry: authorization, timeouts, and error handling.

This package implements the decision layer that guards and orchestrates tool
invocations for the platform agents (Requirements 7.2–7.5):

- ``errors`` — the structured :class:`ToolError` model and constructors for the
  four error types (``timeout``, ``validation``, ``execution``,
  ``authorization``).
- ``authorization`` — :func:`authorize_tool_invocation`, which verifies a user
  may operate on a referenced drawing *before* a tool runs (Req 7.3).
- ``invocation`` — :func:`invoke_with_timeout` (60s budget, Req 7.4) and
  :func:`run_tool_sequence`, the sequential multi-tool turn runner that returns
  partial successes plus structured errors without rollback (Req 7.2, 7.5).

All logic is pure and SDK-free; real side effects are supplied by callers as
plain callables, keeping the registry unit-testable in isolation.
"""

from .authorization import (
    AccessOracle,
    AuthorizationResult,
    authorize_tool_invocation,
)
from .errors import (
    ERROR_TYPES,
    ToolError,
    ToolErrorType,
    authorization_error,
    execution_error,
    timeout_error,
    validation_error,
)
from .invocation import (
    DEFAULT_TIMEOUT_S,
    MAX_TOOLS_PER_TURN,
    MIN_TOOLS_PER_TURN,
    Runner,
    ToolInvocation,
    ToolResult,
    TurnResult,
    invoke_with_timeout,
    run_tool_sequence,
)

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "ERROR_TYPES",
    "MAX_TOOLS_PER_TURN",
    "MIN_TOOLS_PER_TURN",
    "AccessOracle",
    "AuthorizationResult",
    "Runner",
    "ToolError",
    "ToolErrorType",
    "ToolInvocation",
    "ToolResult",
    "TurnResult",
    "authorization_error",
    "authorize_tool_invocation",
    "execution_error",
    "invoke_with_timeout",
    "run_tool_sequence",
    "timeout_error",
    "validation_error",
]
