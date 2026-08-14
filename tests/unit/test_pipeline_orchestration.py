"""Unit tests for ``shared.pipeline.orchestration`` (Requirement 11).

Covers the pure pipeline helpers that wire the end-to-end flow:

* stage sequencing / Q&A availability (Req 11.1),
* top-Pareto selection for auto 3D generation (Req 11.4),
* traceability record construction (Req 11.5),
* missing-constraint detection (Req 11.3),
* optimization-constraint extraction (Req 11.2).
"""

from __future__ import annotations

from shared.pipeline.orchestration import (
    AUTO_3D_STAGE,
    KB_INGESTION_STAGE,
    OPTIMIZATION_STAGE,
    PIPELINE_STAGES,
    QA_AVAILABLE_STAGE,
    REQUIRED_CONSTRAINT_CATEGORIES,
    attach_traceability,
    build_traceability_record,
    extract_optimization_constraints,
    find_missing_constraint_fields,
    is_qa_available,
    is_stage_reached,
    next_pipeline_stage,
    select_top_pareto_designs,
)

# ---------------------------------------------------------------------------
# Pipeline stage sequencing (Req 11.1 / 11.4)
# ---------------------------------------------------------------------------


def test_pipeline_stage_order_is_the_documented_end_to_end_flow() -> None:
    assert PIPELINE_STAGES == (
        "upload_confirmed",
        "extraction",
        "kb_ingestion",
        "qa_available",
        "optimization",
        "auto_3d_generation",
    )


def test_next_pipeline_stage_advances_through_the_flow() -> None:
    assert next_pipeline_stage("upload_confirmed") == "extraction"
    assert next_pipeline_stage("extraction") == KB_INGESTION_STAGE
    assert next_pipeline_stage(KB_INGESTION_STAGE) == QA_AVAILABLE_STAGE
    assert next_pipeline_stage(QA_AVAILABLE_STAGE) == OPTIMIZATION_STAGE
    assert next_pipeline_stage(OPTIMIZATION_STAGE) == AUTO_3D_STAGE


def test_next_pipeline_stage_terminal_and_unknown() -> None:
    assert next_pipeline_stage(AUTO_3D_STAGE) is None
    assert next_pipeline_stage("not-a-stage") is None


def test_next_pipeline_stage_none_or_empty_returns_entry_point() -> None:
    assert next_pipeline_stage(None) == PIPELINE_STAGES[0]
    assert next_pipeline_stage("") == PIPELINE_STAGES[0]
    assert next_pipeline_stage("   ") == PIPELINE_STAGES[0]


def test_is_qa_available_only_after_qa_stage_reached() -> None:
    # Before Q&A availability.
    assert not is_qa_available("upload_confirmed")
    assert not is_qa_available("extraction")
    assert not is_qa_available("kb_ingestion")
    # At and after Q&A availability (Req 11.1: remains available downstream).
    assert is_qa_available("qa_available")
    assert is_qa_available("optimization")
    assert is_qa_available("auto_3d_generation")


def test_is_qa_available_rejects_unknown_state() -> None:
    assert not is_qa_available("bogus")
    assert not is_qa_available(None)


def test_is_stage_reached_monotonic() -> None:
    assert is_stage_reached("optimization", "kb_ingestion")
    assert is_stage_reached("kb_ingestion", "kb_ingestion")
    assert not is_stage_reached("extraction", "optimization")
    assert not is_stage_reached("extraction", "bogus")


# ---------------------------------------------------------------------------
# Top Pareto selection (Req 11.4)
# ---------------------------------------------------------------------------


def test_select_top_pareto_defaults_to_five() -> None:
    designs = [{"id": i} for i in range(10)]
    selected = select_top_pareto_designs(designs)
    assert len(selected) == 5
    # Preserves order when no rank present.
    assert [d["id"] for d in selected] == [0, 1, 2, 3, 4]


def test_select_top_pareto_respects_explicit_rank() -> None:
    designs = [
        {"id": "a", "rank": 3},
        {"id": "b", "rank": 1},
        {"id": "c", "rank": 2},
    ]
    selected = select_top_pareto_designs(designs, n=2)
    assert [d["id"] for d in selected] == ["b", "c"]


def test_select_top_pareto_stable_on_rank_ties() -> None:
    designs = [
        {"id": "a", "rank": 1},
        {"id": "b", "rank": 1},
        {"id": "c", "rank": 1},
    ]
    selected = select_top_pareto_designs(designs, n=3)
    assert [d["id"] for d in selected] == ["a", "b", "c"]


def test_select_top_pareto_preserves_order_when_rank_partial() -> None:
    # Not all designs carry a rank -> assume already-ranked input order.
    designs = [{"id": "a", "rank": 5}, {"id": "b"}, {"id": "c", "rank": 1}]
    selected = select_top_pareto_designs(designs, n=3)
    assert [d["id"] for d in selected] == ["a", "b", "c"]


def test_select_top_pareto_custom_rank_key() -> None:
    designs = [{"id": "a", "score": 2}, {"id": "b", "score": 1}]
    selected = select_top_pareto_designs(designs, n=2, rank_key="score")
    assert [d["id"] for d in selected] == ["b", "a"]


def test_select_top_pareto_never_mutates_input() -> None:
    designs = [{"id": "a", "rank": 2}, {"id": "b", "rank": 1}]
    original = [dict(d) for d in designs]
    select_top_pareto_designs(designs, n=1)
    assert designs == original  # order + contents untouched


def test_select_top_pareto_edge_cases() -> None:
    assert select_top_pareto_designs([], n=5) == []
    assert select_top_pareto_designs(None, n=5) == []
    assert select_top_pareto_designs([{"id": 1}], n=0) == []
    # n larger than available returns all.
    assert len(select_top_pareto_designs([{"id": 1}, {"id": 2}], n=5)) == 2


# ---------------------------------------------------------------------------
# Traceability (Req 11.5)
# ---------------------------------------------------------------------------


def test_build_traceability_record_adds_ids() -> None:
    record = build_traceability_record({"weight": 1.2}, "DRW-1", "EXT-9")
    assert record["drawing_id"] == "DRW-1"
    assert record["extraction_id"] == "EXT-9"
    assert record["weight"] == 1.2


def test_build_traceability_record_does_not_mutate_input() -> None:
    original = {"weight": 1.2}
    record = build_traceability_record(original, "DRW-1", "EXT-9")
    assert original == {"weight": 1.2}
    assert record is not original


def test_build_traceability_record_wraps_non_mapping() -> None:
    record = build_traceability_record(42, "DRW-1", "EXT-9")
    assert record["value"] == 42
    assert record["drawing_id"] == "DRW-1"
    assert record["extraction_id"] == "EXT-9"


def test_build_traceability_record_none_ids_become_empty_string() -> None:
    record = build_traceability_record({}, None, None)
    assert record["drawing_id"] == ""
    assert record["extraction_id"] == ""


def test_attach_traceability_covers_every_record() -> None:
    results = [{"a": 1}, {"b": 2}, {"c": 3}]
    traced = attach_traceability(results, "DRW-7", "EXT-3")
    assert len(traced) == 3
    for record in traced:
        assert record["drawing_id"] == "DRW-7"
        assert record["extraction_id"] == "EXT-3"
    # Inputs untouched.
    assert results == [{"a": 1}, {"b": 2}, {"c": 3}]


def test_attach_traceability_empty() -> None:
    assert attach_traceability([], "DRW", "EXT") == []
    assert attach_traceability(None, "DRW", "EXT") == []


# ---------------------------------------------------------------------------
# Constraint extraction + missing-field detection (Req 11.2 / 11.3)
# ---------------------------------------------------------------------------


def _full_ground_truth() -> dict:
    return {
        "drawing_id": "DRW-100",
        "title_block": {
            "material": "Aluminum 6061-T6",
            "units": "mm",
            "general_tolerance": "ISO 2768-m",
        },
        "bom": [{"material": "Steel"}],
        "dimensions": [
            {"value": 50.0, "tolerance": "\u00b10.1", "type": "linear", "feature": "base"},
            {"value": 10.0, "tolerance": None, "type": "diameter", "feature": "hole"},
        ],
        "gdt_callouts": [
            {"characteristic": "flatness", "tolerance_value": 0.05, "unit": "mm"},
        ],
    }


def test_extract_constraints_full_document_has_all_categories() -> None:
    constraints = extract_optimization_constraints(_full_ground_truth())
    assert constraints["drawing_id"] == "DRW-100"
    assert constraints["missing_categories"] == []
    assert set(constraints["present_categories"]) == set(REQUIRED_CONSTRAINT_CATEGORIES)
    assert constraints["can_optimize"] is True

    # Dimensional bounds: symmetric tolerance widens, missing tolerance -> point.
    bounds = {b["feature"]: b for b in constraints["dimensional_bounds"]}
    assert bounds["base"]["lower_bound"] == 49.9
    assert bounds["base"]["upper_bound"] == 50.1
    assert bounds["base"]["has_explicit_tolerance"] is True
    assert bounds["hole"]["lower_bound"] == 10.0
    assert bounds["hole"]["upper_bound"] == 10.0
    assert bounds["hole"]["has_explicit_tolerance"] is False

    # Material limits from title block + BOM.
    raws = {m["raw"] for m in constraints["material_limits"]}
    assert "Aluminum 6061-T6" in raws
    assert "Steel" in raws

    # Tolerance ranges from ISO 2768 general tolerance + GD&T callout.
    labels = {t["label"] for t in constraints["tolerance_ranges"]}
    assert any("2768" in label for label in labels)
    assert "flatness" in labels


def test_find_missing_constraint_fields_none_missing() -> None:
    assert find_missing_constraint_fields(_full_ground_truth()) == []


def test_find_missing_constraint_fields_all_missing_for_empty() -> None:
    missing = find_missing_constraint_fields({})
    assert missing == list(REQUIRED_CONSTRAINT_CATEGORIES)


def test_find_missing_constraint_fields_reports_null_material() -> None:
    gt = _full_ground_truth()
    gt["title_block"]["material"] = None
    gt["bom"] = [{"material": "TBD"}]  # placeholder counts as absent
    missing = find_missing_constraint_fields(gt)
    assert missing == ["material_limits"]


def test_find_missing_constraint_fields_reports_missing_dimensions() -> None:
    gt = _full_ground_truth()
    gt["dimensions"] = []
    assert find_missing_constraint_fields(gt) == ["dimensional_bounds"]


def test_find_missing_constraint_fields_reports_missing_tolerances() -> None:
    gt = _full_ground_truth()
    gt["title_block"]["general_tolerance"] = "as noted"  # no ISO 2768 class
    gt["gdt_callouts"] = []
    assert find_missing_constraint_fields(gt) == ["tolerance_ranges"]


def test_find_missing_constraint_fields_preserves_canonical_order() -> None:
    # Missing dimensional + tolerance, present material.
    gt = {
        "drawing_id": "DRW-X",
        "title_block": {"material": "Copper"},
        "dimensions": [],
        "gdt_callouts": [],
    }
    missing = find_missing_constraint_fields(gt)
    assert missing == ["dimensional_bounds", "tolerance_ranges"]


def test_extract_constraints_non_mapping_is_all_missing() -> None:
    constraints = extract_optimization_constraints("not a dict")
    assert constraints["missing_categories"] == list(REQUIRED_CONSTRAINT_CATEGORIES)
    assert constraints["drawing_id"] is None
    assert constraints["can_optimize"] is False
