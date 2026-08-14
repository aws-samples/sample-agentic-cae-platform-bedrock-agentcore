"""Unit tests for the Optimization Agent Runtime.

Covers the pure, SDK-free optimization workflow helpers (design section
"5. Optimization Agent (AgentCore Runtime)", Requirement 5):

* ``generate_optimization_report`` — the Markdown report contains every
  required section including the source drawing reference (Req 5.6 / Property 9).
* ``handle_missing_constraints`` — non-empty ``missing_categories`` blocks
  optimization and the notification names the missing categories (Req 5.7).
* ``merge_reoptimization_request`` — a follow-up preserves prior constraints,
  budget, and Pareto results while applying updated objectives (Req 5.5 /
  Property 8).
* ``build_progress_event`` — streaming progress carries generation, hypervolume
  and ETA.
* The module (and helpers) import cleanly without ``strands-agents``.
* ``agentcore.json`` matches the design (name, timeouts, model, tools, memory).

Validates: Requirements 5.1, 5.2, 5.4, 5.5, 5.6, 5.7
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agents.optimizer import main as opt
from agents.optimizer.main import (
    CATEGORY_DIMENSIONAL,
    CATEGORY_MATERIAL,
    CATEGORY_TOLERANCE,
    GATEWAY,
    MEMORY,
    MODEL_ID,
    OPTIMIZER_TOOLS,
    _run_optimizer_entrypoint,
    build_progress_event,
    generate_optimization_report,
    handle_missing_constraints,
    infer_objectives_from_instruction,
    merge_reoptimization_request,
    run_optimization_workflow,
)

_AGENTCORE_JSON = (
    Path(__file__).resolve().parents[2] / "agents" / "optimizer" / "agentcore.json"
)


# --- Fixtures ----------------------------------------------------------------


@pytest.fixture
def sample_constraints() -> dict:
    """A validate_constraints-style result with all three categories present."""
    return {
        "status": "ok",
        "constraints": {
            "drawing_id": "DRW-42",
            "dimensional_bounds": [
                {
                    "feature": "base_width",
                    "lower_bound": 49.9,
                    "upper_bound": 50.1,
                    "unit": "mm",
                }
            ],
            "material_limits": [
                {"raw": "Aluminum 6061-T6", "normalized": "aluminum_6061", "known": True}
            ],
            "tolerance_ranges": [
                {"label": "ISO 2768-m", "lower": -0.1, "upper": 0.1, "unit": "mm"}
            ],
            "present_categories": [
                CATEGORY_DIMENSIONAL,
                CATEGORY_MATERIAL,
                CATEGORY_TOLERANCE,
            ],
            "missing_categories": [],
        },
    }


@pytest.fixture
def sample_result() -> dict:
    """An OptimizationResult-style dict from run_optimization."""
    return {
        "pareto_designs": [
            {"fin_count": 12, "fin_height_m": 0.03},
            {"fin_count": 8, "fin_height_m": 0.02},
        ],
        "pareto_objectives": [
            {"maximize:heat_dissipation_W": 42.5, "minimize:weight_kg": 0.31},
            {"maximize:heat_dissipation_W": 35.1, "minimize:weight_kg": 0.22},
        ],
        "pareto_constraints": [
            {"max_weight": True},
            {"max_weight": True},
        ],
        "n_evaluations": 1000,
        "budget": 1000,
        "population_size": 100,
        "generations": 10,
        "predictor_used": "bar_cohen_analytical",
        "convergence_history": [
            {
                "generation": 1,
                "n_evaluations": 100,
                "best_objectives": {"maximize:heat_dissipation_W": 30.0},
            },
            {
                "generation": 10,
                "n_evaluations": 1000,
                "best_objectives": {"maximize:heat_dissipation_W": 42.5},
            },
        ],
        "source_drawing_id": "DRW-42",
        "extraction_id": "EXT-7",
        "notifications": [],
    }


# --- Report generation (Requirement 5.6 / Property 9) ------------------------


def test_report_contains_all_required_sections(sample_constraints, sample_result) -> None:
    report = generate_optimization_report(
        objectives=["maximize:heat_dissipation_W", "minimize:weight_kg"],
        optimization_result=sample_result,
        constraints=sample_constraints,
    )
    # Property 9 required sections.
    assert "## Optimization Objectives" in report
    assert "## Extracted Constraints" in report
    assert "Population size" in report
    assert "Generation count" in report
    assert "## Convergence Metrics" in report
    assert "## Pareto-Optimal Designs" in report


def test_report_includes_source_drawing_reference(sample_constraints, sample_result) -> None:
    report = generate_optimization_report(
        objectives=["minimize:weight_kg"],
        optimization_result=sample_result,
        constraints=sample_constraints,
    )
    # Source drawing reference must appear (Req 5.6) — and specifically within
    # the extracted-constraints section.
    assert "DRW-42" in report
    constraints_section = report.split("## Extracted Constraints", 1)[1]
    assert "DRW-42" in constraints_section.split("##", 1)[0]


def test_report_includes_objective_and_config_values(sample_constraints, sample_result) -> None:
    report = generate_optimization_report(
        objectives=["maximize:heat_dissipation_W", "minimize:weight_kg"],
        optimization_result=sample_result,
        constraints=sample_constraints,
    )
    # Objectives rendered.
    assert "heat_dissipation_W" in report
    assert "weight_kg" in report
    # Population size and generation count values present.
    assert "100" in report
    assert "10" in report
    # Pareto objective values present.
    assert "42.5" in report


def test_report_source_drawing_override_wins(sample_result) -> None:
    report = generate_optimization_report(
        objectives=["minimize:weight_kg"],
        optimization_result=sample_result,
        constraints={"drawing_id": "DRW-42"},
        source_drawing_id="OVERRIDE-99",
    )
    assert "OVERRIDE-99" in report


def test_report_handles_empty_result_gracefully() -> None:
    report = generate_optimization_report(
        objectives=[],
        optimization_result={},
        constraints={},
    )
    # Still produces every required section with placeholder content.
    assert "## Optimization Objectives" in report
    assert "## Extracted Constraints" in report
    assert "## Pareto-Optimal Designs" in report
    assert "unknown" in report  # source drawing reference fallback


def test_report_surfaces_time_budget_notification(sample_constraints, sample_result) -> None:
    sample_result["notifications"] = ["Time budget of 180s exceeded; returning best set."]
    report = generate_optimization_report(
        objectives=["minimize:weight_kg"],
        optimization_result=sample_result,
        constraints=sample_constraints,
    )
    assert "## Notifications" in report
    assert "Time budget" in report


# --- Missing-constraints handler (Requirement 5.7) ---------------------------


def test_missing_constraints_blocks_and_names_categories() -> None:
    result = {
        "constraints": {
            "drawing_id": "DRW-9",
            "missing_categories": [CATEGORY_MATERIAL, CATEGORY_TOLERANCE],
        }
    }
    notification = handle_missing_constraints(result)
    assert notification.can_proceed is False
    assert set(notification.missing_categories) == {CATEGORY_MATERIAL, CATEGORY_TOLERANCE}
    # Human-readable labels naming the missing categories.
    assert "material limits" in notification.message
    assert "tolerance ranges" in notification.message
    # Prompts for manual input and references the drawing.
    assert "manually" in notification.message.lower()
    assert "DRW-9" in notification.message


def test_missing_constraints_single_category_message() -> None:
    notification = handle_missing_constraints(
        {"constraints": {"missing_categories": [CATEGORY_DIMENSIONAL]}}
    )
    assert notification.can_proceed is False
    assert notification.missing_labels == ["dimensional bounds"]
    assert "dimensional bounds" in notification.message
    assert "cannot proceed" in notification.message.lower()


def test_no_missing_constraints_allows_optimization(sample_constraints) -> None:
    notification = handle_missing_constraints(sample_constraints)
    assert notification.can_proceed is True
    assert notification.missing_categories == []
    assert notification.message == ""


def test_missing_constraints_accepts_bare_constraints_dict() -> None:
    notification = handle_missing_constraints(
        {"missing_categories": [CATEGORY_MATERIAL], "drawing_id": "DRW-3"}
    )
    assert notification.can_proceed is False
    assert "material limits" in notification.message


def test_missing_constraints_handles_non_dict_input() -> None:
    notification = handle_missing_constraints(None)
    # No categories reported → nothing to block on (defensive default).
    assert notification.can_proceed is True


# --- Re-optimization preserves prior constraints (Requirement 5.5) -----------


@pytest.fixture
def prior_state() -> dict:
    return {
        "objectives": ["maximize:heat_dissipation_W"],
        "constraints": {"max_weight": {"field": "weight_kg", "type": "max", "limit": 0.5}},
        "design_space": {"fin_count": {"min": 4, "max": 20, "type": "int"}},
        "budget": 2000,
        "population_size": 80,
        "model_type": "thermal_analytical",
        "source_drawing_id": "DRW-42",
        "extraction_id": "EXT-7",
        "pareto_designs": [{"fin_count": 12}],
        "pareto_objectives": [{"maximize:heat_dissipation_W": 40.0}],
        "pareto_constraints": [{"max_weight": True}],
    }


def test_reoptimization_preserves_constraints_and_applies_new_objectives(prior_state) -> None:
    request = merge_reoptimization_request(
        prior_state, new_objectives=["minimize:weight_kg"]
    )
    # Updated objectives applied.
    assert request["objectives"] == ["minimize:weight_kg"]
    # Prior constraints, budget, population, and design space preserved.
    assert request["constraints"] == prior_state["constraints"]
    assert request["budget"] == 2000
    assert request["population_size"] == 80
    assert request["design_space"] == prior_state["design_space"]
    # Prior Pareto results preserved for conversation context.
    assert request["prior_pareto_designs"] == [{"fin_count": 12}]
    assert request["prior_pareto_objectives"] == [{"maximize:heat_dissipation_W": 40.0}]
    # Traceability preserved.
    assert request["source_drawing_id"] == "DRW-42"
    assert request["extraction_id"] == "EXT-7"
    assert request["is_reoptimization"] is True


def test_reoptimization_infers_objectives_from_follow_up(prior_state) -> None:
    request = merge_reoptimization_request(
        prior_state, follow_up_instruction="can you make it lighter?"
    )
    # "make it lighter" -> minimize weight while preserving the heat objective.
    assert "minimize:weight_kg" in request["objectives"]
    assert "maximize:heat_dissipation_W" in request["objectives"]
    # Constraints and budget still preserved.
    assert request["constraints"] == prior_state["constraints"]
    assert request["budget"] == 2000


def test_reoptimization_without_new_objectives_keeps_prior(prior_state) -> None:
    request = merge_reoptimization_request(prior_state, follow_up_instruction="rerun please")
    assert request["objectives"] == ["maximize:heat_dissipation_W"]
    assert request["constraints"] == prior_state["constraints"]


def test_reoptimization_reads_nested_result_envelope() -> None:
    prior = {
        "objectives": ["maximize:heat_dissipation_W"],
        "constraints": {"c": 1},
        "budget": 1500,
        "optimization_result": {
            "pareto_designs": [{"x": 1}],
            "pareto_objectives": [{"o": 2.0}],
        },
    }
    request = merge_reoptimization_request(prior, new_objectives=["minimize:weight_kg"])
    assert request["prior_pareto_designs"] == [{"x": 1}]
    assert request["prior_pareto_objectives"] == [{"o": 2.0}]
    assert request["budget"] == 1500


def test_infer_objectives_flips_conflicting_field() -> None:
    # "make it lighter" should replace a prior maximize:weight_kg objective.
    merged = infer_objectives_from_instruction(
        "make it lighter", ["maximize:weight_kg", "maximize:heat_dissipation_W"]
    )
    assert "minimize:weight_kg" in merged
    assert "maximize:weight_kg" not in merged
    assert "maximize:heat_dissipation_W" in merged


def test_infer_objectives_no_match_returns_prior() -> None:
    prior = ["maximize:heat_dissipation_W"]
    assert infer_objectives_from_instruction("do something", prior) == prior


# --- Streaming progress events ----------------------------------------------


def test_progress_event_contains_generation_hypervolume_and_eta() -> None:
    event = build_progress_event(
        generation=5,
        total_generations=10,
        hypervolume=0.82,
        elapsed_seconds=30.0,
        n_evaluations=500,
        budget=1000,
    )
    assert event["type"] == "optimization_progress"
    assert event["generation"] == 5
    assert event["total_generations"] == 10
    assert event["hypervolume"] == 0.82
    assert event["progress_pct"] == 50.0
    # Halfway through in 30s → ETA ~30s remaining.
    assert event["eta_seconds"] == pytest.approx(30.0, rel=1e-6)
    assert event["n_evaluations"] == 500
    assert event["budget"] == 1000


def test_progress_event_eta_none_at_generation_zero() -> None:
    event = build_progress_event(
        generation=0, total_generations=10, hypervolume=0.0, elapsed_seconds=0.0
    )
    assert event["eta_seconds"] is None
    assert event["progress_pct"] == 0.0


def test_progress_event_clamps_completed_generation() -> None:
    event = build_progress_event(
        generation=15, total_generations=10, hypervolume=1.0, elapsed_seconds=60.0
    )
    assert event["progress_pct"] == 100.0
    assert event["eta_seconds"] == 0.0


def test_progress_event_is_json_serializable() -> None:
    event = build_progress_event(
        generation=3, total_generations=10, hypervolume=0.5, elapsed_seconds=12.0
    )
    # Must round-trip through JSON for WebSocket streaming.
    assert json.loads(json.dumps(event)) == event


# --- Module imports cleanly without the Strands SDK -------------------------


def test_module_imports_without_strands_sdk() -> None:
    assert opt._STRANDS_AVAILABLE in (True, False)
    assert callable(opt.generate_optimization_report)
    assert callable(opt.handle_missing_constraints)
    assert callable(opt.merge_reoptimization_request)
    assert callable(opt.build_progress_event)


def test_build_agent_requires_sdk() -> None:
    if opt._STRANDS_AVAILABLE:
        pytest.skip("strands-agents is installed; SDK-absent path not exercised")
    with pytest.raises(RuntimeError, match="strands-agents"):
        opt.build_agent()


def test_optimizer_tools_are_the_two_gateway_tools() -> None:
    assert set(OPTIMIZER_TOOLS) == {"validate_constraints", "run_optimization"}


# --- agentcore.json contents (design section 5) -----------------------------


@pytest.fixture(scope="module")
def agentcore_config() -> dict:
    assert _AGENTCORE_JSON.exists(), f"missing agentcore.json at {_AGENTCORE_JSON}"
    return json.loads(_AGENTCORE_JSON.read_text())


def test_agentcore_runtime_settings(agentcore_config: dict) -> None:
    runtime = agentcore_config["runtime"]
    assert runtime["name"] == "cae-platform-optimizer"
    assert runtime["entryPoint"] == "main.py"
    assert runtime["codeRuntime"] == "PYTHON_3_13"
    assert runtime["protocol"] == "HTTP"
    assert runtime["networkMode"] == "PUBLIC"
    assert runtime["idleTimeout"] == 900
    assert runtime["maxLifetime"] == 28800


def test_agentcore_agent_settings(agentcore_config: dict) -> None:
    agent = agentcore_config["agent"]
    # Claude Sonnet 4 (design section 5), cross-Region inference profile id.
    assert agent["model"] == "us.anthropic.claude-sonnet-4-20250514-v1:0"
    assert agent["model"] == MODEL_ID
    assert agent["tools"] == GATEWAY == "agentcore-gateway://cae-platform-gateway"
    assert agent["memory"] == MEMORY == "agentcore-memory://cae-platform-memory"


# --- Real optimization workflow (run_optimization_workflow) -----------------
#
# Mocks the two external seams (`shared.gateway.call_gateway_tool` and
# `shared.memory.{recent_turns,record_turn}`, plus S3 for Ground_Truth_JSON
# loading) so the real orchestration is exercised without network/AWS
# credentials.


def _ground_truth_with_constraints() -> dict:
    return {
        "drawing_id": "DRW-1",
        "title_block": {"material": "Aluminum 6061", "general_tolerance": "ISO 2768-m"},
        "dimensions": [{"value": 10.0, "tolerance": "+/-0.1", "type": "linear", "feature": "width"}],
        "gdt_callouts": [],
        "bom": [],
        "notes": [],
        "revisions": [],
    }


def _optimization_tool_result() -> dict:
    return {
        "pareto_designs": [{"width": 9.5}],
        "pareto_objectives": [{"weight_kg": 1.2}],
        "pareto_constraints": [{"width_ok": True}],
        "population_size": 100,
        "generations": 10,
        "budget": 1000,
        "n_evaluations": 1000,
        "convergence_history": [],
        "predictor_used": "thermal_analytical",
    }


def test_run_optimization_workflow_fresh_run_calls_tool_and_persists_state() -> None:
    with patch("agents.optimizer.main._load_ground_truth", return_value=_ground_truth_with_constraints()), \
         patch("shared.memory.recent_turns", return_value=[]), \
         patch("shared.memory.record_turn") as mock_save, \
         patch("shared.gateway.call_gateway_tool", return_value=_optimization_tool_result()) as mock_tool:
        result = run_optimization_workflow(
            {"drawing_id": "DRW-1", "objectives": ["minimize:weight_kg"]},
            actor_id="u1",
            session_id="s1",
        )

    assert result["status"] == "ok"
    assert "report_markdown" in result
    assert result["result"]["source_drawing_id"] == "DRW-1"
    mock_tool.assert_called_once()
    mock_save.assert_called_once()
    # The persisted state round-trips through JSON and marks itself so a later
    # follow-up recognizes it as prior optimizer state.
    saved_state = json.loads(mock_save.call_args.args[3])
    assert saved_state["_optimizer_state"] is True


def test_run_optimization_workflow_blocks_on_missing_constraints() -> None:
    empty_ground_truth = {
        "drawing_id": "DRW-2",
        "title_block": {},
        "dimensions": [],
        "gdt_callouts": [],
        "bom": [],
        "notes": [],
        "revisions": [],
    }
    with patch("agents.optimizer.main._load_ground_truth", return_value=empty_ground_truth), \
         patch("shared.memory.recent_turns", return_value=[]), \
         patch("shared.gateway.call_gateway_tool") as mock_tool:
        result = run_optimization_workflow({"drawing_id": "DRW-2"}, actor_id="u1", session_id="s1")

    assert result["status"] == "needs_manual_input"
    assert result["missing_categories"]
    mock_tool.assert_not_called()


def test_run_optimization_workflow_follow_up_merges_prior_state() -> None:
    prior_state = {
        "_optimizer_state": True,
        "objectives": ["maximize:weight_kg"],
        "constraints": {CATEGORY_DIMENSIONAL: [{"feature": "width", "lower_bound": 5, "upper_bound": 15}]},
        "design_space": {"width": {"min": 5, "max": 15}},
        "budget": 500,
        "population_size": 50,
        "model_type": "thermal_analytical",
        "source_drawing_id": "DRW-1",
        "extraction_id": "EX-1",
        "optimization_result": _optimization_tool_result(),
    }
    with patch("agents.optimizer.main._load_prior_state", return_value=prior_state), \
         patch("shared.memory.record_turn") as mock_save, \
         patch("shared.gateway.call_gateway_tool", return_value=_optimization_tool_result()) as mock_tool:
        result = run_optimization_workflow(
            {"instruction": "make it lighter", "session_id": "s1"}, actor_id="u1", session_id="s1"
        )

    assert result["status"] == "ok"
    # "make it lighter" flips the prior maximize objective to minimize.
    assert result["objectives"] == ["minimize:weight_kg"]
    call_args = mock_tool.call_args.args
    assert call_args[1]["budget"] == 500  # preserved from prior state
    mock_save.assert_called_once()


def test_run_optimization_workflow_gateway_failure_returns_error() -> None:
    from shared.gateway import GatewayToolError

    with patch("agents.optimizer.main._load_ground_truth", return_value=_ground_truth_with_constraints()), \
         patch("shared.memory.recent_turns", return_value=[]), \
         patch(
             "shared.gateway.call_gateway_tool",
             side_effect=GatewayToolError("down", tool_name="run_optimization"),
         ):
        result = run_optimization_workflow(
            {"drawing_id": "DRW-1", "objectives": ["minimize:weight_kg"]},
            actor_id="u1",
            session_id="s1",
        )

    assert result["status"] == "error"


def test_entrypoint_wraps_workflow_exceptions() -> None:
    with patch("agents.optimizer.main.run_optimization_workflow", side_effect=RuntimeError("boom")):
        response = _run_optimizer_entrypoint({"drawing_id": "DRW-1"})
    assert response["status"] == "error"


def test_entrypoint_success_passthrough() -> None:
    with patch(
        "agents.optimizer.main.run_optimization_workflow",
        return_value={"status": "ok", "objectives": [], "result": {}, "report_markdown": ""},
    ):
        response = _run_optimizer_entrypoint({"drawing_id": "DRW-1", "actor_id": "u1", "session_id": "s1"})
    assert response["status"] == "ok"
