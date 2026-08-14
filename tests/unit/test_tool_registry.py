"""Unit tests for the Unified Agent (tool) Registry.

Covers the pure, SDK-free authorization, timeout, and multi-tool turn logic
(design "Unified Agent Tool Registry", Requirements 7.2, 7.3, 7.4, 7.5):

* Structured error model: the four error types, required fields, ``to_dict``.
* Per-tool authorization before execution, with structured denial errors.
* 60-second timeout wrapper producing a structured timeout error (using an
  injected runner so no test sleeps for the real budget).
* Multi-tool turn runner: 2–5 bounds, partial failures return successes plus
  structured errors, and no rollback of prior successes.

Validates: Requirements 7.2, 7.3, 7.4, 7.5
"""

from __future__ import annotations

import pytest

from shared.tool_registry import (
    ERROR_TYPES,
    AuthorizationResult,
    ToolError,
    ToolErrorType,
    ToolInvocation,
    ToolResult,
    TurnResult,
    authorization_error,
    authorize_tool_invocation,
    execution_error,
    invoke_with_timeout,
    run_tool_sequence,
    timeout_error,
    validation_error,
)

# --- Error model ------------------------------------------------------------


def test_error_types_are_the_four_required_values() -> None:
    assert ERROR_TYPES == {"timeout", "validation", "execution", "authorization"}
    assert {t.value for t in ToolErrorType} == ERROR_TYPES


def test_authorization_error_carries_resource_and_reason() -> None:
    err = authorization_error("zoom_and_read", "DRW-9", "no grant")
    assert err.tool_name == "zoom_and_read"
    assert err.error_type is ToolErrorType.AUTHORIZATION
    assert err.resource_id == "DRW-9"
    assert err.denial_reason == "no grant"
    assert err.recovery_suggestion  # non-empty (Req 7.4)

    payload = err.to_dict()
    assert payload["tool_name"] == "zoom_and_read"
    assert payload["error_type"] == "authorization"
    assert payload["resource_id"] == "DRW-9"
    assert payload["denial_reason"] == "no grant"
    assert payload["recovery_suggestion"]


def test_timeout_error_shape_and_serialization() -> None:
    err = timeout_error("run_optimization", timeout_s=60)
    assert err.error_type is ToolErrorType.TIMEOUT
    payload = err.to_dict()
    assert payload["tool_name"] == "run_optimization"
    assert payload["error_type"] == "timeout"
    assert payload["recovery_suggestion"]
    # Non-authorization errors omit resource/denial fields.
    assert "resource_id" not in payload
    assert "denial_reason" not in payload


def test_validation_and_execution_error_types() -> None:
    assert validation_error("check_compliance").error_type is ToolErrorType.VALIDATION
    assert execution_error("check_compliance").error_type is ToolErrorType.EXECUTION


def test_recovery_suggestion_override_is_used() -> None:
    err = timeout_error("t", timeout_s=1, recovery_suggestion="do the thing")
    assert err.recovery_suggestion == "do the thing"


def test_tool_error_requires_non_empty_recovery_suggestion() -> None:
    with pytest.raises(ValueError):
        ToolError(
            tool_name="t",
            error_type=ToolErrorType.EXECUTION,
            recovery_suggestion="   ",
        )


def test_tool_error_requires_non_empty_tool_name() -> None:
    with pytest.raises(ValueError):
        ToolError(
            tool_name="",
            error_type=ToolErrorType.EXECUTION,
            recovery_suggestion="retry",
        )


# --- Authorization ----------------------------------------------------------


def test_authorize_allows_when_user_has_access_set_oracle() -> None:
    result = authorize_tool_invocation("u1", "zoom_and_read", "DRW-1", {"DRW-1", "DRW-2"})
    assert isinstance(result, AuthorizationResult)
    assert result.authorized is True
    assert result.should_execute is True
    assert result.error is None


def test_authorize_denies_with_structured_error() -> None:
    result = authorize_tool_invocation("u1", "zoom_and_read", "DRW-9", {"DRW-1"})
    assert result.authorized is False
    assert result.should_execute is False
    assert result.error is not None
    assert result.error.error_type is ToolErrorType.AUTHORIZATION
    assert result.error.tool_name == "zoom_and_read"
    assert result.error.resource_id == "DRW-9"
    assert result.error.denial_reason  # explains the denial (Req 7.3)


def test_authorize_with_mapping_oracle() -> None:
    access = {"u1": {"DRW-1"}, "u2": {"DRW-2"}}
    assert authorize_tool_invocation("u1", "t", "DRW-1", access).authorized
    assert not authorize_tool_invocation("u1", "t", "DRW-2", access).authorized
    # Unknown user -> denied.
    assert not authorize_tool_invocation("u3", "t", "DRW-1", access).authorized


def test_authorize_with_callable_oracle() -> None:
    def oracle(user_id: str, resource_id: str) -> bool:
        return user_id == "owner" and resource_id.startswith("DRW-")

    assert authorize_tool_invocation("owner", "t", "DRW-5", oracle).authorized
    assert not authorize_tool_invocation("guest", "t", "DRW-5", oracle).authorized


def test_authorize_allows_when_no_resource_referenced() -> None:
    # A tool that does not reference a drawing has nothing to guard.
    result = authorize_tool_invocation("u1", "retrieve_from_knowledge_base", None, set())
    assert result.authorized is True


def test_authorize_denies_missing_user() -> None:
    result = authorize_tool_invocation("", "t", "DRW-1", {"DRW-1"})
    assert result.authorized is False
    assert result.error is not None
    assert result.error.error_type is ToolErrorType.AUTHORIZATION


def test_authorize_fails_closed_on_bad_oracle() -> None:
    result = authorize_tool_invocation("u1", "t", "DRW-1", 12345)  # type: ignore[arg-type]
    assert result.authorized is False
    assert result.error is not None
    assert result.error.error_type is ToolErrorType.AUTHORIZATION


# --- invoke_with_timeout ----------------------------------------------------


def test_invoke_success_returns_value() -> None:
    result = invoke_with_timeout(lambda: 42, tool_name="t")
    assert result.success is True
    assert result.value == 42
    assert result.error is None


def test_invoke_timeout_returns_structured_error_without_sleeping() -> None:
    # Injected runner simulates a timeout deterministically.
    def slow_runner(fn, timeout_s):
        raise TimeoutError("simulated")

    result = invoke_with_timeout(
        lambda: "never", tool_name="run_optimization", timeout_s=60, runner=slow_runner
    )
    assert result.success is False
    assert result.error is not None
    assert result.error.error_type is ToolErrorType.TIMEOUT
    assert result.error.tool_name == "run_optimization"
    assert result.error.recovery_suggestion


def test_invoke_exception_becomes_execution_error() -> None:
    def boom() -> None:
        raise RuntimeError("kaboom")

    result = invoke_with_timeout(boom, tool_name="check_compliance")
    assert result.success is False
    assert result.error is not None
    assert result.error.error_type is ToolErrorType.EXECUTION
    assert "kaboom" in (result.error.detail or "")


def test_invoke_real_thread_runner_enforces_timeout() -> None:
    import time

    # Use the default thread runner with a tiny budget to prove the real path
    # produces a timeout error quickly (no 60s wait).
    result = invoke_with_timeout(
        lambda: time.sleep(5), tool_name="t", timeout_s=0.05
    )
    assert result.success is False
    assert result.error is not None
    assert result.error.error_type is ToolErrorType.TIMEOUT


def test_invoke_rejects_non_positive_timeout() -> None:
    with pytest.raises(ValueError):
        invoke_with_timeout(lambda: 1, tool_name="t", timeout_s=0)


# --- run_tool_sequence ------------------------------------------------------


def _ok(name: str, value: object, resource_id: str | None = None) -> ToolInvocation:
    return ToolInvocation(tool_name=name, fn=lambda v=value: v, resource_id=resource_id)


def test_turn_all_success() -> None:
    invocations = [_ok("a", 1), _ok("b", 2), _ok("c", 3)]
    turn = run_tool_sequence(invocations, user_id="u1", access=set())
    assert isinstance(turn, TurnResult)
    assert turn.all_succeeded is True
    assert [r.value for r in turn.successes] == [1, 2, 3]
    assert turn.errors == []


def test_turn_partial_failure_keeps_successes_no_rollback() -> None:
    def boom() -> None:
        raise ValueError("bad")

    invocations = [
        _ok("a", "ok-a"),
        ToolInvocation(tool_name="b", fn=boom),
        _ok("c", "ok-c"),
    ]
    turn = run_tool_sequence(invocations, user_id="u1", access=set())

    # Successful tools' results are preserved (Req 7.5, no rollback).
    assert [r.tool_name for r in turn.successes] == ["a", "c"]
    assert [r.value for r in turn.successes] == ["ok-a", "ok-c"]

    # The failed tool contributes a structured error.
    assert len(turn.errors) == 1
    assert turn.errors[0].tool_name == "b"
    assert turn.errors[0].error_type is ToolErrorType.EXECUTION
    assert turn.any_failed is True


def test_turn_authorization_denied_skips_execution() -> None:
    executed: list[str] = []

    def guarded() -> str:
        executed.append("b")
        return "ran"

    invocations = [
        _ok("a", 1, resource_id="DRW-1"),
        ToolInvocation(tool_name="b", fn=guarded, resource_id="DRW-9"),
        _ok("c", 3, resource_id="DRW-1"),
    ]
    # User can access DRW-1 but not DRW-9.
    turn = run_tool_sequence(invocations, user_id="u1", access={"DRW-1"})

    # The unauthorized tool was NOT executed (Req 7.3).
    assert executed == []
    # Its result is a structured authorization error carrying the resource id.
    assert len(turn.errors) == 1
    err = turn.errors[0]
    assert err.error_type is ToolErrorType.AUTHORIZATION
    assert err.tool_name == "b"
    assert err.resource_id == "DRW-9"
    # Authorized tools still succeed (no rollback).
    assert [r.tool_name for r in turn.successes] == ["a", "c"]


def test_turn_timeout_uses_injected_runner() -> None:
    calls = {"n": 0}

    def flaky_runner(fn, timeout_s):
        calls["n"] += 1
        if calls["n"] == 2:  # second tool "times out"
            raise TimeoutError("simulated")
        return fn()

    invocations = [_ok("a", 1), _ok("b", 2), _ok("c", 3)]
    turn = run_tool_sequence(
        invocations, user_id="u1", access=set(), timeout_s=60, runner=flaky_runner
    )
    assert [r.tool_name for r in turn.successes] == ["a", "c"]
    assert len(turn.errors) == 1
    assert turn.errors[0].error_type is ToolErrorType.TIMEOUT
    assert turn.errors[0].tool_name == "b"


@pytest.mark.parametrize("count", [0, 1, 6, 7])
def test_turn_enforces_2_to_5_bounds(count: int) -> None:
    invocations = [_ok(f"t{i}", i) for i in range(count)]
    with pytest.raises(ValueError, match="between 2 and 5"):
        run_tool_sequence(invocations, user_id="u1", access=set())


@pytest.mark.parametrize("count", [2, 3, 4, 5])
def test_turn_accepts_2_to_5_invocations(count: int) -> None:
    invocations = [_ok(f"t{i}", i) for i in range(count)]
    turn = run_tool_sequence(invocations, user_id="u1", access=set())
    assert len(turn.results) == count


def test_turn_bounds_can_be_disabled() -> None:
    turn = run_tool_sequence(
        [_ok("only", 1)], user_id="u1", access=set(), enforce_turn_bounds=False
    )
    assert len(turn.results) == 1


def test_turn_to_dict_structure() -> None:
    def boom() -> None:
        raise RuntimeError("x")

    invocations = [_ok("a", 1), ToolInvocation(tool_name="b", fn=boom)]
    turn = run_tool_sequence(invocations, user_id="u1", access=set())
    payload = turn.to_dict()
    assert {"successes", "errors", "results"} == set(payload)
    assert len(payload["successes"]) == 1
    assert len(payload["errors"]) == 1
    assert len(payload["results"]) == 2
    assert payload["errors"][0]["error_type"] == "execution"


def test_tool_result_to_dict_success_and_failure() -> None:
    ok = ToolResult(tool_name="a", success=True, value=99)
    assert ok.to_dict() == {"tool_name": "a", "success": True, "value": 99}

    bad = ToolResult(
        tool_name="b",
        success=False,
        error=execution_error("b", detail="d"),
        resource_id="DRW-1",
    )
    payload = bad.to_dict()
    assert payload["success"] is False
    assert payload["resource_id"] == "DRW-1"
    assert payload["error"]["error_type"] == "execution"
