"""Property-based tests for the Optimization Agent runtime helpers.

Covers two correctness properties from the design document (design section
"5. Optimization Agent (AgentCore Runtime)"):

- **Property 8: Re-optimization Preserves Prior Constraints** — For any
  follow-up optimization request within an active session,
  ``merge_reoptimization_request`` SHALL preserve all previously extracted
  constraints, the evaluation budget, population size, design space, and prior
  Pareto results while applying the updated objectives (either supplied
  explicitly or inferred from the follow-up instruction), flagging the request
  as a re-optimization.
  **Validates: Requirements 5.5**

- **Property 9: Optimization Report Completeness** — For any completed
  optimization, ``generate_optimization_report`` SHALL produce a Markdown
  report containing: optimization objectives, extracted constraints WITH the
  source drawing reference, population size, generation count, convergence
  metrics, and the final Pareto-optimal design set with objective values.
  **Validates: Requirements 5.6**

The generators emit random-but-structurally-valid prior optimization states
(Property 8) and optimization-result dicts (Property 9). Expected values (the
preserved prior context, the resolved source drawing reference) are recomputed
independently of the helper internals so the tests do not merely restate the
implementation.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from agents.optimizer.main import (
    CATEGORY_DIMENSIONAL,
    CATEGORY_MATERIAL,
    CATEGORY_TOLERANCE,
    generate_optimization_report,
    merge_reoptimization_request,
)

# ===========================================================================
# Shared strategies
# ===========================================================================

# Objective fields the platform optimizes over.
_OBJECTIVE_FIELDS = [
    "heat_dissipation_W",
    "weight_kg",
    "volume_m3",
    "thermal_resistance_KpW",
    "surface_temperature_C",
]
_SENSES = ["maximize", "minimize"]

# Design-space parameter names.
_PARAM_NAMES = [
    "fin_count",
    "fin_height_m",
    "width_m",
    "length_m",
    "fin_thickness_m",
    "base_thickness_m",
]

# Simple, verbatim-round-trippable drawing identifiers (no markdown-sensitive
# characters), so a resolved reference can be located in the rendered report.
_ID_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
_drawing_id = st.text(alphabet=_ID_ALPHABET, min_size=1, max_size=20)

_numbers = st.floats(min_value=-1e4, max_value=1e4, allow_nan=False, allow_infinity=False)


@st.composite
def _objective_list(draw, *, min_size: int = 1, max_size: int = 3) -> list[str]:
    """Draw a list of ``sense:field`` objectives with unique fields."""
    fields = draw(
        st.lists(
            st.sampled_from(_OBJECTIVE_FIELDS),
            min_size=min_size,
            max_size=max_size,
            unique=True,
        )
    )
    return [f"{draw(st.sampled_from(_SENSES))}:{field}" for field in fields]


@st.composite
def _design_space(draw) -> dict:
    """Draw a design-space dict (param -> {min, max, type})."""
    names = draw(
        st.lists(st.sampled_from(_PARAM_NAMES), min_size=1, max_size=3, unique=True)
    )
    space: dict[str, dict] = {}
    for name in names:
        lo = draw(st.integers(min_value=1, max_value=50))
        hi = lo + draw(st.integers(min_value=1, max_value=50))
        space[name] = {"min": lo, "max": hi, "type": draw(st.sampled_from(["int", "float"]))}
    return space


@st.composite
def _constraints_dict(draw) -> dict:
    """Draw a constraints dict (name -> {field, type, limit})."""
    names = draw(
        st.lists(
            st.sampled_from(["c_temp", "c_weight", "c_volume", "c_resistance"]),
            max_size=3,
            unique=True,
        )
    )
    constraints: dict[str, dict] = {}
    for name in names:
        constraints[name] = {
            "field": draw(st.sampled_from(_OBJECTIVE_FIELDS)),
            "type": draw(st.sampled_from(["max", "min", "eq"])),
            "limit": draw(st.floats(min_value=0.1, max_value=500.0, allow_nan=False)),
        }
    return constraints


def _param_map() -> st.SearchStrategy:
    """A small dict of design parameters to numeric values."""
    return st.dictionaries(
        st.sampled_from(_PARAM_NAMES), st.integers(min_value=1, max_value=100), max_size=4
    )


# ===========================================================================
# Property 8: Re-optimization Preserves Prior Constraints
# ===========================================================================


@st.composite
def _prior_state(draw) -> dict:
    """Draw a prior optimization state loaded from AgentCore Memory."""
    n_pareto = draw(st.integers(min_value=0, max_value=3))
    pareto_designs = [draw(_param_map()) for _ in range(n_pareto)]
    pareto_objectives = [
        draw(st.dictionaries(st.sampled_from(_OBJECTIVE_FIELDS), _numbers, max_size=3))
        for _ in range(n_pareto)
    ]
    pareto_constraints = [
        draw(st.dictionaries(st.sampled_from(["c_a", "c_b"]), st.booleans(), max_size=2))
        for _ in range(n_pareto)
    ]
    return {
        "objectives": draw(_objective_list()),
        "constraints": draw(_constraints_dict()),
        "design_space": draw(_design_space()),
        "budget": draw(st.integers(min_value=100, max_value=10_000)),
        "population_size": draw(st.integers(min_value=10, max_value=200)),
        "model_type": draw(st.sampled_from(["thermal_analytical", "surrogate"])),
        "source_drawing_id": draw(_drawing_id),
        "extraction_id": draw(_drawing_id),
        "pareto_designs": pareto_designs,
        "pareto_objectives": pareto_objectives,
        "pareto_constraints": pareto_constraints,
    }


# Known follow-up phrases mapped to the objective they should induce. These
# mirror the phrase table in ``agents/optimizer/main.py`` but are asserted
# independently here.
_INSTRUCTION_OBJECTIVES: list[tuple[str, str]] = [
    ("make it lighter", "minimize:weight_kg"),
    ("can you reduce weight?", "minimize:weight_kg"),
    ("make it lightweight", "minimize:weight_kg"),
    ("make it smaller", "minimize:volume_m3"),
    ("a more compact design", "minimize:volume_m3"),
    ("give me better cooling", "maximize:heat_dissipation_W"),
    ("it should dissipate more heat", "maximize:heat_dissipation_W"),
    ("lower thermal resistance", "minimize:thermal_resistance_KpW"),
]


@settings(max_examples=200, deadline=None)
@given(
    prior=_prior_state(),
    use_explicit=st.booleans(),
    new_objectives=_objective_list(),
    instruction_pair=st.sampled_from(_INSTRUCTION_OBJECTIVES),
    plain_instruction=st.text(max_size=12),
)
def test_property_8_reoptimization_preserves_prior_constraints(
    prior: dict,
    use_explicit: bool,
    new_objectives: list[str],
    instruction_pair: tuple[str, str],
    plain_instruction: str,
) -> None:
    """A follow-up preserves prior context while applying the updated objectives.

    **Validates: Requirements 5.5**
    """
    if use_explicit:
        # Explicit updated objectives supplied by the caller.
        request = merge_reoptimization_request(prior, new_objectives=new_objectives)
        assert request["objectives"] == [str(o) for o in new_objectives]
    else:
        # Objectives inferred from a natural-language follow-up instruction.
        instruction, expected_objective = instruction_pair
        request = merge_reoptimization_request(prior, follow_up_instruction=instruction)
        # The update is reflected: the inferred objective is present.
        assert expected_objective in request["objectives"]
        assert request["follow_up_instruction"] == instruction

    # --- Prior constraints preserved verbatim (the core of Property 8) --------
    assert request["constraints"] == prior["constraints"]

    # --- Evaluation budget, population size, and design space preserved -------
    assert request["budget"] == prior["budget"]
    assert request["population_size"] == prior["population_size"]
    assert request["design_space"] == prior["design_space"]

    # --- Prior Pareto results preserved for conversational context ------------
    assert request["prior_pareto_designs"] == prior["pareto_designs"]
    assert request["prior_pareto_objectives"] == prior["pareto_objectives"]
    assert request["prior_pareto_constraints"] == prior["pareto_constraints"]

    # --- Traceability preserved -----------------------------------------------
    assert request["source_drawing_id"] == prior["source_drawing_id"]
    assert request["extraction_id"] == prior["extraction_id"]

    # --- The request is flagged as a re-optimization --------------------------
    assert request["is_reoptimization"] is True


@settings(max_examples=100, deadline=None)
@given(prior=_prior_state(), plain_instruction=st.text(max_size=12))
def test_property_8_unrecognized_instruction_keeps_prior_objectives(
    prior: dict, plain_instruction: str
) -> None:
    """When no explicit objectives and no phrase match, prior objectives persist.

    Constraint/budget/Pareto preservation still holds — the follow-up only ever
    updates the objectives, never the preserved context.

    **Validates: Requirements 5.5**
    """
    # A plain instruction with no recognized optimization phrase.
    request = merge_reoptimization_request(prior, follow_up_instruction=plain_instruction)

    # Prior objectives retained when nothing new is inferred.
    assert request["objectives"] == prior["objectives"]
    # Preservation invariants unchanged.
    assert request["constraints"] == prior["constraints"]
    assert request["budget"] == prior["budget"]
    assert request["population_size"] == prior["population_size"]
    assert request["prior_pareto_designs"] == prior["pareto_designs"]
    assert request["is_reoptimization"] is True


# ===========================================================================
# Property 9: Optimization Report Completeness
# ===========================================================================


@st.composite
def _report_case(draw) -> tuple[list[str], dict, dict, str | None, str]:
    """Draw (objectives, optimization_result, constraints, source_override, source).

    ``source`` is the source drawing reference the report is *expected* to
    surface, computed independently using the documented precedence:
    explicit override > result.source_drawing_id > constraints.drawing_id.
    """
    objectives = draw(_objective_list())

    # Pareto set: designs, objective values keyed by the SAME objective keys so
    # the report renders objective values for each design.
    n_pareto = draw(st.integers(min_value=0, max_value=4))
    pareto_designs = [draw(_param_map()) for _ in range(n_pareto)]
    pareto_objectives = [
        {obj: draw(_numbers) for obj in objectives} for _ in range(n_pareto)
    ]
    pareto_constraints = [
        draw(st.dictionaries(st.sampled_from(["c_a", "c_b"]), st.booleans(), max_size=2))
        for _ in range(n_pareto)
    ]

    # Convergence history entries.
    n_gen = draw(st.integers(min_value=0, max_value=4))
    convergence_history = [
        {
            "generation": g + 1,
            "n_evaluations": (g + 1) * 10,
            "best_objectives": {obj: draw(_numbers) for obj in objectives},
        }
        for g in range(n_gen)
    ]

    population_size = draw(st.integers(min_value=10, max_value=200))
    generations = draw(st.integers(min_value=1, max_value=50))

    # Source drawing reference can live in the result and/or constraints, and
    # may be overridden by an explicit argument.
    result_source = draw(st.one_of(st.none(), _drawing_id))
    constraint_source = draw(st.one_of(st.none(), _drawing_id))
    source_override = draw(st.one_of(st.none(), _drawing_id))

    optimization_result: dict = {
        "pareto_designs": pareto_designs,
        "pareto_objectives": pareto_objectives,
        "pareto_constraints": pareto_constraints,
        "population_size": population_size,
        "generations": generations,
        "budget": draw(st.integers(min_value=100, max_value=10_000)),
        "n_evaluations": draw(st.integers(min_value=0, max_value=10_000)),
        "convergence_history": convergence_history,
        "predictor_used": draw(st.sampled_from(["bar_cohen_analytical", "sagemaker"])),
        "extraction_id": draw(_drawing_id),
    }
    if result_source is not None:
        optimization_result["source_drawing_id"] = result_source

    constraints: dict = {
        CATEGORY_DIMENSIONAL: [],
        CATEGORY_MATERIAL: [],
        CATEGORY_TOLERANCE: [],
    }
    if constraint_source is not None:
        constraints["drawing_id"] = constraint_source

    # Independently resolve the expected source per documented precedence.
    if source_override:
        source = source_override
    elif result_source:
        source = result_source
    elif constraint_source:
        source = constraint_source
    else:
        source = "unknown"

    return objectives, optimization_result, constraints, source_override, source


@settings(max_examples=200, deadline=None)
@given(case=_report_case())
def test_property_9_report_contains_all_required_sections(case: tuple) -> None:
    """The Markdown report contains every section required by Property 9.

    **Validates: Requirements 5.6**
    """
    objectives, optimization_result, constraints, source_override, expected_source = case

    report = generate_optimization_report(
        objectives=objectives,
        optimization_result=optimization_result,
        constraints=constraints,
        source_drawing_id=source_override,
    )

    # --- All required sections are present ------------------------------------
    assert "## Optimization Objectives" in report
    assert "## Extracted Constraints" in report
    assert "Population size" in report
    assert "Generation count" in report
    assert "## Convergence Metrics" in report
    assert "## Pareto-Optimal Designs" in report

    # --- Population size and generation count VALUES are surfaced --------------
    assert str(optimization_result["population_size"]) in report
    assert str(optimization_result["generations"]) in report

    # --- Objectives are rendered (field names appear) -------------------------
    for objective in objectives:
        field = objective.partition(":")[2]
        assert field in report

    # --- Source drawing reference appears, and within the constraints section -
    assert expected_source in report
    constraints_section = report.split("## Extracted Constraints", 1)[1].split("##", 1)[0]
    assert expected_source in constraints_section

    # --- Pareto set with objective values -------------------------------------
    pareto_section = report.split("## Pareto-Optimal Designs", 1)[1]
    pareto_designs = optimization_result["pareto_designs"]
    if pareto_designs:
        assert "#### Design 1" in pareto_section
        # Each design's objective values are rendered by objective key.
        for objective in objectives:
            assert objective in pareto_section
    else:
        assert "_No Pareto-optimal designs were produced._" in pareto_section


@settings(max_examples=50, deadline=None)
@given(objectives=_objective_list(), source=_drawing_id)
def test_property_9_source_override_takes_precedence(
    objectives: list[str], source: str
) -> None:
    """An explicit source override is always the reference surfaced in the report.

    **Validates: Requirements 5.6**
    """
    report = generate_optimization_report(
        objectives=objectives,
        optimization_result={
            "source_drawing_id": "RESULT-ID",
            "population_size": 100,
            "generations": 10,
        },
        constraints={"drawing_id": "CONSTRAINT-ID"},
        source_drawing_id=source,
    )
    assert source in report
    constraints_section = report.split("## Extracted Constraints", 1)[1].split("##", 1)[0]
    assert source in constraints_section
