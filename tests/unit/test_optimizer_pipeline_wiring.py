"""Unit tests for the Optimization Agent's end-to-end pipeline wiring.

Covers ``prepare_optimization_run`` and ``finalize_optimization_run`` in
``agents/optimizer/main.py``, which wire the pure ``shared.pipeline`` helpers
into the optimization workflow (Requirements 11.2, 11.3, 11.4, 11.5).
"""

from __future__ import annotations

from agents.optimizer.main import (
    DEFAULT_BUDGET,
    DEFAULT_POPULATION_SIZE,
    finalize_optimization_run,
    prepare_optimization_run,
)


def _full_ground_truth() -> dict:
    return {
        "drawing_id": "DRW-42",
        "title_block": {
            "material": "Aluminum 6061",
            "units": "mm",
            "general_tolerance": "ISO 2768-m",
        },
        "bom": [{"material": "Steel"}],
        "dimensions": [
            {"value": 50.0, "tolerance": "\u00b10.1", "type": "linear", "feature": "base"},
        ],
        "gdt_callouts": [
            {"characteristic": "flatness", "tolerance_value": 0.05, "unit": "mm"},
        ],
    }


# ---------------------------------------------------------------------------
# prepare_optimization_run — Req 11.2 (auto-use) and 11.3 (missing gate)
# ---------------------------------------------------------------------------


def test_prepare_auto_uses_ground_truth_constraints() -> None:
    """Req 11.2: constraints are auto-loaded from the Ground_Truth_JSON."""
    result = prepare_optimization_run(
        _full_ground_truth(),
        objectives=["minimize:weight_kg"],
        extraction_id="EXT-1",
    )
    assert result["status"] == "ready"
    assert result["can_proceed"] is True
    assert result["source_drawing_id"] == "DRW-42"
    assert result["extraction_id"] == "EXT-1"
    assert result["objectives"] == ["minimize:weight_kg"]
    assert result["budget"] == DEFAULT_BUDGET
    assert result["population_size"] == DEFAULT_POPULATION_SIZE
    # Constraints were extracted and carried through (no re-specification).
    constraints = result["constraints"]
    assert constraints["missing_categories"] == []
    assert constraints["dimensional_bounds"]
    assert constraints["material_limits"]
    assert constraints["tolerance_ranges"]


def test_prepare_honors_custom_budget_and_population() -> None:
    result = prepare_optimization_run(
        _full_ground_truth(), budget=500, population_size=40
    )
    assert result["budget"] == 500
    assert result["population_size"] == 40


def test_prepare_blocks_on_missing_constraints() -> None:
    """Req 11.3: missing constraint fields block the run and prompt for input."""
    gt = _full_ground_truth()
    gt["title_block"]["material"] = None
    gt["bom"] = []  # no material anywhere -> material_limits missing

    result = prepare_optimization_run(gt, extraction_id="EXT-2")
    assert result["status"] == "needs_manual_input"
    assert result["can_proceed"] is False
    assert result["missing_categories"] == ["material_limits"]
    assert "material limits" in result["message"]
    assert "manually" in result["message"].lower()
    assert result["source_drawing_id"] == "DRW-42"
    assert result["extraction_id"] == "EXT-2"


def test_prepare_blocks_reports_all_missing_categories() -> None:
    result = prepare_optimization_run({"drawing_id": "DRW-empty"})
    assert result["status"] == "needs_manual_input"
    assert result["missing_categories"] == [
        "dimensional_bounds",
        "material_limits",
        "tolerance_ranges",
    ]


# ---------------------------------------------------------------------------
# finalize_optimization_run — Req 11.5 (traceability) and 11.4 (auto 3D)
# ---------------------------------------------------------------------------


def test_finalize_attaches_traceability_to_every_record() -> None:
    """Req 11.5: drawing_id + extraction_id on every result record."""
    opt_result = {
        "pareto_designs": [{"fin_count": 10}, {"fin_count": 20}, {"fin_count": 30}],
        "generations": 25,
    }
    final = finalize_optimization_run(
        opt_result, source_drawing_id="DRW-42", extraction_id="EXT-9"
    )
    assert final["source_drawing_id"] == "DRW-42"
    assert final["extraction_id"] == "EXT-9"
    for record in final["pareto_designs"]:
        assert record["drawing_id"] == "DRW-42"
        assert record["extraction_id"] == "EXT-9"


def test_finalize_selects_top_five_for_auto_3d() -> None:
    """Req 11.4: up to 5 top-ranked designs auto-selected for 3D generation."""
    opt_result = {"pareto_designs": [{"id": i, "rank": i} for i in range(8)]}
    final = finalize_optimization_run(
        opt_result, source_drawing_id="DRW-1", extraction_id="EXT-1"
    )
    auto = final["auto_3d_designs"]
    assert len(auto) == 5
    assert [d["id"] for d in auto] == [0, 1, 2, 3, 4]
    # Auto-3D records also carry traceability.
    for record in auto:
        assert record["drawing_id"] == "DRW-1"
        assert record["extraction_id"] == "EXT-1"


def test_finalize_fewer_than_five_designs() -> None:
    opt_result = {"pareto_designs": [{"id": 1}, {"id": 2}]}
    final = finalize_optimization_run(
        opt_result, source_drawing_id="DRW-1", extraction_id="EXT-1"
    )
    assert len(final["auto_3d_designs"]) == 2


def test_finalize_does_not_mutate_input() -> None:
    opt_result = {"pareto_designs": [{"id": 1}]}
    finalize_optimization_run(opt_result, source_drawing_id="D", extraction_id="E")
    # Original pareto design untouched (no traceability keys leaked in).
    assert opt_result["pareto_designs"] == [{"id": 1}]


def test_finalize_handles_no_pareto_designs() -> None:
    final = finalize_optimization_run(
        {"generations": 10}, source_drawing_id="D", extraction_id="E"
    )
    assert final["pareto_designs"] == []
    assert final["auto_3d_designs"] == []
    assert final["source_drawing_id"] == "D"
    assert final["extraction_id"] == "E"


def test_finalize_custom_top_n() -> None:
    opt_result = {"pareto_designs": [{"id": i} for i in range(6)]}
    final = finalize_optimization_run(
        opt_result, source_drawing_id="D", extraction_id="E", n_top=3
    )
    assert len(final["auto_3d_designs"]) == 3
