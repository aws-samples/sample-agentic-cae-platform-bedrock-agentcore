"""Unit tests for the ``run_optimization`` optimizer Lambda handler.

Covers (design "5. Optimization Agent", Requirements 5.2, 5.3, 5.4, 5.8):

* A valid config returns a valid Pareto set: total evaluations <= budget, each
  design has numeric objective values and a pass/fail status per constraint,
  and at most 20 designs are returned.
* Budget clamping: below 100 -> 100, above 10000 -> 10000, default when unset.
* Physics predictor SageMaker path with a 5-second timeout that falls back to
  the local Bar-Cohen analytical model.
* Handler routing (direct MCP args + HTTP body) and config-error responses.

Budgets and populations are kept small so the NSGA-II runs finish quickly.
The SageMaker runtime client is mocked with ``unittest.mock`` and bounded by a
short ``SAGEMAKER_TIMEOUT_SECONDS`` so the fallback test runs fast.

Validates: Requirements 5.2, 5.3, 5.4, 5.8
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock

import pytest

from tools.optimizer import handler as opt


# --- Shared fixtures --------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure a clean, local-only optimizer configuration for every test."""
    for var in (
        "SAGEMAKER_ENDPOINT_NAME",
        "SAGEMAKER_REGION",
        "SAGEMAKER_TIMEOUT_SECONDS",
        "OPT_DEFAULT_BUDGET",
        "OPT_DEFAULT_POPULATION_SIZE",
        "OPT_TIME_BUDGET_SECONDS",
        "OPT_MAX_PARETO",
    ):
        monkeypatch.delenv(var, raising=False)


DESIGN_SPACE = {
    "fin_count": {"min": 5, "max": 30, "type": "int"},
    "fin_height_m": {"min": 0.01, "max": 0.05, "type": "float"},
    "width_m": {"min": 0.05, "max": 0.15, "type": "float"},
}
OBJECTIVES = ["maximize:heat_dissipation_W", "minimize:weight_kg"]
CONSTRAINTS = {
    "max_surface_temp": {"field": "surface_temperature_C", "type": "max", "limit": 120.0},
    "max_weight": {"field": "weight_kg", "type": "max", "limit": 10.0},
}


# --- Valid config -> valid Pareto set (Requirements 5.2, 5.4) ---------------


def test_valid_config_returns_valid_pareto_set() -> None:
    result = opt.run_optimization(
        objectives=OBJECTIVES,
        design_space=DESIGN_SPACE,
        constraints=CONSTRAINTS,
        budget=120,
        population_size=20,
        model_type="thermal_analytical",
        source_drawing_id="drawing-1",
        extraction_id="extract-1",
    )

    # Evaluations must not exceed the (clamped) budget (Requirement 5.2).
    assert result.n_evaluations <= result.budget == 120
    assert result.generations * result.population_size <= result.budget

    # At most 20 Pareto designs (Requirement 5.4 / design "up to 20").
    assert 1 <= len(result.pareto_designs) <= opt.MAX_PARETO_DESIGNS
    assert (
        len(result.pareto_objectives)
        == len(result.pareto_constraints)
        == len(result.pareto_designs)
    )

    # Each design: numeric objective values + per-constraint pass/fail status.
    for objectives in result.pareto_objectives:
        assert set(objectives) == set(OBJECTIVES)
        assert all(isinstance(v, float) for v in objectives.values())
    for statuses in result.pareto_constraints:
        assert set(statuses) == set(CONSTRAINTS)
        assert all(isinstance(v, bool) for v in statuses.values())

    # Traceability + convergence history recorded.
    assert result.source_drawing_id == "drawing-1"
    assert result.extraction_id == "extract-1"
    assert result.predictor_used == "bar_cohen_analytical"
    assert result.fell_back_to_local is False
    assert result.time_budget_exceeded is False
    assert len(result.convergence_history) == result.generations
    assert len(result.top_recommendations) <= 5


def test_optimization_without_constraints_returns_empty_status_maps() -> None:
    result = opt.run_optimization(
        objectives=["minimize:weight_kg"],
        design_space=DESIGN_SPACE,
        constraints={},
        budget=100,
        population_size=20,
    )
    assert result.n_evaluations <= 100
    assert result.feasible is True
    for statuses in result.pareto_constraints:
        assert statuses == {}


# --- Budget clamping (Requirement 5.2) --------------------------------------


def test_clamp_budget_below_minimum() -> None:
    assert opt.clamp_budget(1) == opt.MIN_BUDGET
    assert opt.clamp_budget(99) == opt.MIN_BUDGET


def test_clamp_budget_above_maximum() -> None:
    assert opt.clamp_budget(50_000) == opt.MAX_BUDGET
    assert opt.clamp_budget(10_001) == opt.MAX_BUDGET


def test_clamp_budget_within_range_unchanged() -> None:
    assert opt.clamp_budget(100) == 100
    assert opt.clamp_budget(1000) == 1000
    assert opt.clamp_budget(10_000) == 10_000


def test_clamp_budget_default_when_none() -> None:
    assert opt.clamp_budget(None) == opt.DEFAULT_BUDGET


def test_clamp_budget_default_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPT_DEFAULT_BUDGET", "2500")
    assert opt.clamp_budget(None) == 2500


def test_run_optimization_clamps_sub_minimum_budget() -> None:
    # A below-minimum budget is clamped up to 100; evaluations stay within it.
    result = opt.run_optimization(
        objectives=["minimize:weight_kg"],
        design_space=DESIGN_SPACE,
        constraints={},
        budget=10,
        population_size=20,
    )
    assert result.budget == opt.MIN_BUDGET == 100
    assert result.n_evaluations <= 100


def test_clamp_population_bounds() -> None:
    assert opt.clamp_population(1, budget=100) == 2  # floor of 2
    assert opt.clamp_population(50, budget=40) == 40  # cannot exceed budget
    assert opt.clamp_population(30, budget=1000) == 30


# --- Bar-Cohen analytical model ---------------------------------------------


def test_bar_cohen_predict_returns_numeric_metrics() -> None:
    metrics = opt.bar_cohen_predict(
        {"fin_count": 12, "fin_height_m": 0.03, "width_m": 0.1, "material": "aluminum"}
    )
    for key in (
        "heat_dissipation_W",
        "weight_kg",
        "thermal_resistance_KpW",
        "surface_temperature_C",
        "volume_m3",
    ):
        assert key in metrics
        assert isinstance(metrics[key], float)
    assert metrics["heat_dissipation_W"] > 0
    assert metrics["weight_kg"] > 0


def test_bar_cohen_predict_handles_missing_params() -> None:
    # Even an empty design produces numeric metrics (universal fallback).
    metrics = opt.bar_cohen_predict({})
    assert metrics["heat_dissipation_W"] > 0
    assert metrics["weight_kg"] > 0


# --- SageMaker fallback on 5-second timeout (Requirement 5.3) ---------------


def test_sagemaker_timeout_falls_back_to_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAGEMAKER_ENDPOINT_NAME", "cae-platform-surrogate")
    monkeypatch.setenv("SAGEMAKER_TIMEOUT_SECONDS", "0.2")

    # A SageMaker client whose invoke_endpoint hangs past the timeout.
    def _slow_invoke(**_kwargs: object) -> dict:
        time.sleep(0.6)
        return {"Body": MagicMock()}

    slow_client = MagicMock()
    slow_client.invoke_endpoint.side_effect = _slow_invoke
    monkeypatch.setattr(opt, "get_sagemaker_client", lambda: slow_client)

    result = opt.run_optimization(
        objectives=["minimize:weight_kg"],
        design_space=DESIGN_SPACE,
        constraints={},
        budget=100,
        population_size=10,
        model_type="sagemaker",
    )

    # The endpoint timed out -> fell back to the local Bar-Cohen model, and
    # the optimization still produced a valid Pareto set.
    assert result.fell_back_to_local is True
    assert result.predictor_used == "bar_cohen_analytical"
    assert len(result.pareto_designs) >= 1
    assert result.n_evaluations <= result.budget
    assert any("SageMaker" in n for n in result.notifications)


def test_sagemaker_endpoint_used_when_responsive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAGEMAKER_ENDPOINT_NAME", "cae-platform-surrogate")
    monkeypatch.setenv("SAGEMAKER_TIMEOUT_SECONDS", "5")

    body = MagicMock()
    body.read.return_value = json.dumps(
        {"metrics": {"heat_dissipation_W": 42.0, "weight_kg": 1.5}}
    ).encode("utf-8")
    client = MagicMock()
    client.invoke_endpoint.return_value = {"Body": body}
    monkeypatch.setattr(opt, "get_sagemaker_client", lambda: client)

    predictor = opt.PhysicsPredictor(model_type="sagemaker")
    metrics = predictor.predict({"fin_count": 10, "width_m": 0.1})

    assert predictor.used_sagemaker is True
    assert predictor.fell_back_to_local is False
    assert metrics["heat_dissipation_W"] == 42.0
    # Raw params are merged so constraint/objective fields still resolve.
    assert metrics["fin_count"] == 10.0


def test_local_model_used_when_endpoint_not_configured() -> None:
    predictor = opt.PhysicsPredictor(model_type="thermal_analytical")
    predictor.predict({"fin_count": 10})
    assert predictor.used_sagemaker is False
    assert predictor.predictor_used == "bar_cohen_analytical"


# --- Time budget termination (Requirement 5.8) ------------------------------


def test_time_budget_exceeded_sets_notification_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    # A zero-second time budget forces early termination after one generation.
    monkeypatch.setenv("OPT_TIME_BUDGET_SECONDS", "0")
    result = opt.run_optimization(
        objectives=OBJECTIVES,
        design_space=DESIGN_SPACE,
        constraints=CONSTRAINTS,
        budget=10_000,
        population_size=20,
    )
    assert result.time_budget_exceeded is True
    # Terminated early -> far fewer evaluations than the large budget.
    assert result.n_evaluations < result.budget
    assert len(result.pareto_designs) >= 1
    assert any("Time budget" in n for n in result.notifications)


# --- Objective / design-space validation ------------------------------------


def test_invalid_objective_format_raises() -> None:
    with pytest.raises(opt.OptimizationConfigError):
        opt.run_optimization(
            objectives=["heat_dissipation_W"],  # missing sense
            design_space=DESIGN_SPACE,
            budget=100,
            population_size=20,
        )


def test_empty_objectives_raises() -> None:
    with pytest.raises(opt.OptimizationConfigError):
        opt.run_optimization(objectives=[], design_space=DESIGN_SPACE)


def test_empty_design_space_raises() -> None:
    with pytest.raises(opt.OptimizationConfigError):
        opt.run_optimization(objectives=OBJECTIVES, design_space={})


def test_design_space_missing_bounds_raises() -> None:
    with pytest.raises(opt.OptimizationConfigError):
        opt.run_optimization(
            objectives=OBJECTIVES,
            design_space={"width_m": {"type": "float"}},  # no min/max
        )


# --- Handler routing --------------------------------------------------------


def test_handler_direct_mcp_args() -> None:
    event = {
        "objectives": ["minimize:weight_kg"],
        "design_space": DESIGN_SPACE,
        "constraints": {},
        "budget": 100,
        "population_size": 10,
    }
    resp = opt.handler(event)
    assert resp["statusCode"] == 200
    payload = json.loads(resp["body"])
    assert payload["status"] == "ok"
    assert payload["result"]["n_evaluations"] <= payload["result"]["budget"]
    assert len(payload["result"]["pareto_designs"]) >= 1


def test_handler_http_body() -> None:
    event = {
        "body": json.dumps(
            {
                "objectives": ["minimize:weight_kg"],
                "design_space": DESIGN_SPACE,
                "budget": 100,
                "population_size": 10,
            }
        )
    }
    resp = opt.handler(event)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["status"] == "ok"


def test_handler_invalid_config_returns_400() -> None:
    event = {"objectives": [], "design_space": DESIGN_SPACE}
    resp = opt.handler(event)
    assert resp["statusCode"] == 400
    assert json.loads(resp["body"])["status"] == "error"
