"""Tool invocation and multi-tool turn orchestration for the Agent Registry.

This module provides the pure decision/orchestration layer that sits in front
of real tool execution (Requirements 7.2, 7.4, 7.5):

    * :func:`invoke_with_timeout` runs a single tool callable under a time
      budget (default 60s) and converts a timeout — or any raised exception —
      into a structured :class:`~shared.tool_registry.errors.ToolError`
      (Requirement 7.4).
    * :func:`run_tool_sequence` runs 2–5 tool invocations sequentially within a
      single conversation turn (Requirement 7.2). It performs the authorization
      check *before* each invocation (Requirement 7.3) and, on any failure,
      returns the results of all successful tools alongside the structured
      errors for the failed ones, **without rolling back** prior successes
      (Requirement 7.5, design Property 13).

The clock/executor is injectable via the ``runner`` parameter so tests are
deterministic and fast — they never sleep for the real 60s budget. The default
runner uses ``concurrent.futures`` and enforces a wall-clock timeout.

The module is SDK-free: the actual side-effecting work (boto3 / AgentCore /
Gateway call) is supplied by the caller as a plain zero-argument callable, so
all decision logic here stays unit-testable in isolation.
"""

from __future__ import annotations

import concurrent.futures
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .authorization import AccessOracle, authorize_tool_invocation
from .errors import ToolError, execution_error, timeout_error

# Bounds on the number of tool invocations per conversation turn (Req 7.2).
MIN_TOOLS_PER_TURN = 2
MAX_TOOLS_PER_TURN = 5

# Default per-tool time budget in seconds (Requirement 7.4).
DEFAULT_TIMEOUT_S = 60.0

# A runner executes ``fn`` under ``timeout_s`` seconds, returning its result or
# raising ``TimeoutError`` when the budget is exceeded. Injectable for tests.
Runner = Callable[[Callable[[], Any], float], Any]


@dataclass(frozen=True)
class ToolInvocation:
    """A single tool invocation request within a conversation turn.

    Attributes:
        tool_name: The tool to invoke.
        fn: A zero-argument callable performing the actual invocation. It is
            only called after the authorization check passes.
        resource_id: The drawing/resource the tool operates on, or ``None`` if
            the tool references no specific drawing.
    """

    tool_name: str
    fn: Callable[[], Any] = field(compare=False, default=lambda: None)
    resource_id: str | None = None


@dataclass(frozen=True)
class ToolResult:
    """The outcome of a single tool invocation.

    Exactly one of ``value`` / ``error`` is meaningful depending on ``success``.

    Attributes:
        tool_name: The tool this result is for.
        success: ``True`` when the tool completed without error.
        value: The tool's return value when ``success`` is ``True``.
        error: The structured :class:`ToolError` when ``success`` is ``False``.
        resource_id: The referenced resource id, if any.
    """

    tool_name: str
    success: bool
    value: Any = None
    error: ToolError | None = None
    resource_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "tool_name": self.tool_name,
            "success": self.success,
        }
        if self.resource_id is not None:
            payload["resource_id"] = self.resource_id
        if self.success:
            payload["value"] = self.value
        elif self.error is not None:
            payload["error"] = self.error.to_dict()
        return payload


@dataclass(frozen=True)
class TurnResult:
    """Aggregate outcome of a multi-tool conversation turn (Req 7.5).

    Attributes:
        results: Per-invocation :class:`ToolResult` objects, in invocation
            order (successes and failures interleaved as they occurred).
    """

    results: tuple[ToolResult, ...]

    @property
    def successes(self) -> list[ToolResult]:
        """Results for tools that completed successfully (order preserved)."""
        return [r for r in self.results if r.success]

    @property
    def errors(self) -> list[ToolError]:
        """Structured errors for tools that failed (order preserved)."""
        return [r.error for r in self.results if not r.success and r.error is not None]

    @property
    def all_succeeded(self) -> bool:
        return all(r.success for r in self.results)

    @property
    def any_failed(self) -> bool:
        return any(not r.success for r in self.results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "successes": [r.to_dict() for r in self.successes],
            "errors": [e.to_dict() for e in self.errors],
            "results": [r.to_dict() for r in self.results],
        }


def _threaded_runner(fn: Callable[[], Any], timeout_s: float) -> Any:
    """Default runner: execute ``fn`` on a worker thread with a wall clock.

    Raises ``TimeoutError`` (``concurrent.futures.TimeoutError`` is an alias of
    the builtin on Python 3.11+) when ``fn`` does not return within
    ``timeout_s``. The executor is not waited on when a timeout occurs so a
    hung tool does not block the turn.
    """
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(fn)
    try:
        return future.result(timeout=timeout_s)
    finally:
        # Do not block on a hung worker; let it be reclaimed in the background.
        executor.shutdown(wait=False)


def invoke_with_timeout(
    fn: Callable[[], Any],
    *,
    tool_name: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    resource_id: str | None = None,
    runner: Runner | None = None,
) -> ToolResult:
    """Invoke ``fn`` under a time budget, returning a structured result.

    Converts a timeout into a ``timeout`` :class:`ToolError` and any raised
    exception into an ``execution`` :class:`ToolError` (Requirement 7.4). Never
    raises for tool-level failures; only invalid arguments (e.g. a
    non-positive ``timeout_s``) raise.

    Args:
        fn: Zero-argument callable performing the invocation.
        tool_name: Name of the tool (embedded in any error).
        timeout_s: Time budget in seconds (default 60).
        resource_id: Optional referenced resource id, echoed on the result.
        runner: Optional injected runner ``(fn, timeout_s) -> result`` used to
            make timeouts deterministic in tests. Defaults to a thread-based
            wall-clock runner.

    Returns:
        A :class:`ToolResult` — ``success=True`` with ``value`` on success, or
        ``success=False`` with a structured ``error`` on timeout/failure.
    """
    if timeout_s <= 0:
        raise ValueError("timeout_s must be positive")

    run = runner if runner is not None else _threaded_runner
    try:
        value = run(fn, timeout_s)
    except TimeoutError:
        return ToolResult(
            tool_name=tool_name,
            success=False,
            error=timeout_error(tool_name, timeout_s=timeout_s),
            resource_id=resource_id,
        )
    except Exception as exc:  # noqa: BLE001 - convert any tool failure to a ToolError.
        return ToolResult(
            tool_name=tool_name,
            success=False,
            error=execution_error(tool_name, detail=str(exc)),
            resource_id=resource_id,
        )

    return ToolResult(
        tool_name=tool_name,
        success=True,
        value=value,
        resource_id=resource_id,
    )


def run_tool_sequence(
    invocations: Sequence[ToolInvocation],
    *,
    user_id: str,
    access: AccessOracle,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    runner: Runner | None = None,
    enforce_turn_bounds: bool = True,
) -> TurnResult:
    """Run a multi-tool turn sequentially with per-tool auth + timeouts.

    For each invocation, in order (Requirements 7.2, 7.3, 7.4, 7.5):

        1. Authorize the invocation *before* executing it. On denial, record
           the structured authorization error and skip execution of that tool.
        2. Otherwise invoke it under the time budget via
           :func:`invoke_with_timeout`, recording success or a structured
           timeout/execution error.

    A failure of one tool never rolls back or discards the results of tools
    that already succeeded (Requirement 7.5): every invocation contributes a
    :class:`ToolResult` to the returned :class:`TurnResult` in order.

    Args:
        invocations: The 2–5 tool invocations for this turn.
        user_id: The authenticated user id (for authorization).
        access: The access oracle (see
            :data:`~shared.tool_registry.authorization.AccessOracle`).
        timeout_s: Per-tool time budget in seconds (default 60).
        runner: Optional injected runner for deterministic timeouts in tests.
        enforce_turn_bounds: When ``True`` (default), a turn must contain
            between :data:`MIN_TOOLS_PER_TURN` and :data:`MAX_TOOLS_PER_TURN`
            invocations (Req 7.2); a violation raises :class:`ValueError`.

    Returns:
        A :class:`TurnResult` aggregating successes and structured errors.

    Raises:
        ValueError: If ``enforce_turn_bounds`` is set and the number of
            invocations is outside the 2–5 range.
    """
    count = len(invocations)
    if enforce_turn_bounds and not (MIN_TOOLS_PER_TURN <= count <= MAX_TOOLS_PER_TURN):
        raise ValueError(
            f"A multi-tool turn must contain between {MIN_TOOLS_PER_TURN} and "
            f"{MAX_TOOLS_PER_TURN} invocations (got {count})."
        )

    results: list[ToolResult] = []
    for inv in invocations:
        # Step 1: authorize BEFORE invoking (Req 7.3). A denial does not
        # execute the tool.
        auth = authorize_tool_invocation(
            user_id=user_id,
            tool_name=inv.tool_name,
            resource_id=inv.resource_id,
            access=access,
        )
        if not auth.should_execute:
            results.append(
                ToolResult(
                    tool_name=inv.tool_name,
                    success=False,
                    error=auth.error,
                    resource_id=inv.resource_id,
                )
            )
            continue

        # Step 2: invoke under the time budget (Req 7.4). Prior successes in
        # ``results`` are never rolled back on failure (Req 7.5).
        results.append(
            invoke_with_timeout(
                inv.fn,
                tool_name=inv.tool_name,
                timeout_s=timeout_s,
                resource_id=inv.resource_id,
                runner=runner,
            )
        )

    return TurnResult(results=tuple(results))


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "MAX_TOOLS_PER_TURN",
    "MIN_TOOLS_PER_TURN",
    "Runner",
    "ToolInvocation",
    "ToolResult",
    "TurnResult",
    "invoke_with_timeout",
    "run_tool_sequence",
]
