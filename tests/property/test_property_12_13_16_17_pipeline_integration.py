"""Property-based tests for end-to-end pipeline integration (task 14.4).

Covers four correctness properties from the design document. Each property is
exercised against the REAL, pure, SDK-free helpers implemented in tasks
14.1/14.2/14.3 — no logic is reimplemented here.

- Property 12: Authorization Enforcement
  ``shared.tool_registry.authorize_tool_invocation`` grants access iff the user
  may operate on the referenced resource (or no resource is referenced), and on
  denial returns a structured authorization error carrying the tool name, the
  denied resource id, and a non-empty denial reason — and the tool is NOT
  executed. **Validates: Requirements 7.3**

- Property 13: Multi-Tool Partial Failure Handling
  ``shared.tool_registry.run_tool_sequence`` returns a ToolResult for EVERY
  invocation in order, preserves every successful tool's result (no rollback),
  and contributes a structured error with a valid ``error_type`` for every
  failed tool. **Validates: Requirements 7.5**

- Property 16: Pipeline Traceability
  ``shared.pipeline.orchestration.attach_traceability`` (and
  ``agents.optimizer.main.finalize_optimization_run``) stamp every result
  record with ``drawing_id`` and ``extraction_id`` without mutating the input.
  **Validates: Requirements 11.5**

- Property 17: Pipeline Failure Notification Completeness
  ``shared.notifications.build_failure_notification`` always yields a
  non-empty stage name, a non-empty description, and a non-empty list of
  non-empty recovery actions for ANY stage/description/override combination.
  **Validates: Requirements 11.6**

The generators emit random-but-valid inputs across the documented input space;
expected outcomes are recomputed independently of helper internals so the tests
do not merely restate the implementation. All settings use >= 100 examples per
the design's property-testing configuration.
"""

from __future__ import annotations

import copy

from hypothesis import given, settings
from hypothesis import strategies as st

from agents.optimizer.main import finalize_optimization_run
from shared.notifications import (
    PipelineStage,
    RecoveryAction,
    build_failure_notification,
)
from shared.pipeline.orchestration import (
    attach_traceability,
    build_traceability_record,
)
from shared.tool_registry import (
    ERROR_TYPES,
    ToolErrorType,
    ToolInvocation,
    authorize_tool_invocation,
    run_tool_sequence,
)

# ===========================================================================
# Shared strategies
# ===========================================================================

# The seven tools exposed by the Unified Agent Registry (design §7).
_TOOL_NAMES = [
    "retrieve_from_knowledge_base",
    "zoom_and_read",
    "extract_pdf_annotations",
    "check_compliance",
    "run_optimization",
    "generate_3d_model",
    "validate_constraints",
]

_ID_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"

# Non-empty, non-whitespace authenticated user ids (blank users always deny).
_user_id = st.text(alphabet=_ID_ALPHABET, min_size=1, max_size=16)

# Drawing/resource identifiers.
_resource_id = st.text(alphabet=_ID_ALPHABET, min_size=1, max_size=12).map(lambda s: f"drw-{s}")


def _immediate_runner(fn, timeout_s):  # noqa: ARG001 - timeout unused by design
    """Deterministic runner that simply invokes ``fn`` (no threads, no sleeping)."""
    return fn()


def _flag_runner(fn, timeout_s):  # noqa: ARG001 - timeout unused by design
    """Runner that simulates a timeout for functions flagged to do so.

    A real timeout is modelled by raising ``TimeoutError`` *without* invoking
    ``fn`` — mirroring the production behaviour where a hung tool never returns
    — so the property runs instantly instead of waiting the real 60s budget.
    """
    if getattr(fn, "_simulate_timeout", False):
        raise TimeoutError("simulated timeout")
    return fn()


# ===========================================================================
# Property 12: Authorization Enforcement (Requirement 7.3)
# ===========================================================================
# Feature: sample-agentic-cae-platform, Property 12: Authorization Enforcement


@settings(max_examples=250, deadline=None)
@given(
    user_id=_user_id,
    tool_name=st.sampled_from(_TOOL_NAMES),
    resource_id=st.one_of(st.none(), _resource_id),
    allowed=st.lists(_resource_id, max_size=6),
    oracle_shape=st.sampled_from(["callable", "mapping", "collection"]),
)
def test_property_12_authorization_grants_iff_access(
    user_id: str,
    tool_name: str,
    resource_id: str | None,
    allowed: list[str],
    oracle_shape: str,
) -> None:
    """Authorization is granted iff the user may access the resource.

    On denial the structured error carries the tool name, the denied resource
    id, and a non-empty denial reason, and ``should_execute`` is False.

    **Validates: Requirements 7.3**
    """
    allowed_set = set(allowed)

    if oracle_shape == "callable":
        access = lambda u, r: (u == user_id) and (r in allowed_set)  # noqa: E731
    elif oracle_shape == "mapping":
        access = {user_id: allowed_set}
    else:  # collection scoped to the current user
        access = allowed_set

    result = authorize_tool_invocation(user_id, tool_name, resource_id, access)

    if resource_id is None:
        # No referenced drawing -> nothing to guard -> allow.
        assert result.authorized is True
        assert result.should_execute is True
        assert result.error is None
        return

    expected = resource_id in allowed_set
    assert result.authorized is expected
    assert result.should_execute is expected

    if expected:
        assert result.error is None
    else:
        error = result.error
        assert error is not None
        assert error.error_type is ToolErrorType.AUTHORIZATION
        payload = error.to_dict()
        assert payload["tool_name"] == tool_name
        assert payload["resource_id"] == resource_id
        assert payload["error_type"] == "authorization"
        assert isinstance(payload["denial_reason"], str) and payload["denial_reason"].strip()
        assert (
            isinstance(payload["recovery_suggestion"], str)
            and payload["recovery_suggestion"].strip()
        )


@settings(max_examples=200, deadline=None)
@given(
    user_id=_user_id,
    specs=st.lists(st.tuples(st.sampled_from(_TOOL_NAMES), st.booleans()), min_size=2, max_size=5),
)
def test_property_12_denied_tool_is_not_executed(
    user_id: str, specs: list[tuple[str, bool]]
) -> None:
    """A denied tool's ``fn`` is never invoked and yields an authorization error.

    Runs a multi-tool turn where each invocation is authorized or not; the
    registry must authorize BEFORE executing, so denied tools never run.

    **Validates: Requirements 7.3**
    """
    called: list[int] = []
    invocations: list[ToolInvocation] = []
    access_set: set[str] = set()
    denied: dict[int, bool] = {}

    def _make_fn(idx: int):
        def fn():
            called.append(idx)
            return f"ok-{idx}"

        return fn

    for i, (tool_name, authorized) in enumerate(specs):
        resource_id = f"drw-{i}"
        if authorized:
            access_set.add(resource_id)
        denied[i] = not authorized
        invocations.append(
            ToolInvocation(tool_name=tool_name, fn=_make_fn(i), resource_id=resource_id)
        )

    result = run_tool_sequence(
        invocations, user_id=user_id, access=access_set, runner=_immediate_runner
    )

    assert len(result.results) == len(invocations)
    for i, res in enumerate(result.results):
        if denied[i]:
            assert res.success is False
            assert res.error is not None
            assert res.error.error_type is ToolErrorType.AUTHORIZATION
            assert res.error.resource_id == f"drw-{i}"
            assert i not in called  # the tool was never executed
        else:
            assert res.success is True
            assert res.value == f"ok-{i}"
            assert called.count(i) == 1


# ===========================================================================
# Property 13: Multi-Tool Partial Failure Handling (Requirement 7.5)
# ===========================================================================
# Feature: sample-agentic-cae-platform, Property 13: Multi-Tool Partial Failure Handling

_BEHAVIORS = ["success", "raise", "timeout", "unauthorized"]


def _make_tool_fn(index: int, behavior: str, called: list[int]):
    """Build a tool ``fn`` for the given behavior, tracking whether it ran."""

    def fn():
        called.append(index)
        if behavior == "raise":
            raise RuntimeError(f"boom-{index}")
        return f"ok-{index}"

    if behavior == "timeout":
        # Flag so ``_flag_runner`` simulates a timeout without invoking ``fn``.
        fn._simulate_timeout = True  # type: ignore[attr-defined]
    return fn


def _build_turn(behaviors: list[str]) -> tuple[list[ToolInvocation], set[str], list[int]]:
    """Assemble invocations + access oracle for a list of per-tool behaviors."""
    called: list[int] = []
    invocations: list[ToolInvocation] = []
    access_set: set[str] = set()
    for i, behavior in enumerate(behaviors):
        if behavior == "unauthorized":
            resource_id = f"denied-{i}"  # deliberately NOT authorized
        else:
            resource_id = f"allowed-{i}"
            access_set.add(resource_id)
        invocations.append(
            ToolInvocation(
                tool_name=f"tool_{i}",
                fn=_make_tool_fn(i, behavior, called),
                resource_id=resource_id,
            )
        )
    return invocations, access_set, called


def _assert_partial_failure_invariants(
    behaviors: list[str], result, called: list[int]
) -> None:
    """Shared assertions for the partial-failure property."""
    # A ToolResult for EVERY invocation, preserving order.
    assert len(result.results) == len(behaviors)
    assert [r.tool_name for r in result.results] == [f"tool_{i}" for i in range(len(behaviors))]

    for i, (behavior, res) in enumerate(zip(behaviors, result.results)):
        if behavior == "success":
            assert res.success is True
            assert res.value == f"ok-{i}"
            assert called.count(i) == 1
        else:
            assert res.success is False
            assert res.error is not None
            # Every failure carries a valid, known error type.
            assert res.error.error_type.value in ERROR_TYPES
            if behavior == "unauthorized":
                assert res.error.error_type is ToolErrorType.AUTHORIZATION
                assert i not in called
            elif behavior == "timeout":
                assert res.error.error_type is ToolErrorType.TIMEOUT
                assert i not in called
            else:  # raise
                assert res.error.error_type is ToolErrorType.EXECUTION
                assert called.count(i) == 1

    # Successes and errors partition the results (no rollback of prior work).
    assert len(result.successes) + len(result.errors) == len(result.results)
    success_indices = [i for i, b in enumerate(behaviors) if b == "success"]
    assert len(result.successes) == len(success_indices)
    assert {r.value for r in result.successes} == {f"ok-{i}" for i in success_indices}
    assert result.all_succeeded is (len(result.errors) == 0)
    assert result.any_failed is (len(result.errors) > 0)


@settings(max_examples=250, deadline=None)
@given(user_id=_user_id, behaviors=st.lists(st.sampled_from(_BEHAVIORS), min_size=2, max_size=5))
def test_property_13_partial_failures_return_all_results(
    user_id: str, behaviors: list[str]
) -> None:
    """Every invocation yields a result; successes are preserved on partial failure.

    **Validates: Requirements 7.5**
    """
    invocations, access_set, called = _build_turn(behaviors)
    result = run_tool_sequence(
        invocations, user_id=user_id, access=access_set, runner=_flag_runner
    )
    _assert_partial_failure_invariants(behaviors, result, called)


@settings(max_examples=150, deadline=None)
@given(user_id=_user_id, behaviors=st.lists(st.sampled_from(_BEHAVIORS), min_size=1, max_size=12))
def test_property_13_partial_failures_unbounded_turn(
    user_id: str, behaviors: list[str]
) -> None:
    """Same partition/no-rollback guarantees hold for larger (unbounded) turns.

    **Validates: Requirements 7.5**
    """
    invocations, access_set, called = _build_turn(behaviors)
    result = run_tool_sequence(
        invocations,
        user_id=user_id,
        access=access_set,
        runner=_flag_runner,
        enforce_turn_bounds=False,
    )
    _assert_partial_failure_invariants(behaviors, result, called)


# ===========================================================================
# Property 16: Pipeline Traceability (Requirement 11.5)
# ===========================================================================
# Feature: sample-agentic-cae-platform, Property 16: Pipeline Traceability

# Result records may be dicts or arbitrary scalars/containers.
_scalar_result = st.one_of(
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=10),
    st.booleans(),
    st.none(),
)
_dict_result = st.dictionaries(
    st.text(alphabet=_ID_ALPHABET, min_size=1, max_size=6),
    st.one_of(st.integers(), st.text(max_size=8)),
    max_size=4,
)
_result_item = st.one_of(_dict_result, _scalar_result, st.lists(st.integers(), max_size=3))

# drawing_id / extraction_id are strings (coerced) or None (-> "").
_trace_id = st.one_of(st.none(), st.text(alphabet=_ID_ALPHABET, min_size=0, max_size=15))


@settings(max_examples=250, deadline=None)
@given(
    results=st.lists(_result_item, max_size=8),
    drawing_id=_trace_id,
    extraction_id=_trace_id,
)
def test_property_16_attach_traceability_stamps_every_record(
    results: list, drawing_id: str | None, extraction_id: str | None
) -> None:
    """Every record carries the source ids (coerced); inputs are not mutated.

    **Validates: Requirements 11.5**
    """
    original = copy.deepcopy(results)

    records = attach_traceability(results, drawing_id, extraction_id)

    expected_drawing = "" if drawing_id is None else str(drawing_id)
    expected_extraction = "" if extraction_id is None else str(extraction_id)

    assert len(records) == len(results)
    for src, rec in zip(results, records):
        assert isinstance(rec, dict)
        assert rec["drawing_id"] == expected_drawing
        assert rec["extraction_id"] == expected_extraction
        # build_traceability_record agrees with attach_traceability.
        single = build_traceability_record(src, drawing_id, extraction_id)
        assert single["drawing_id"] == expected_drawing
        assert single["extraction_id"] == expected_extraction
        # Non-mapping records are wrapped under "value" so no data is lost.
        if not isinstance(src, dict):
            assert single["value"] == src

    # The input list and its elements are never mutated.
    assert results == original


# Pareto designs: dicts of parameter -> numeric value, optionally ranked.
@st.composite
def _pareto_design(draw) -> dict:
    params = draw(
        st.dictionaries(
            st.sampled_from(["fin_count", "fin_height_m", "width_m", "length_m"]),
            st.integers(min_value=1, max_value=100),
            max_size=3,
        )
    )
    if draw(st.booleans()):
        params["rank"] = draw(st.integers(min_value=0, max_value=20))
    return params


# Safe non-reserved extra keys for the optimization result envelope.
_extra_result = st.dictionaries(
    st.sampled_from(["n_evaluations", "execution_time_s", "predictor_used", "generations"]),
    st.one_of(st.integers(min_value=0, max_value=10_000), st.text(max_size=8)),
    max_size=3,
)
_nonempty_id = st.text(alphabet=_ID_ALPHABET, min_size=1, max_size=15)


@settings(max_examples=150, deadline=None)
@given(
    pareto=st.lists(_pareto_design(), max_size=10),
    drawing_id=_nonempty_id,
    extraction_id=_nonempty_id,
    n_top=st.integers(min_value=0, max_value=8),
    extra=_extra_result,
)
def test_property_16_finalize_optimization_run_is_traceable(
    pareto: list[dict],
    drawing_id: str,
    extraction_id: str,
    n_top: int,
    extra: dict,
) -> None:
    """Finalizing an optimization stamps traceability and caps auto-3D designs.

    Every Pareto design and every auto-3D design record is traceable, and the
    number of auto-3D designs never exceeds ``min(n_top, len(pareto))``.

    **Validates: Requirements 11.5**
    """
    optimization_result = {"pareto_designs": pareto, **extra}
    original = copy.deepcopy(optimization_result)

    result = finalize_optimization_run(
        optimization_result,
        source_drawing_id=drawing_id,
        extraction_id=extraction_id,
        n_top=n_top,
    )

    assert result["source_drawing_id"] == drawing_id
    assert result["extraction_id"] == extraction_id
    assert len(result["pareto_designs"]) == len(pareto)

    for rec in result["pareto_designs"]:
        assert rec["drawing_id"] == drawing_id
        assert rec["extraction_id"] == extraction_id

    auto_3d = result["auto_3d_designs"]
    assert len(auto_3d) <= min(n_top, len(pareto))
    for rec in auto_3d:
        assert rec["drawing_id"] == drawing_id
        assert rec["extraction_id"] == extraction_id

    # The input result dict is not mutated by the pure finalizer.
    assert optimization_result == original


# ===========================================================================
# Property 17: Pipeline Failure Notification Completeness (Requirement 11.6)
# ===========================================================================
# Feature: sample-agentic-cae-platform, Property 17: Pipeline Failure Notification Completeness

_stage = st.one_of(
    st.sampled_from(list(PipelineStage)),
    st.sampled_from([s.value for s in PipelineStage]),
    st.sampled_from([s.name for s in PipelineStage]),
    st.text(max_size=12),  # unknown raw strings
    st.none(),
)

_recovery_override = st.one_of(
    st.none(),
    st.just([]),
    st.lists(st.sampled_from(list(RecoveryAction)), max_size=4),
    st.lists(st.text(max_size=8), max_size=4),  # may include blanks/dupes
    st.sampled_from(list(RecoveryAction)),
    st.text(max_size=8),
    st.integers(),  # non-usable override type -> defaults kick in
)

_error_description = st.one_of(st.none(), st.just(""), st.just("   "), st.text(max_size=40))


@settings(max_examples=300, deadline=None)
@given(
    stage=_stage,
    error_description=_error_description,
    recovery_actions=_recovery_override,
    error_category=st.one_of(st.none(), st.text(max_size=8)),
    drawing_id=st.one_of(st.none(), _nonempty_id),
    extraction_id=st.one_of(st.none(), _nonempty_id),
)
def test_property_17_notification_is_always_complete(
    stage,
    error_description,
    recovery_actions,
    error_category,
    drawing_id,
    extraction_id,
) -> None:
    """Any stage/description/override yields a complete failure notification.

    The notification always has a non-empty stage name, a non-empty
    description, and a recovery-actions list with at least one non-empty
    string.

    **Validates: Requirements 11.6**
    """
    notification = build_failure_notification(
        stage,
        error_description,
        recovery_actions,
        error_category=error_category,
        drawing_id=drawing_id,
        extraction_id=extraction_id,
    )

    stage_name = notification["stage"]
    assert isinstance(stage_name, str) and stage_name.strip()

    description = notification["description"]
    assert isinstance(description, str) and description.strip()

    actions = notification["recovery_actions"]
    assert isinstance(actions, list)
    assert len(actions) >= 1
    for action in actions:
        assert isinstance(action, str) and action.strip()


@settings(max_examples=150, deadline=None)
@given(
    stage=st.sampled_from(list(PipelineStage)),
    overrides=st.lists(st.sampled_from(list(RecoveryAction)), min_size=1, max_size=4, unique=True),
    error_description=st.text(min_size=1, max_size=40).filter(lambda s: s.strip()),
)
def test_property_17_usable_recovery_override_is_honored(
    stage: PipelineStage, overrides: list[RecoveryAction], error_description: str
) -> None:
    """When usable recovery actions are supplied, they populate the notification.

    **Validates: Requirements 11.6**
    """
    notification = build_failure_notification(stage, error_description, overrides)
    actions = notification["recovery_actions"]
    assert actions == [action.value for action in overrides]
